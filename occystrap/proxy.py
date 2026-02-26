# Filtering registry proxy for occystrap.
#
# Runs a persistent Docker Registry V2 server on localhost.
# When a client pushes an image, the proxy receives blobs and
# manifest via the standard V2 push-path API, applies the
# configured filter chain, and forwards the result to a
# downstream registry.
#
# Multiple images can be processed concurrently: each manifest
# PUT blocks its own HTTP response until processing completes,
# but a configurable semaphore allows multiple manifests to be
# processed in parallel. Blob reference counting prevents
# shared blobs from being deleted while still in use.
#
# Docker Registry HTTP API V2:
# https://distribution.github.io/distribution/spec/api/

import hashlib
import http.server
import io
import json
import os
import signal
import socket
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse, parse_qs

from shakenfist_utilities import logs

from occystrap import compression
from occystrap import constants
from occystrap.inputs.base import ImageInput
from occystrap.layer_cache import LayerCache
from occystrap.pipeline import PipelineBuilder


LOG = logs.setup_console(__name__)

COPY_BUFSIZE = 1024 * 1024  # 1MB chunks


DEFAULT_MAX_CONCURRENT = 4


class _ProxyState:
    """Shared state between the proxy HTTP handler
    threads and the main process.

    All mutations to uploads, blobs, blob_refcounts,
    active_processing, and statistics must be protected
    by the lock.
    """

    def __init__(self, temp_dir=None,
                 downstream_uri=None,
                 filter_strs=None,
                 layer_cache=None,
                 ctx=None,
                 max_concurrent=DEFAULT_MAX_CONCURRENT):
        self.temp_dir = temp_dir
        self.downstream_uri = downstream_uri
        self.filter_strs = filter_strs or []
        self.layer_cache = layer_cache
        self.ctx = ctx

        # In-progress uploads: uuid -> {path, offset}
        self.uploads = {}
        # Completed blobs: digest_hex -> temp file path
        self.blobs = {}
        # Blob reference counts: digest_hex -> int.
        # Incremented when a manifest references a blob,
        # decremented when processing completes. Blobs
        # are only deleted when refcount reaches 0.
        self.blob_refcounts = {}
        self.lock = threading.Lock()

        # Semaphore limiting concurrent manifest
        # processing (backpressure).
        self.processing_semaphore = (
            threading.Semaphore(max_concurrent))

        # Count of in-flight manifest processing
        # operations (for graceful shutdown).
        self.active_processing = 0

        # Statistics
        self.images_processed = 0
        self.images_failed = 0


