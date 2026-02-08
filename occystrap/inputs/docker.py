# Fetch images from the local Docker or Podman daemon via the Docker Engine API.
# This communicates over a Unix domain socket (default: /var/run/docker.sock).
#
# Docker Engine API documentation:
# https://docs.docker.com/engine/api/
#
# Podman compatibility:
# Podman provides a Docker-compatible API via podman.socket. Use the --socket
# option to point to the Podman socket:
# - Rootful: /run/podman/podman.sock
# - Rootless: /run/user/<uid>/podman/podman.sock
# See: https://docs.podman.io/en/latest/markdown/podman-system-service.1.html
#
# The API returns images in the same format as 'docker save', which is the
# same format that inputs/tarfile.py reads.
#
# API Limitation: Unlike the registry API (inputs/registry.py) which can fetch
# individual layer blobs via GET /v2/<name>/blobs/<digest>, the Docker Engine
# API only provides /images/{name}/get which returns a complete tarball. There
# is no endpoint to fetch individual image components (config, layers)
# separately. This is a fundamental limitation of the Docker Engine API.
# See: https://github.com/moby/moby/issues/24851
#
# Hybrid Streaming:
# We use a hybrid approach to minimize disk usage:
# - Stream the tarball sequentially (mode='r|')
# - When layers arrive in expected order, yield them directly (no disk I/O)
# - When layers arrive out of order, buffer them to temp files for later
# - In the best case (layers in order), no temp files are used
# - In the worst case (all layers out of order), we buffer like before
#
# This is an improvement over the original approach which always buffered the
# entire tarball to a temp file before processing.

import io
import json
import logging
import os
import shutil
import tarfile
import tempfile

import requests_unixsocket

from occystrap import constants
from occystrap.inputs.base import ImageInput

COPY_BUFSIZE = 1024 * 1024  # 1MB chunks for streaming copies


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'


def always_fetch(digest):
    return True


