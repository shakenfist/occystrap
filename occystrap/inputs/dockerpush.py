# Fetch images from the local Docker or Podman daemon by pushing
# to an embedded registry. This avoids the Docker Engine API
# limitation where /images/{name}/get returns a single sequential
# tarball that cannot be parallelized.
#
# Instead, we start a minimal HTTP server implementing the Docker
# Registry V2 push-path API on localhost, then use Docker's own
# push mechanism (POST /images/{name}/push) to send layers to it.
# Docker push uploads layers individually and in parallel using
# the Registry V2 protocol, providing significantly better
# throughput for multi-layer images.
#
# Docker treats the entire 127.0.0.0/8 range as "insecure",
# meaning it skips TLS certificate verification. However,
# some Docker versions (especially those using containerd for
# registry operations) do not fall back from HTTPS to HTTP
# when the server doesn't speak TLS. To avoid this, we serve
# HTTPS with an ephemeral self-signed certificate generated
# via openssl. Docker accepts the self-signed cert because
# 127.0.0.1 is in the insecure range. We use 127.0.0.1 (not
# "localhost") because the hostname may resolve to ::1 and
# not receive the insecure exemption in all Docker versions.
#
# Docker Engine API documentation:
# https://docs.docker.com/engine/api/
#
# Docker Registry HTTP API V2:
# https://distribution.github.io/distribution/spec/api/

import base64
import hashlib
import http.server
import io
import json
import os
import ssl
import subprocess
import tempfile
import threading
import uuid
from urllib.parse import (
    urlparse, parse_qs, quote, urlencode)

import requests_unixsocket
from shakenfist_utilities import logs

from occystrap import compression
from occystrap import constants
from occystrap.inputs.base import ImageInput, always_fetch


LOG = logs.setup_console(__name__)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'

COPY_BUFSIZE = 1024 * 1024  # 1MB chunks

# Seconds to wait for the manifest after _push_image
# returns. Since _push_image consumes the entire push
# stream, the manifest should already be in memory by
# then -- this is just a safety margin.
MANIFEST_TIMEOUT = 10


class _RegistryState:
    """Shared state between the embedded registry HTTP handler
    threads and the main fetch() thread.

    All mutations to uploads, blobs, and manifest_data must
    be protected by the lock.
    """

    def __init__(self, temp_dir=None):
        self.temp_dir = temp_dir
        # In-progress uploads: uuid -> {path, file, offset}
        self.uploads = {}
        # Completed blobs: digest_hex -> temp file path
        self.blobs = {}
        # Received manifest
        self.manifest_data = None
        self.manifest_event = threading.Event()
        self.lock = threading.Lock()
        # Compressed digest hexes that Docker should skip
        # (populated from cache + digest mapping before
        # push). When non-empty, HEAD returns 200 for
        # these digests, causing Docker to skip upload.
        self.skip_digests = set()