class _ProxyInput(ImageInput):
    """Synthetic input that yields ImageElements from
    already-received blobs.

    Used by the proxy to feed received push data into
    the standard occystrap pipeline.
    """

    def __init__(self, image, tag, manifest_data,
                 blobs, temp_dir=None):
        self._image = image
        self._tag = tag
        self._manifest_data = manifest_data
        self._blobs = blobs
        self._temp_dir = temp_dir

    @property
    def image(self):
        return self._image

    @property
    def tag(self):
        return self._tag

    def fetch(self, fetch_callback=None, ordered=True):
        """Yield ImageElements from received blobs."""
        from occystrap.inputs.base import always_fetch
        if fetch_callback is None:
            fetch_callback = always_fetch

        manifest = json.loads(self._manifest_data)

        # Get config blob
        config_digest = manifest['config']['digest']
        config_hex = config_digest.split(':')[1]

        if config_hex not in self._blobs:
            raise Exception(
                'Config blob %s not received'
                % config_digest)

        with open(self._blobs[config_hex], 'rb') as f:
            config_data = f.read()

        config_filename = '%s.json' % config_hex
        yield constants.ImageElement(
            constants.CONFIG_FILE,
            config_filename,
            io.BytesIO(config_data))

        # Parse config for DiffIDs
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
                'DiffID count (%d) does not match '
                'layer count (%d)'
                % (len(diff_ids), len(layers)))

        for layer_idx, (layer_meta, diff_id) \
                in enumerate(zip(layers, diff_ids)):
            idx = (layer_idx
                   if not ordered else None)

            if not fetch_callback(diff_id):
                LOG.debug(
                    '[%d/%d] Skipping layer %s...'
                    ' (fetch callback)',
                    layer_idx + 1, len(layers),
                    diff_id[:12])
                yield constants.ImageElement(
                    constants.IMAGE_LAYER,
                    diff_id, None,
                    layer_index=idx)
                continue

            # Get compressed blob
            compressed_digest = layer_meta['digest']
            compressed_hex = \
                compressed_digest.split(':')[1]

            if compressed_hex not in self._blobs:
                raise Exception(
                    'Layer blob %s not received'
                    % compressed_digest)

            blob_path = self._blobs[compressed_hex]

            # Detect compression type
            media_type = layer_meta.get('mediaType')
            comp_type = (
                compression
                .detect_compression_from_media_type(
                    media_type))
            unknown = constants.COMPRESSION_UNKNOWN
            if comp_type == unknown:
                with open(blob_path, 'rb') as f:
                    comp_type = (
                        compression
                        .detect_compression(f))
            if comp_type == unknown:
                comp_type = constants.COMPRESSION_NONE

            # Stream decompress to temp file
            d = compression.StreamingDecompressor(
                comp_type)
            tf = tempfile.NamedTemporaryFile(
                delete=False, dir=self._temp_dir)
            try:
                with open(blob_path, 'rb') as f:
                    while True:
                        chunk = f.read(COPY_BUFSIZE)
                        if not chunk:
                            break
                        tf.write(d.decompress(chunk))
                remaining = d.flush()
                if remaining:
                    tf.write(remaining)
                tf.close()
            except BaseException:
                tf.close()
                os.unlink(tf.name)
                raise

            LOG.debug(
                '[%d/%d] Layer %s... decompressed',
                layer_idx + 1, len(layers),
                diff_id[:12])

            try:
                with open(tf.name, 'rb') as fh:
                    yield constants.ImageElement(
                        constants.IMAGE_LAYER,
                        diff_id, fh,
                        layer_index=idx)
            finally:
                try:
                    os.unlink(tf.name)
                except OSError:
                    pass


