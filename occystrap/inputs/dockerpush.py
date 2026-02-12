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
# Since Docker 1.3.2, the entire 127.0.0.0/8 range is implicitly
# trusted as insecure, so no daemon.json changes or TLS
# certificates are needed.
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
import logging
import os
import tempfile
import threading
import uuid
from urllib.parse import urlparse, parse_qs

import requests_unixsocket

from occystrap import compression
from occystrap import constants
from occystrap.inputs.base import ImageInput


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'

COPY_BUFSIZE = 1024 * 1024  # 1MB chunks


def always_fetch(digest):
    return True


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

    @property
    def state(self):
        return self.server.registry_state

    def log_message(self, format, *args):
        LOG.debug('Registry: %s' % (format % args))

    def _read_body(self):
        """Read the full request body."""
        length = int(
            self.headers.get('Content-Length', 0))
        if length > 0:
            return self.rfile.read(length)
        return b''

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
        layers), causing Docker to skip the upload.
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

        with self.state.lock:
            upload = self.state.uploads.get(upload_uuid)

        if not upload:
            self.send_response(404)
            self.end_headers()
            return

        # Read and write data
        data = self._read_body()
        with open(upload['path'], 'ab') as f:
            f.write(data)

        with self.state.lock:
            upload['offset'] += len(data)
            new_offset = upload['offset']

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

        # Read any remaining body data (monolithic upload
        # sends all data in the PUT)
        data = self._read_body()
        if data:
            with open(upload['path'], 'ab') as f:
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
        with open(upload['path'], 'rb') as f:
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
                os.unlink(upload['path'])
            except OSError:
                pass
            with self.state.lock:
                del self.state.uploads[upload_uuid]
            self.send_response(400)
            self.end_headers()
            return

        # Move blob to completed state
        blob_path = upload['path']
        with self.state.lock:
            self.state.blobs[expected_hex] = blob_path
            del self.state.uploads[upload_uuid]

        blob_size = os.path.getsize(blob_path)
        LOG.info(
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

        LOG.info(
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
    """Fetch images from Docker/Podman by pushing to an embedded
    registry.

    Instead of using Docker's /images/{name}/get API (which
    returns a single sequential tarball), this input starts a
    minimal HTTP server on localhost and uses Docker's push
    mechanism to transfer layers in parallel.
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
        path = '/images/%s/tag?repo=%s&tag=%s' % (
            ref, repo, tag)
        r = self._request('POST', path)
        if r.status_code == 404:
            raise Exception(
                'Image not found: %s' % ref)
        if r.status_code not in (200, 201):
            raise Exception(
                'Failed to tag image: %d %s'
                % (r.status_code, r.text))
        LOG.info('Tagged %s as %s:%s' % (ref, repo, tag))

    def _push_image(self, repo, tag):
        """Push an image to the embedded registry.

        Uses POST /images/{name}/push with streaming response.
        Consumes the stream and checks for errors.
        """
        push_ref = '%s:%s' % (repo, tag)
        path = '/images/%s/push' % push_ref
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

        LOG.info('Push started for %s' % push_ref)

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

        LOG.info('Push completed for %s' % push_ref)

    def _untag_image(self, repo, tag):
        """Remove a temporary tag from the Docker daemon.

        Uses DELETE /images/{name}?noprune=true to remove
        only the tag without deleting underlying layers.
        """
        ref = '%s:%s' % (repo, tag)
        path = '/images/%s?noprune=true' % ref
        r = self._request('DELETE', path)
        if r.status_code in (200, 404):
            LOG.info('Untagged %s' % ref)
        else:
            LOG.warning(
                'Failed to untag %s: %d %s'
                % (ref, r.status_code, r.text))

    def _start_server(self, state):
        """Start the embedded registry HTTP server.

        Binds to 127.0.0.1:0 (ephemeral port) and runs
        in a daemon thread.
        """
        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0),
            EmbeddedRegistryHandler)
        server.registry_state = state
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True)
        thread.start()
        port = server.server_address[1]
        LOG.info(
            'Embedded registry listening on'
            ' 127.0.0.1:%d' % port)
        return server

    def _stop_server(self, server):
        """Stop the embedded registry HTTP server."""
        server.shutdown()
        LOG.info('Embedded registry stopped')

    def _digest_mapping_path(self):
        """Return the path for the digest mapping file.

        Stored alongside the layer cache as
        {cache_path}.digests.
        """
        if self.layer_cache is None:
            return None
        return self.layer_cache._path + '.digests'

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
                LOG.info(
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
                LOG.info(
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

        Starts a minimal V2 registry on localhost, uses
        Docker's push API to transfer layers, then yields
        ImageElements from the received data.
        """
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
        push_repo = 'localhost:%d/%s' % (
            port, self._image)

        try:
            # Tag image for localhost push
            self._tag_image(push_repo, self._tag)

            try:
                # Push to embedded registry
                self._push_image(
                    push_repo, self._tag)

                # Wait for manifest
                if not state.manifest_event.wait(
                        timeout=300):
                    raise Exception(
                        'Timed out waiting for'
                        ' manifest from Docker push')

                # Parse manifest
                manifest = json.loads(
                    state.manifest_data)
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
                LOG.info(
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
                        LOG.info(
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
                    if compressed_hex \
                            not in state.blobs:
                        if compressed_hex \
                                in state.skip_digests:
                            LOG.info(
                                '[%d/%d] Skipping'
                                ' layer %s...'
                                ' (cached, HEAD'
                                ' skip)'
                                % (layer_idx + 1,
                                   len(layers),
                                   diff_id[:12]))
                            yield constants\
                                .ImageElement(
                                    constants
                                    .IMAGE_LAYER,
                                    diff_id, None,
                                    layer_index=idx)
                            layers_skipped += 1
                            continue
                        raise Exception(
                            'Layer blob %s not'
                            ' received'
                            % compressed_digest)

                    blob_path = state.blobs[
                        compressed_hex]

                    # Detect and decompress
                    media_type = layer_meta.get(
                        'mediaType')
                    comp_type = compression\
                        .detect_compression_from_media_type(
                            media_type)
                    if comp_type == constants\
                            .COMPRESSION_UNKNOWN:
                        with open(
                                blob_path, 'rb') as f:
                            comp_type = compression\
                                .detect_compression(f)

                    with open(blob_path, 'rb') as f:
                        blob_data = f.read()

                    if comp_type in (
                            constants.COMPRESSION_GZIP,
                            constants
                            .COMPRESSION_ZSTD):
                        decompressed = compression\
                            .decompress_data(
                                blob_data, comp_type)
                    else:
                        decompressed = blob_data

                    LOG.info(
                        '[%d/%d] Layer %s...'
                        ' (%d bytes compressed,'
                        ' %d decompressed)'
                        % (layer_idx + 1,
                           len(layers),
                           diff_id[:12],
                           len(blob_data),
                           len(decompressed)))

                    yield constants.ImageElement(
                        constants.IMAGE_LAYER,
                        diff_id,
                        io.BytesIO(decompressed),
                        layer_index=idx)
                    layers_fetched += 1

                # Save updated digest mapping
                self._save_digest_mapping(
                    digest_mapping)

                LOG.info(
                    'Done: %d layer(s) fetched,'
                    ' %d skipped'
                    % (layers_fetched,
                       layers_skipped))

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