class Image(ImageInput):
    def __init__(self, image, tag='latest', socket_path=DEFAULT_SOCKET_PATH,
                 temp_dir=None):
        self._image = image
        self._tag = tag
        self.socket_path = socket_path
        self.temp_dir = temp_dir
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
            self._session = requests_unixsocket.Session()
        return self._session

    def _socket_url(self, path):
        # requests_unixsocket uses http+unix:// scheme with URL-encoded path
        encoded_socket = self.socket_path.replace('/', '%2F')
        return 'http+unix://%s%s' % (encoded_socket, path)

    def _request(self, method, path, stream=False):
        session = self._get_session()
        url = self._socket_url(path)
        LOG.debug('Docker API request: %s %s' % (method, path))
        r = session.request(method, url, stream=stream)
        if r.status_code == 404:
            raise Exception('Image not found: %s:%s' % (self.image, self.tag))
        if r.status_code != 200:
            raise Exception('Docker API error %d: %s' % (r.status_code, r.text))
        return r

    def _get_image_reference(self):
        # Return the image reference in the format Docker expects
        return '%s:%s' % (self.image, self.tag)

    def inspect(self):
        """Get image metadata from the Docker daemon."""
        ref = self._get_image_reference()
        r = self._request('GET', '/images/%s/json' % ref)
        return r.json()

    def _buffer_to_tempfile(self, fileobj, name):
        """Buffer file content to a temporary file using chunked I/O.

        Copies data in chunks to avoid loading entire layers into RAM.
        Returns the path to the temp file.
        """
        tf = tempfile.NamedTemporaryFile(delete=False, dir=self.temp_dir)
        shutil.copyfileobj(fileobj, tf, length=COPY_BUFSIZE)
        tf.close()
        size = os.path.getsize(tf.name)
        LOG.info('Buffered %s to %s (%d bytes)'
                 % (name, tf.name, size))
        return tf.name

    def _open_buffered(self, buffered, filename):
        """Open a buffered temp file for reading and remove from tracking.

        Returns (file_handle, path). Caller must close the handle and
        delete the file when done.
        """
        path = buffered.pop(filename)
        fh = open(path, 'rb')
        return fh, path

    def _cleanup_file(self, fh, path):
        """Close a file handle and delete its backing file."""
        fh.close()
        try:
            os.unlink(path)
        except OSError:
            pass

    def fetch(self, fetch_callback=always_fetch):
        """Fetch image layers from the local Docker daemon.

        Uses hybrid streaming: streams layers directly when they arrive in
        expected order, buffers out-of-order layers to temp files. This
        minimizes disk usage when layers arrive in order (common case).
        """
        ref = self._get_image_reference()
        LOG.info('Fetching image %s from Docker daemon at %s'
                 % (ref, self.socket_path))

        # First verify the image exists
        try:
            self.inspect()
        except Exception as e:
            LOG.error('Failed to inspect image: %s' % str(e))
            raise

        # Stream the image tarball from Docker using sequential mode
        LOG.info('Streaming image tarball from Docker daemon')
        r = self._request('GET', '/images/%s/get' % ref, stream=True)

        # State tracking
        manifest = None
        config_filename = None
        expected_layers = None
        next_layer_idx = 0
        config_yielded = False

        # Stats for summary
        layers_streamed = 0
        layers_buffered = 0

        # Buffered files: filename -> temp file path
        # Used for files that arrive before we can process them
        buffered = {}

        def _layer_progress():
            """Return a progress string like '[3/10]'."""
            return '[%d/%d]' % (
                next_layer_idx + 1, len(expected_layers))

        def _yield_layer(layer_path, from_buffer=False):
            """Yield a layer from a buffered temp file.

            Opens the temp file, yields a seekable file handle, then
            cleans up. Data is never fully loaded into RAM.
            """
            layer_digest = os.path.dirname(layer_path)
            fh, path = self._open_buffered(buffered, layer_path)
            size = os.path.getsize(path)
            source = 'buffer' if from_buffer else 'temp'
            LOG.info(
                '%s Yielding layer %s from %s (%d bytes)'
                % (_layer_progress(), layer_digest, source, size))
            try:
                yield (constants.IMAGE_LAYER, layer_digest, fh)
            finally:
                self._cleanup_file(fh, path)

        # Helper to yield buffered layers that are now ready
        def flush_ready_layers():
            nonlocal next_layer_idx, layers_buffered
            while (next_layer_idx < len(expected_layers) and
                   expected_layers[next_layer_idx] in buffered):
                layer_path = expected_layers[next_layer_idx]
                layer_digest = os.path.dirname(layer_path)

                if not fetch_callback(layer_digest):
                    LOG.info(
                        '%s Skipping layer %s (fetch callback)'
                        % (_layer_progress(), layer_digest))
                    yield (constants.IMAGE_LAYER, layer_digest, None)
                else:
                    for elem in _yield_layer(
                            layer_path, from_buffer=True):
                        yield elem
                    layers_buffered += 1
                next_layer_idx += 1

        try:
            # Use streaming mode - files are read sequentially
            LOG.info('Opening tarball stream (sequential mode)')
            tar = tarfile.open(fileobj=r.raw, mode='r|')

            for member in tar:
                f = tar.extractfile(member)
                if f is None:
                    LOG.info(
                        'Skipping directory entry: %s' % member.name)
                    continue

                # Handle manifest.json (small, safe to read into RAM)
                if member.name == 'manifest.json':
                    manifest = json.loads(
                        f.read().decode('utf-8'))
                    config_filename = manifest[0]['Config']
                    expected_layers = manifest[0]['Layers']
                    LOG.info(
                        'Found manifest: config=%s, %d layers'
                        % (config_filename, len(expected_layers)))
                    if buffered:
                        LOG.info(
                            '%d file(s) were buffered before manifest'
                            % len(buffered))

                    # Check if config was already buffered
                    if config_filename in buffered:
                        LOG.info(
                            'Config was buffered before manifest,'
                            ' yielding from buffer')
                        fh, path = self._open_buffered(
                            buffered, config_filename)
                        config_data = fh.read()
                        self._cleanup_file(fh, path)
                        yield (constants.CONFIG_FILE,
                               config_filename,
                               io.BytesIO(config_data))
                        config_yielded = True

                        for elem in flush_ready_layers():
                            yield elem
                    continue

                # Before manifest, buffer everything (chunked I/O)
                if manifest is None:
                    LOG.info(
                        'Manifest not yet seen, buffering %s'
                        % member.name)
                    buffered[member.name] = \
                        self._buffer_to_tempfile(f, member.name)
                    continue

                # Handle config file (small, safe to read into RAM)
                if member.name == config_filename \
                        and not config_yielded:
                    config_data = f.read()
                    LOG.info('Found config file %s (%d bytes)'
                             % (config_filename, len(config_data)))
                    yield (constants.CONFIG_FILE, config_filename,
                           io.BytesIO(config_data))
                    config_yielded = True

                    for elem in flush_ready_layers():
                        yield elem
                    continue

                # Next expected layer - buffer to temp via chunked I/O
                # then yield seekable file handle (no full RAM load)
                if (config_yielded
                        and next_layer_idx < len(expected_layers)
                        and member.name
                        == expected_layers[next_layer_idx]):
                    layer_path = member.name
                    layer_digest = os.path.dirname(layer_path)

                    if not fetch_callback(layer_digest):
                        LOG.info(
                            '%s Skipping layer %s (fetch callback)'
                            % (_layer_progress(), layer_digest))
                        yield (constants.IMAGE_LAYER,
                               layer_digest, None)
                    else:
                        # Buffer to temp file using chunked I/O,
                        # then yield seekable file handle
                        temp_path = self._buffer_to_tempfile(
                            f, member.name)
                        fh = open(temp_path, 'rb')
                        size = os.path.getsize(temp_path)
                        LOG.info(
                            '%s Streaming layer %s via temp'
                            ' (%d bytes)'
                            % (_layer_progress(), layer_digest,
                               size))
                        try:
                            yield (constants.IMAGE_LAYER,
                                   layer_digest, fh)
                        finally:
                            self._cleanup_file(fh, temp_path)
                        layers_streamed += 1
                    next_layer_idx += 1

                    for elem in flush_ready_layers():
                        yield elem
                    continue

                # Out-of-order layer or unknown file - buffer it
                if member.isfile():
                    LOG.info(
                        'Out-of-order file %s, buffering to temp'
                        % member.name)
                    buffered[member.name] = \
                        self._buffer_to_tempfile(f, member.name)

            tar.close()
            LOG.info('Tarball stream complete')

            # Yield any remaining buffered items
            if not config_yielded and config_filename \
                    and config_filename in buffered:
                LOG.info(
                    'Yielding config from buffer'
                    ' (arrived after layers)')
                fh, path = self._open_buffered(
                    buffered, config_filename)
                config_data = fh.read()
                self._cleanup_file(fh, path)
                yield (constants.CONFIG_FILE, config_filename,
                       io.BytesIO(config_data))
                config_yielded = True

            # Yield remaining buffered layers in order
            if next_layer_idx < len(expected_layers):
                remaining = len(expected_layers) - next_layer_idx
                LOG.info(
                    '%d layer(s) remaining in buffer,'
                    ' yielding in order' % remaining)

            while next_layer_idx < len(expected_layers):
                layer_path = expected_layers[next_layer_idx]
                layer_digest = os.path.dirname(layer_path)

                if layer_path not in buffered:
                    raise Exception(
                        'Layer %s not found in tarball'
                        % layer_path)

                if not fetch_callback(layer_digest):
                    LOG.info(
                        '%s Skipping layer %s (fetch callback)'
                        % (_layer_progress(), layer_digest))
                    yield (constants.IMAGE_LAYER,
                           layer_digest, None)
                    buffered.pop(layer_path)
                else:
                    for elem in _yield_layer(
                            layer_path, from_buffer=True):
                        yield elem
                    layers_buffered += 1
                next_layer_idx += 1

        finally:
            # Clean up any remaining buffered files
            for path in buffered.values():
                if os.path.exists(path):
                    os.unlink(path)

        total = layers_streamed + layers_buffered
        LOG.info(
            'Done: %d layer(s) streamed directly,'
            ' %d from buffer (%d total)'
            % (layers_streamed, layers_buffered, total))