class ProxyRegistryHandler(
        http.server.BaseHTTPRequestHandler):
    """HTTP handler implementing the Docker Registry V2
    push-path endpoints for the proxy.

    Blob receipt follows the standard V2 flow:
    GET /v2/, HEAD blobs, POST/PATCH/PUT uploads.

    The manifest PUT blocks while the image is processed
    through the filter pipeline and pushed to the
    downstream registry.
    """

    timeout = 300

    @property
    def state(self):
        return self.server.proxy_state

    def log_message(self, format, *args):
        LOG.debug('Proxy: %s' % (format % args))

    def _read_body(self):
        """Read the full request body."""
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
            chunk_header = line.strip().split(b';')[0]
            if not chunk_header:
                break
            chunk_size = int(chunk_header, 16)
            if chunk_size == 0:
                self.rfile.readline()
                break
            body.write(self.rfile.read(chunk_size))
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

        Returns 200 if the blob has already been
        uploaded in this session. Returns 404 otherwise
        to force Docker to upload it.
        """
        path, _ = self._parse_path()

        if '/blobs/sha256:' in path:
            digest_hex = path.split(
                '/blobs/sha256:')[1]

            with self.state.lock:
                blob_path = self.state.blobs.get(
                    digest_hex)
            if blob_path:
                blob_size = os.path.getsize(blob_path)
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
        """Handle POST /v2/{name}/blobs/uploads/."""
        path, _ = self._parse_path()

        if '/blobs/uploads' not in path:
            self.send_response(404)
            self.end_headers()
            return

        upload_uuid = str(uuid.uuid4())

        tf = tempfile.NamedTemporaryFile(
            delete=False, dir=self.state.temp_dir)
        tf_path = tf.name
        tf.close()

        with self.state.lock:
            self.state.uploads[upload_uuid] = {
                'path': tf_path,
                'offset': 0,
            }

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
        """
        path, _ = self._parse_path()

        parts = path.split('/blobs/uploads/')
        if len(parts) < 2:
            self.send_response(404)
            self.end_headers()
            return

        upload_uuid = parts[1].strip('/')

        data = self._read_body()

        with self.state.lock:
            upload = self.state.uploads.get(
                upload_uuid)
            if not upload:
                self.send_response(404)
                self.end_headers()
                return
            upload_path = upload['path']

        with open(upload_path, 'ab') as f:
            f.write(data)

        with self.state.lock:
            upload = self.state.uploads.get(
                upload_uuid)
            if upload:
                upload['offset'] += len(data)
                new_offset = upload['offset']
            else:
                new_offset = len(data)

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
        """Handle PUT for blob completion and manifest."""
        path, params = self._parse_path()

        if '/manifests/' in path:
            self._handle_manifest_put(path)
            return

        if '/blobs/uploads/' not in path:
            self.send_response(404)
            self.end_headers()
            return

        self._handle_blob_put(path, params)

    def _handle_blob_put(self, path, params):
        """Complete a blob upload with digest
        verification."""
        parts = path.split('/blobs/uploads/')
        if len(parts) < 2:
            self.send_response(404)
            self.end_headers()
            return

        upload_uuid = parts[1].strip('/')

        with self.state.lock:
            upload = self.state.uploads.get(
                upload_uuid)
            if not upload:
                self.send_response(404)
                self.end_headers()
                return
            upload_path = upload['path']

        data = self._read_body()
        if data:
            with open(upload_path, 'ab') as f:
                f.write(data)

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
                ' got %s',
                expected_hex, actual_hex)
            try:
                os.unlink(upload_path)
            except OSError:
                pass
            with self.state.lock:
                del self.state.uploads[upload_uuid]
            self.send_response(400)
            self.end_headers()
            return

        with self.state.lock:
            self.state.blobs[expected_hex] = \
                upload_path
            del self.state.uploads[upload_uuid]

        blob_size = os.path.getsize(upload_path)
        LOG.debug(
            'Received blob sha256:%s... (%d bytes)',
            expected_hex[:12], blob_size)

        self.send_response(201)
        self.send_header(
            'Docker-Content-Digest',
            'sha256:%s' % expected_hex)
        self.send_header('Content-Length', '0')
        self.send_header(
            'Location',
            '/v2/_/blobs/sha256:%s' % expected_hex)
        self.end_headers()

    def _handle_manifest_put(self, path):
        """Receive manifest, process image through
        pipeline, push to downstream registry.

        Blocks the HTTP response until processing is
        complete. Returns 201 on success, 500 on
        failure. Multiple manifests can be processed
        concurrently, limited by the processing
        semaphore.
        """
        data = self._read_body()

        # Compute manifest digest
        h = hashlib.sha256()
        h.update(data)
        manifest_digest = h.hexdigest()

        # Extract repo name and tag from path
        # Path: /v2/{name}/manifests/{tag}
        repo_name, tag = \
            self._parse_manifest_path(path)

        LOG.info(
            'Received manifest for %s:%s'
            ' (%d bytes, sha256:%s...)',
            repo_name, tag, len(data),
            manifest_digest[:12])

        # Check for manifest list (unsupported)
        manifest = json.loads(data)
        if 'manifests' in manifest:
            LOG.error(
                'Manifest list (multi-arch) not'
                ' supported by proxy')
            self.send_response(400)
            self.end_headers()
            return

        # Snapshot the blobs this manifest references
        referenced_blobs = set()
        for layer in manifest.get('layers', []):
            digest = layer['digest']
            referenced_blobs.add(
                digest.split(':')[1])
        config_digest = manifest['config']['digest']
        referenced_blobs.add(
            config_digest.split(':')[1])

        # Under lock: increment refcounts, snapshot
        # blob paths, and mark processing as active.
        # Refcounts must be incremented before
        # acquiring the semaphore so blobs are
        # protected even while waiting for a slot.
        with self.state.lock:
            for hex_digest in referenced_blobs:
                rc = self.state.blob_refcounts
                rc[hex_digest] = (
                    rc.get(hex_digest, 0) + 1)
            blobs_snapshot = {
                k: v
                for k, v in self.state.blobs.items()
                if k in referenced_blobs
            }
            self.state.active_processing += 1

        # Acquire semaphore (blocks if too many
        # images are processing concurrently)
        self.state.processing_semaphore.acquire()

        success = False
        try:
            self._process_image(
                repo_name, tag, data,
                blobs_snapshot)
            success = True

        except Exception as e:
            with self.state.lock:
                self.state.images_failed += 1

            LOG.error(
                'Failed to process %s:%s: %s',
                repo_name, tag, e)

        finally:
            self.state.processing_semaphore.release()

            # Decrement refcounts and clean up blobs
            # that are no longer referenced by any
            # in-flight manifest processing.
            with self.state.lock:
                for hex_digest in referenced_blobs:
                    rc = self.state.blob_refcounts
                    count = rc.get(hex_digest, 1) - 1
                    if count <= 0:
                        rc.pop(hex_digest, None)
                        blob_path = (
                            self.state.blobs.pop(
                                hex_digest, None))
                        if blob_path:
                            try:
                                os.unlink(blob_path)
                            except OSError:
                                pass
                    else:
                        rc[hex_digest] = count

                self.state.active_processing -= 1

        # Send HTTP response after cleanup so state
        # is consistent when the client receives it.
        if success:
            with self.state.lock:
                self.state.images_processed += 1

            LOG.info(
                'Successfully processed %s:%s',
                repo_name, tag)

            self.send_response(201)
            self.send_header(
                'Docker-Content-Digest',
                'sha256:%s' % manifest_digest)
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.send_response(500)
            self.end_headers()

    def _parse_manifest_path(self, path):
        """Extract repository name and tag from
        manifest path.

        Path format: /v2/{name}/manifests/{tag}
        where {name} can contain slashes.
        """
        # Strip /v2/ prefix
        rest = path
        if rest.startswith('/v2/'):
            rest = rest[4:]

        # Split on /manifests/
        parts = rest.split('/manifests/')
        if len(parts) != 2:
            return ('unknown', 'unknown')

        return (parts[0], parts[1])

    def _process_image(self, repo_name, tag,
                       manifest_data, blobs):
        """Process one image through the filter
        pipeline and push to downstream registry.

        Args:
            repo_name: Repository name from the push
                (e.g., 'kolla/nova-api').
            tag: Image tag (e.g., 'latest').
            manifest_data: Raw manifest JSON bytes.
            blobs: Dict of digest_hex -> temp file
                path for blobs referenced by this
                manifest.
        """
        # Build downstream URI with the repo name
        # from the push, preserving the path.
        downstream = self.state.downstream_uri
        dest_uri = 'registry://%s/%s:%s' % (
            downstream, repo_name, tag)

        LOG.info(
            'Processing %s:%s -> %s',
            repo_name, tag, dest_uri)

        # Create synthetic input
        proxy_input = _ProxyInput(
            repo_name, tag, manifest_data,
            blobs, temp_dir=self.state.temp_dir)

        # Build output pipeline (filters + output)
        builder = PipelineBuilder(self.state.ctx)

        # We need to build the output and filters
        # manually since we have a custom input.
        from occystrap import uri
        dest_spec = uri.parse_uri(dest_uri)
        filter_specs = [
            uri.parse_filter(f)
            for f in self.state.filter_strs]

        # Set up layer cache
        layer_cache = self.state.layer_cache
        filters_hash = 'none'
        if layer_cache is not None:
            compression_type = dest_spec.options.get(
                'compression',
                builder._get_ctx('COMPRESSION'))
            filters_hash = (
                PipelineBuilder
                ._compute_filters_hash(
                    self.state.filter_strs,
                    compression_type))

        output = builder.build_output(
            dest_spec, repo_name, tag,
            layer_cache=layer_cache,
            filters_hash=filters_hash)

        # Wrap with filters (reverse order so first
        # filter is outermost)
        for filter_spec in reversed(filter_specs):
            output = builder.build_filter(
                filter_spec, output,
                image=repo_name, tag=tag)

        # Run the pipeline
        ordered = output.requires_ordered_layers
        for element in proxy_input.fetch(
                fetch_callback=output.fetch_callback,
                ordered=ordered):
            output.process_image_element(element)
        output.finalize()