class EmbeddedRegistryHandler(
        http.server.BaseHTTPRequestHandler):
    """HTTP handler implementing the Docker Registry V2
    push-path endpoints.

    Docker's push command uses these endpoints in order:
    1. GET /v2/ - Version check
    2. HEAD /v2/{name}/blobs/{digest} - Check blob existence
    3. POST /v2/{name}/blobs/uploads/ - Start upload
    4. PATCH /v2/{name}/blobs/uploads/{uuid} - Receive chunks
    5. PUT /v2/{name}/blobs/uploads/{uuid}?digest=... - Complete
    6. PUT /v2/{name}/manifests/{tag} - Receive manifest

    All received blobs are stored as temp files. The manifest
    is stored in memory.
    """

    # Close idle connections after 30 seconds to
    # prevent stale handler threads from lingering.
    timeout = 30

    @property
    def state(self):
        return self.server.registry_state

    def log_message(self, format, *args):
        LOG.debug('Registry: %s' % (format % args))

    def _read_body(self):
        """Read the full request body.

        Handles both Content-Length and chunked
        Transfer-Encoding. Falls back to empty body
        if neither is present.
        """
        transfer_enc = self.headers.get(
            'Transfer-Encoding', '').lower()
        if 'chunked' in transfer_enc:
            return self._read_chunked_body()

        length = int(
            self.headers.get('Content-Length', 0))
        if length > 0:
            return self.rfile.read(length)
        return b''

    def _read_chunked_body(self):
        """Read a chunked transfer-encoded body."""
        body = io.BytesIO()
        while True:
            line = self.rfile.readline()
            # Chunk size is hex, may have extensions
            # after a semicolon
            chunk_header = line.strip().split(b';')[0]
            if not chunk_header:
                # Connection closed mid-stream
                break
            chunk_size = int(chunk_header, 16)
            if chunk_size == 0:
                # Read trailing CRLF after final chunk
                self.rfile.readline()
                break
            body.write(
                self.rfile.read(chunk_size))
            # Read trailing CRLF after chunk data
            self.rfile.readline()
        return body.getvalue()

    def _parse_path(self):
        """Parse the request path and query string."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        return parsed.path, params

    def do_GET(self):
        """Handle GET /v2/ version check."""
        path, _ = self._parse_path()
        if path == '/v2/' or path == '/v2':
            self.send_response(200)
            self.send_header(
                'Docker-Distribution-API-Version',
                'registry/2.0')
            self.send_header(
                'Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        """Handle HEAD /v2/{name}/blobs/{digest}.

        Returns 200 for digests in skip_digests (cached
        layers) or blobs (already uploaded), causing
        Docker to skip the upload or confirm receipt.
        Returns 404 otherwise to force Docker to upload.
        """
        path, _ = self._parse_path()

        # Extract digest from path
        # Format: /v2/{name}/blobs/sha256:{hex}
        if '/blobs/sha256:' in path:
            digest_hex = path.split(
                '/blobs/sha256:')[1]
            if digest_hex in \
                    self.state.skip_digests:
                self.send_response(200)
                self.send_header(
                    'Docker-Content-Digest',
                    'sha256:%s' % digest_hex)
                self.send_header(
                    'Content-Length', '0')
                self.end_headers()
                return

            with self.state.lock:
                blob_path = self.state.blobs.get(
                    digest_hex)
            if blob_path:
                blob_size = os.path.getsize(
                    blob_path)
                self.send_response(200)
                self.send_header(
                    'Docker-Content-Digest',
                    'sha256:%s' % digest_hex)
                self.send_header(
                    'Content-Length',
                    str(blob_size))
                self.end_headers()
                return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        """Handle POST /v2/{name}/blobs/uploads/.

        Start a new blob upload. Returns a UUID and Location
        header for subsequent PATCH/PUT requests.
        """
        path, _ = self._parse_path()

        if '/blobs/uploads' not in path:
            self.send_response(404)
            self.end_headers()
            return

        upload_uuid = str(uuid.uuid4())

        # Create temp file for this upload
        tf = tempfile.NamedTemporaryFile(
            delete=False, dir=self.state.temp_dir)
        tf_path = tf.name
        tf.close()

        with self.state.lock:
            self.state.uploads[upload_uuid] = {
                'path': tf_path,
                'offset': 0,
            }

        # Extract the repository name from the path
        # Path format: /v2/{name}/blobs/uploads/
        parts = path.split('/blobs/uploads')
        repo_path = parts[0] if parts else '/v2/_'

        location = (
            '%s/blobs/uploads/%s'
            % (repo_path, upload_uuid))

        self.send_response(202)
        self.send_header('Location', location)
        self.send_header(
            'Docker-Upload-UUID', upload_uuid)
        self.send_header('Range', '0-0')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_PATCH(self):
        """Handle PATCH /v2/{name}/blobs/uploads/{uuid}.

        Receive blob data chunks and append to temp file.
        """
        path, _ = self._parse_path()

        # Extract UUID from path
        parts = path.split('/blobs/uploads/')
        if len(parts) < 2:
            self.send_response(404)
            self.end_headers()
            return

        upload_uuid = parts[1].strip('/')

        # Read body outside the lock to avoid
        # blocking other handler threads during I/O
        data = self._read_body()

        with self.state.lock:
            upload = self.state.uploads.get(upload_uuid)
            if not upload:
                self.send_response(404)
                self.end_headers()
                return
            upload_path = upload['path']

        # File write outside lock: each upload has
        # its own temp file, so no contention. Docker
        # serializes PATCH requests per upload UUID.
        with open(upload_path, 'ab') as f:
            f.write(data)

        with self.state.lock:
            upload = self.state.uploads.get(upload_uuid)
            if upload:
                upload['offset'] += len(data)
                new_offset = upload['offset']
            else:
                new_offset = len(data)

        # Build location for response
        repo_path = path.split(
            '/blobs/uploads/')[0]
        location = (
            '%s/blobs/uploads/%s'
            % (repo_path, upload_uuid))

        self.send_response(202)
        self.send_header('Location', location)
        self.send_header(
            'Docker-Upload-UUID', upload_uuid)
        self.send_header(
            'Range', '0-%d' % (new_offset - 1))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_PUT(self):
        """Handle PUT for blob completion and manifest upload.

        Two cases:
        - PUT /v2/{name}/blobs/uploads/{uuid}?digest=...
          Complete a blob upload with digest verification.
        - PUT /v2/{name}/manifests/{tag}
          Receive the image manifest.
        """
        path, params = self._parse_path()

        if '/manifests/' in path:
            self._handle_manifest_put()
            return

        if '/blobs/uploads/' not in path:
            self.send_response(404)
            self.end_headers()
            return

        self._handle_blob_put(path, params)

    def _handle_blob_put(self, path, params):
        """Complete a blob upload with digest verification."""
        # Extract UUID
        parts = path.split('/blobs/uploads/')
        if len(parts) < 2:
            self.send_response(404)
            self.end_headers()
            return

        upload_uuid = parts[1].strip('/')

        with self.state.lock:
            upload = self.state.uploads.get(upload_uuid)
            if not upload:
                self.send_response(404)
                self.end_headers()
                return
            upload_path = upload['path']

        # Read any remaining body data (monolithic upload
        # sends all data in the PUT). File write outside
        # lock: each upload has its own temp file.
        data = self._read_body()
        if data:
            with open(upload_path, 'ab') as f:
                f.write(data)

        # Get expected digest from query params
        digest_list = params.get('digest', [])
        if not digest_list:
            self.send_response(400)
            self.end_headers()
            return

        expected_digest = digest_list[0]
        if expected_digest.startswith('sha256:'):
            expected_hex = expected_digest[7:]
        else:
            expected_hex = expected_digest

        # Verify SHA256
        h = hashlib.sha256()
        with open(upload_path, 'rb') as f:
            while True:
                chunk = f.read(COPY_BUFSIZE)
                if not chunk:
                    break
                h.update(chunk)

        actual_hex = h.hexdigest()
        if actual_hex != expected_hex:
            LOG.error(
                'Blob digest mismatch: expected %s,'
                ' got %s' % (expected_hex, actual_hex))
            # Clean up temp file
            try:
                os.unlink(upload_path)
            except OSError:
                pass
            with self.state.lock:
                del self.state.uploads[upload_uuid]
            self.send_response(400)
            self.end_headers()
            return

        # Move blob to completed state
        with self.state.lock:
            self.state.blobs[expected_hex] = upload_path
            del self.state.uploads[upload_uuid]

        blob_size = os.path.getsize(upload_path)
        LOG.debug(
            'Received blob sha256:%s... (%d bytes)'
            % (expected_hex[:12], blob_size))

        self.send_response(201)
        self.send_header(
            'Docker-Content-Digest',
            'sha256:%s' % expected_hex)
        self.send_header('Content-Length', '0')
        self.send_header(
            'Location',
            '/v2/_/blobs/sha256:%s' % expected_hex)
        self.end_headers()

    def _handle_manifest_put(self):
        """Receive and store the image manifest."""
        data = self._read_body()

        # Compute digest of manifest
        h = hashlib.sha256()
        h.update(data)
        manifest_digest = h.hexdigest()

        with self.state.lock:
            self.state.manifest_data = data

        LOG.debug(
            'Received manifest (%d bytes, sha256:%s...)'
            % (len(data), manifest_digest[:12]))

        # Signal that the push is complete
        self.state.manifest_event.set()

        self.send_response(201)
        self.send_header(
            'Docker-Content-Digest',
            'sha256:%s' % manifest_digest)
        self.send_header('Content-Length', '0')
        self.end_headers()


class Image(ImageInput):
    """Fetch images from Docker/Podman by pushing to an
    embedded registry.

    Instead of using Docker's /images/{name}/get API (which
    returns a single sequential tarball), this input starts a
    minimal HTTPS server on localhost (with an ephemeral
    self-signed certificate) and uses Docker's push mechanism
    to transfer layers in parallel.

    Limitations:
    - Only supports single-platform V2 manifests. If
      Docker pushes a manifest list (fat manifest) for a
      multi-arch image, parsing will fail. Use the
      registry:// input for multi-arch images.
    - Layer decompression uses streaming with constant
      memory (1MB buffer), but blob data is received to
      temp files on disk during the push.
    """

    def __init__(self, image, tag='latest',
                 socket_path=DEFAULT_SOCKET_PATH,
                 temp_dir=None, layer_cache=None,
                 filters_hash='none'):
        self._image = image
        self._tag = tag
        self.socket_path = socket_path
        self.temp_dir = temp_dir
        self.layer_cache = layer_cache
        self.filters_hash = filters_hash
        self._session = None

    @property
    def image(self):
        """Return the image name."""
        return self._image

    @property
    def tag(self):
        """Return the image tag."""
        return self._tag

    def _get_session(self):
        if self._session is None:
            self._session = \
                requests_unixsocket.Session()
        return self._session

    def _socket_url(self, path):
        # requests_unixsocket uses http+unix:// with
        # URL-encoded socket path
        encoded_socket = self.socket_path.replace(
            '/', '%2F')
        return 'http+unix://%s%s' % (
            encoded_socket, path)

    def _request(self, method, path, stream=False,
                 headers=None, data=None):
        session = self._get_session()
        url = self._socket_url(path)
        LOG.debug(
            'Docker API request: %s %s'
            % (method, path))
        r = session.request(
            method, url, stream=stream,
            headers=headers, data=data)
        return r

    def _get_image_reference(self):
        return '%s:%s' % (self.image, self.tag)

    def _tag_image(self, repo, tag):
        """Tag an image for pushing to localhost registry.

        Uses POST /images/{name}/tag to create a new tag
        pointing to the same image.
        """
        ref = self._get_image_reference()
        params = urlencode({'repo': repo, 'tag': tag})
        path = '/images/%s/tag?%s' % (
            quote(ref, safe=''), params)
        r = self._request('POST', path)
        if r.status_code == 404:
            raise Exception(
                'Image not found: %s' % ref)
        if r.status_code not in (200, 201):
            raise Exception(
                'Failed to tag image: %d %s'
                % (r.status_code, r.text))
        LOG.debug('Tagged %s as %s:%s' % (ref, repo, tag))

    def _push_image(self, repo, tag):
        """Push an image to the embedded registry.

        Uses POST /images/{name}/push with streaming response.
        Consumes the stream and checks for errors.
        """
        push_ref = '%s:%s' % (repo, tag)
        path = '/images/%s/push' % quote(
            push_ref, safe='')
        auth_header = base64.b64encode(
            b'{}').decode('ascii')
        r = self._request(
            'POST', path, stream=True,
            headers={
                'X-Registry-Auth': auth_header
            })

        if r.status_code != 200:
            raise Exception(
                'Push request failed: %d %s'
                % (r.status_code, r.text))

        LOG.debug('Push started for %s' % push_ref)

        # Consume streaming response and check for
        # errors. Docker sends one JSON object per line.
        for line in r.iter_lines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if 'error' in event:
                raise Exception(
                    'Docker push failed: %s'
                    % event['error'])

            status = event.get('status', '')
            if status:
                LOG.debug('Push: %s' % status)

        LOG.debug('Push completed for %s' % push_ref)

    def _untag_image(self, repo, tag):
        """Remove a temporary tag from the Docker daemon.

        Uses DELETE /images/{name}?noprune=true to remove
        only the tag without deleting underlying layers.
        """
        ref = '%s:%s' % (repo, tag)
        path = '/images/%s?noprune=true' % quote(
            ref, safe='')
        r = self._request('DELETE', path)
        if r.status_code in (200, 404):
            LOG.debug('Untagged %s' % ref)
        else:
            LOG.warning(
                'Failed to untag %s: %d %s'
                % (ref, r.status_code, r.text))

    def _generate_tls_context(self):
        """Generate a self-signed TLS certificate and return
        an ssl.SSLContext.

        Uses openssl to create an ephemeral RSA key and
        self-signed certificate valid for 1 day. The cert
        files are deleted after loading into the context.
        """
        cert_fd, cert_path = tempfile.mkstemp(
            suffix='.pem', dir=self.temp_dir)
        key_fd, key_path = tempfile.mkstemp(
            suffix='.pem', dir=self.temp_dir)
        os.close(cert_fd)
        os.close(key_fd)

        try:
            subprocess.run(
                [
                    'openssl', 'req', '-x509',
                    '-newkey', 'rsa:2048',
                    '-keyout', key_path,
                    '-out', cert_path,
                    '-days', '1',
                    '-nodes',
                    '-subj', '/CN=127.0.0.1',
                    '-addext',
                    'subjectAltName=IP:127.0.0.1',
                ],
                check=True,
                capture_output=True,
            )
            context = ssl.SSLContext(
                ssl.PROTOCOL_TLS_SERVER)
            # Restrict protocol versions to TLS 1.2 and above.
            try:
                # Preferred on Python 3.7+
                context.minimum_version = ssl.TLSVersion.TLSv1_2
            except AttributeError:
                # Fallback for older Python versions
                try:
                    context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
                except AttributeError:
                    # If options or constants are unavailable, proceed with defaults.
                    pass
            context.load_cert_chain(cert_path, key_path)
            return context
        finally:
            for path in (cert_path, key_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _start_server(self, state):
        """Start the embedded registry HTTPS server.

        Binds to 127.0.0.1:0 (ephemeral port), wraps the
        socket with TLS using a self-signed certificate,
        and runs in a daemon thread.
        """
        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0),
            EmbeddedRegistryHandler)
        server.registry_state = state

        tls_context = self._generate_tls_context()
        server.socket = tls_context.wrap_socket(
            server.socket, server_side=True)

        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True)
        thread.start()
        port = server.server_address[1]
        LOG.debug(
            'Embedded registry listening on'
            ' https://127.0.0.1:%d' % port)
        return server

    def _stop_server(self, server):
        """Stop the embedded registry HTTP server."""
        server.shutdown()
        LOG.debug('Embedded registry stopped')

    def _digest_mapping_path(self):
        """Return the path for the digest mapping file.

        Stored alongside the layer cache as
        {cache_path}.digests.
        """
        if self.layer_cache is None:
            return None
        return self.layer_cache.path + '.digests'

    def _load_digest_mapping(self):
        """Load the Docker compressed digest -> DiffID
        mapping from disk.

        Returns:
            Dict mapping docker_compressed_hex to
            diffid_hex, or empty dict if not found.
        """
        path = self._digest_mapping_path()
        if path is None or not os.path.exists(path):
            return {}
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if data.get('version') == 1:
                LOG.debug(
                    'Loaded digest mapping with'
                    ' %d entries from %s'
                    % (len(data.get('mappings', {})),
                       path))
                return data.get('mappings', {})
        except (json.JSONDecodeError, OSError) as e:
            LOG.warning(
                'Could not load digest mapping'
                ' %s: %s' % (path, e))
        return {}

    def _save_digest_mapping(self, mappings):
        """Save the Docker compressed digest -> DiffID
        mapping to disk.

        Args:
            mappings: Dict mapping
                docker_compressed_hex to diffid_hex.
        """
        path = self._digest_mapping_path()
        if path is None:
            return

        data = {
            'version': 1,
            'mappings': mappings,
        }

        cache_dir = os.path.dirname(path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=cache_dir or '.', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, path)
                LOG.debug(
                    'Saved digest mapping with'
                    ' %d entries to %s'
                    % (len(mappings), path))
            except BaseException:
                os.unlink(tmp_path)
                raise
        except OSError as e:
            LOG.warning(
                'Could not save digest mapping:'
                ' %s' % e)

    def _build_skip_digests(self, fetch_callback):
        """Build the set of Docker compressed digests
        that should be skipped.

        Uses the digest mapping file to translate between
        Docker's compressed digests and DiffIDs, then
        checks the layer cache to see which DiffIDs are
        cached.

        The fetch_callback follows the convention from
        ImageInput.fetch(): it returns True if the layer
        should be fetched, False if it can be skipped.
        For RegistryWriter, False means the remote
        registry already has the blob. This method is
        called before the push starts, but fetch_callback
        is already usable at that point because it only
        queries the layer cache and remote registry
        (both initialized before fetch() is called).

        Returns:
            Tuple of (skip_digests_set,
                      digest_to_diffid_mapping).
        """
        if self.layer_cache is None:
            return set(), {}

        mapping = self._load_digest_mapping()
        if not mapping:
            return set(), mapping

        skip = set()
        for docker_hex, diffid_hex in mapping.items():
            # Check if this layer is cached
            entry = self.layer_cache.lookup(
                diffid_hex, self.filters_hash)
            if entry is not None:
                # Also check fetch_callback to ensure
                # the output agrees this layer can be
                # skipped
                if not fetch_callback(diffid_hex):
                    skip.add(docker_hex)
                    LOG.debug(
                        'Will skip blob %s...'
                        ' (cached as %s...)'
                        % (docker_hex[:12],
                           diffid_hex[:12]))

        if skip:
            LOG.info(
                'Skipping %d cached layer(s) via'
                ' HEAD optimization' % len(skip))

        return skip, mapping

    def fetch(self, fetch_callback=always_fetch,
              ordered=True):
        """Fetch image layers by pushing to an embedded
        registry.

        Starts a minimal HTTPS V2 registry on localhost
        (with an ephemeral self-signed certificate), uses
        Docker's push API to transfer layers, then yields
        ImageElements from the received data.

        Note: ordered=False provides no throughput
        benefit here since all blobs are already on disk
        when yielding begins. The only effect is setting
        layer_index on yielded elements so outputs can
        reconstruct manifest order.
        """
        if fetch_callback is None:
            fetch_callback = always_fetch

        ref = self._get_image_reference()
        LOG.info(
            'Fetching image %s via dockerpush from'
            ' daemon at %s' % (ref, self.socket_path))

        state = _RegistryState(temp_dir=self.temp_dir)

        # Build skip set from cache before starting
        skip_set, digest_mapping = \
            self._build_skip_digests(fetch_callback)
        state.skip_digests = skip_set

        server = self._start_server(state)
        port = server.server_address[1]
        push_repo = f'127.0.0.1:{port}/{self._image}'

        try:
            # Tag image for localhost push
            self._tag_image(push_repo, self._tag)

            try:
                # Push to embedded registry
                self._push_image(
                    push_repo, self._tag)

                # Wait for manifest
                if not state.manifest_event.wait(
                        timeout=MANIFEST_TIMEOUT):
                    raise Exception(
                        'Timed out waiting for'
                        ' manifest from Docker push'
                        ' (timeout=%ds)'
                        % MANIFEST_TIMEOUT)

                # Parse manifest
                manifest = json.loads(
                    state.manifest_data)

                # Detect manifest list (multi-arch)
                # which we don't support - Docker
                # should always push a single-platform
                # manifest, but check just in case.
                if 'manifests' in manifest:
                    raise Exception(
                        'Received a manifest list'
                        ' (multi-arch) instead of a'
                        ' single image manifest.'
                        ' This is unexpected for a'
                        ' Docker push. Please ensure'
                        ' the image is single-platform'
                        ' or use registry:// input'
                        ' which supports manifest'
                        ' list resolution.')

                LOG.info(
                    'Manifest received: %d layers'
                    % len(manifest.get('layers', [])))

                # Get config blob
                config_digest = manifest[
                    'config']['digest']
                config_hex = config_digest.split(
                    ':')[1]

                if config_hex not in state.blobs:
                    raise Exception(
                        'Config blob %s not received'
                        % config_digest)

                with open(
                        state.blobs[config_hex],
                        'rb') as f:
                    config_data = f.read()

                config_filename = (
                    '%s.json' % config_hex)
                LOG.debug(
                    'Config: %s (%d bytes)'
                    % (config_filename,
                       len(config_data)))

                yield constants.ImageElement(
                    constants.CONFIG_FILE,
                    config_filename,
                    io.BytesIO(config_data))

                # Parse config for DiffIDs (the
                # uncompressed layer digests). Note:
                # the image config JSON uses lowercase
                # keys (rootfs.diff_ids), not the
                # capitalized Docker inspect format
                # (RootFS.Layers).
                config_json = json.loads(config_data)
                raw_diff_ids = config_json.get(
                    'rootfs', {}).get('diff_ids', [])
                diff_ids = []
                for d in raw_diff_ids:
                    if d.startswith('sha256:'):
                        diff_ids.append(d[7:])
                    else:
                        diff_ids.append(d)

                layers = manifest.get('layers', [])
                if len(diff_ids) != len(layers):
                    raise Exception(
                        'DiffID count (%d) does not'
                        ' match layer count (%d)'
                        % (len(diff_ids), len(layers)))

                # Yield layers
                layers_fetched = 0
                layers_skipped = 0

                for layer_idx, (layer_meta, diff_id) \
                        in enumerate(
                            zip(layers, diff_ids)):
                    idx = (layer_idx
                           if not ordered else None)

                    if not fetch_callback(diff_id):
                        LOG.debug(
                            '[%d/%d] Skipping layer'
                            ' %s... (fetch callback)'
                            % (layer_idx + 1,
                               len(layers),
                               diff_id[:12]))
                        yield constants.ImageElement(
                            constants.IMAGE_LAYER,
                            diff_id, None,
                            layer_index=idx)
                        layers_skipped += 1
                        continue

                    # Get compressed blob
                    compressed_digest = layer_meta[
                        'digest']
                    compressed_hex = \
                        compressed_digest.split(
                            ':')[1]

                    # Update digest mapping for
                    # future cache runs
                    digest_mapping[
                        compressed_hex] = diff_id

                    # If Docker skipped upload (blob
                    # not in state.blobs), the layer
                    # was cached and HEAD returned 200
                    blob_missing = (
                        compressed_hex not in state.blobs)
                    was_skipped = (
                        compressed_hex
                        in state.skip_digests)

                    if blob_missing:
                        if was_skipped:
                            LOG.debug(
                                '[%d/%d] Skipping'
                                ' layer %s...'
                                ' (cached, HEAD skip)'
                                % (layer_idx + 1,
                                   len(layers),
                                   diff_id[:12]))
                            elem = constants.ImageElement(
                                constants.IMAGE_LAYER,
                                diff_id, None,
                                layer_index=idx)
                            yield elem
                            layers_skipped += 1
                            continue
                        raise Exception(
                            'Layer blob %s not'
                            ' received'
                            % compressed_digest)

                    blob_path = state.blobs[
                        compressed_hex]

                    # Detect compression type
                    media_type = layer_meta.get(
                        'mediaType')
                    comp_type = (
                        compression
                        .detect_compression_from_media_type(
                            media_type))
                    unknown = constants.COMPRESSION_UNKNOWN
                    if comp_type == unknown:
                        with open(
                                blob_path, 'rb') as f:
                            comp_type = (
                                compression
                                .detect_compression(f))

                    # Treat unknown as passthrough
                    if comp_type == unknown:
                        comp_type = (
                            constants.COMPRESSION_NONE)

                    # Stream decompress to temp file
                    # (constant memory regardless of
                    # layer size)
                    d = compression.StreamingDecompressor(
                        comp_type)
                    tf = tempfile.NamedTemporaryFile(
                        delete=False,
                        dir=self.temp_dir)
                    compressed_size = 0
                    try:
                        with open(
                                blob_path, 'rb') as f:
                            while True:
                                chunk = f.read(
                                    COPY_BUFSIZE)
                                if not chunk:
                                    break
                                compressed_size += (
                                    len(chunk))
                                tf.write(
                                    d.decompress(chunk))
                        remaining = d.flush()
                        if remaining:
                            tf.write(remaining)
                        decompressed_size = tf.tell()
                        tf.close()
                    except BaseException:
                        tf.close()
                        os.unlink(tf.name)
                        raise

                    # Delete compressed blob now that
                    # we have the decompressed copy
                    try:
                        os.unlink(blob_path)
                    except OSError:
                        pass
                    with state.lock:
                        del state.blobs[compressed_hex]

                    LOG.debug(
                        '[%d/%d] Layer %s...'
                        ' (%d bytes compressed,'
                        ' %d decompressed)'
                        % (layer_idx + 1,
                           len(layers),
                           diff_id[:12],
                           compressed_size,
                           decompressed_size))

                    try:
                        with open(
                                tf.name, 'rb') as fh:
                            yield constants.ImageElement(
                                constants.IMAGE_LAYER,
                                diff_id, fh,
                                layer_index=idx)
                    finally:
                        try:
                            os.unlink(tf.name)
                        except OSError:
                            pass
                    layers_fetched += 1

                # Save updated digest mapping
                self._save_digest_mapping(
                    digest_mapping)

                LOG.with_fields({
                    'fetched': layers_fetched,
                    'skipped': layers_skipped,
                }).info('Fetch complete')

            finally:
                # Untag the localhost image
                try:
                    self._untag_image(
                        push_repo, self._tag)
                except Exception:
                    LOG.warning(
                        'Failed to untag %s:%s'
                        % (push_repo, self._tag))

        finally:
            # Stop server and clean up temp files
            self._stop_server(server)
            for path in state.blobs.values():
                try:
                    os.unlink(path)
                except OSError:
                    pass
            # Clean up any abandoned uploads
            for upload in state.uploads.values():
                try:
                    os.unlink(upload['path'])
                except OSError:
                    pass