class _KeepAliveHTTPServer(
        http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with SO_KEEPALIVE enabled
    on accepted connections.

    Prevents TCP connections from being dropped during
    long manifest processing (the manifest PUT blocks
    while the image is filtered and pushed downstream).
    """

    def get_request(self):
        conn, addr = super().get_request()
        conn.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_KEEPALIVE, 1)
        return conn, addr


def run_proxy(listen_host, listen_port,
              downstream_uri, filter_strs=None,
              layer_cache_path=None, ctx=None,
              max_concurrent=DEFAULT_MAX_CONCURRENT):
    """Start the proxy registry server.

    Runs until interrupted by SIGINT or SIGTERM.

    Args:
        listen_host: Host to bind to (e.g.,
            '127.0.0.1').
        listen_port: Port to listen on.
        downstream_uri: Downstream registry host
            (e.g., 'ghcr.io').
        filter_strs: List of filter specification
            strings.
        layer_cache_path: Path to layer cache JSON
            file (optional).
        ctx: Click context object.
        max_concurrent: Maximum number of images to
            process concurrently (default 4).
    """
    layer_cache = None
    if layer_cache_path:
        layer_cache = LayerCache(layer_cache_path)

    state = _ProxyState(
        temp_dir=(ctx.obj.get('TEMP_DIR')
                  if ctx and ctx.obj else None),
        downstream_uri=downstream_uri,
        filter_strs=filter_strs or [],
        layer_cache=layer_cache,
        ctx=ctx,
        max_concurrent=max_concurrent)

    server = _KeepAliveHTTPServer(
        (listen_host, listen_port),
        ProxyRegistryHandler)
    server.proxy_state = state

    addr = server.server_address
    LOG.info(
        'Proxy registry listening on %s:%d',
        addr[0], addr[1])
    LOG.debug(
        'Downstream: %s', downstream_uri)
    if filter_strs:
        LOG.debug(
            'Filters: %s',
            ', '.join(filter_strs))
    if layer_cache:
        LOG.debug(
            'Layer cache: %s (%d entries)',
            layer_cache_path, len(layer_cache))

    # Handle graceful shutdown
    shutdown_event = threading.Event()

    def _signal_handler(signum, frame):
        LOG.info(
            'Received signal %d, shutting down...',
            signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True)
    server_thread.start()

    LOG.info('Ready to receive pushes. Press Ctrl+C'
             ' to stop.')

    # Block until shutdown signal
    shutdown_event.wait()

    LOG.debug('Shutting down server...')
    server.shutdown()

    # Wait for in-flight image processing to finish
    # (up to 5 minutes).
    deadline = time.time() + 300
    with state.lock:
        active = state.active_processing
    if active > 0:
        LOG.info(
            'Waiting for %d in-flight image(s) to'
            ' finish...', active)
    while active > 0 and time.time() < deadline:
        time.sleep(1)
        with state.lock:
            active = state.active_processing
    if active > 0:
        LOG.warning(
            '%d image(s) still processing after'
            ' shutdown timeout', active)

    # Save layer cache on exit
    if layer_cache:
        layer_cache.save()

    LOG.info(
        'Proxy stopped. Processed %d images'
        ' (%d failed).',
        state.images_processed,
        state.images_failed)

    # Clean up any remaining blobs
    with state.lock:
        for blob_path in state.blobs.values():
            try:
                os.unlink(blob_path)
            except OSError:
                pass
        for upload in state.uploads.values():
            try:
                os.unlink(upload['path'])
            except OSError:
                pass
