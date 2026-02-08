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
import tarfile
import tempfile

import requests_unixsocket

from occystrap import constants
from occystrap.inputs.base import ImageInput


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'


def always_fetch(digest):
    return True


class Image(ImageInput):
    def __init__(self, image, tag='latest', socket_path=DEFAULT_SOCKET_PATH):
        self._image = image
        self._tag = tag
        self.socket_path = socket_path
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
        """Buffer file content to a temporary file and return the path."""
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(fileobj.read())
        tf.close()
        LOG.debug('Buffered %s to %s' % (name, tf.name))
        return tf.name

    def _read_and_delete_buffered(self, buffered, filename):
        """Read content from a buffered file and delete it."""
        with open(buffered[filename], 'rb') as f:
            data = f.read()
        os.unlink(buffered[filename])
        del buffered[filename]
        return data

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

        # Buffered files: filename -> temp file path
        # Used for files that arrive before we can process them
        buffered = {}

        # Helper to yield buffered layers that are now ready
        def flush_ready_layers():
            nonlocal next_layer_idx
            while (next_layer_idx < len(expected_layers) and
                   expected_layers[next_layer_idx] in buffered):
                layer_path = expected_layers[next_layer_idx]
                layer_digest = os.path.dirname(layer_path)

                if not fetch_callback(layer_digest):
                    LOG.info('Fetch callback says skip layer %s'
                             % layer_digest)
                    yield (constants.IMAGE_LAYER, layer_digest, None)
                else:
                    LOG.info('Yielding buffered layer %s' % layer_path)
                    layer_data = self._read_and_delete_buffered(
                        buffered, layer_path)
                    yield (constants.IMAGE_LAYER, layer_digest,
                           io.BytesIO(layer_data))
                next_layer_idx += 1

        try:
            # Use streaming mode - files are read sequentially as they appear
            tar = tarfile.open(fileobj=r.raw, mode='r|')

            for member in tar:
                f = tar.extractfile(member)
                if f is None:
                    continue  # Skip directories

                # Handle manifest.json
                if member.name == 'manifest.json':
                    manifest = json.loads(f.read().decode('utf-8'))
                    config_filename = manifest[0]['Config']
                    expected_layers = manifest[0]['Layers']
                    LOG.info('Found manifest: config=%s, %d layers'
                             % (config_filename, len(expected_layers)))

                    # Check if config was already buffered
                    if config_filename in buffered:
                        config_data = self._read_and_delete_buffered(
                            buffered, config_filename)
                        yield (constants.CONFIG_FILE, config_filename,
                               io.BytesIO(config_data))
                        config_yielded = True

                        # Yield any buffered layers that are now ready
                        for elem in flush_ready_layers():
                            yield elem
                    continue

                # Before manifest, buffer everything
                if manifest is None:
                    buffered[member.name] = self._buffer_to_tempfile(
                        f, member.name)
                    continue

                # Handle config file
                if member.name == config_filename and not config_yielded:
                    config_data = f.read()
                    yield (constants.CONFIG_FILE, config_filename,
                           io.BytesIO(config_data))
                    config_yielded = True

                    # Yield any buffered layers
                    for elem in flush_ready_layers():
                        yield elem
                    continue

                # Check if this is the next expected layer (optimistic case)
                if (config_yielded and
                        next_layer_idx < len(expected_layers) and
                        member.name == expected_layers[next_layer_idx]):
                    layer_path = member.name
                    layer_digest = os.path.dirname(layer_path)

                    if not fetch_callback(layer_digest):
                        LOG.info('Fetch callback says skip layer %s'
                                 % layer_digest)
                        yield (constants.IMAGE_LAYER, layer_digest, None)
                    else:
                        LOG.info('Streaming layer %s directly' % layer_path)
                        layer_data = f.read()
                        yield (constants.IMAGE_LAYER, layer_digest,
                               io.BytesIO(layer_data))
                    next_layer_idx += 1

                    # Yield any buffered layers that are now next
                    for elem in flush_ready_layers():
                        yield elem
                    continue

                # Out-of-order layer or unknown file - buffer it
                if member.isfile():
                    buffered[member.name] = self._buffer_to_tempfile(
                        f, member.name)

            tar.close()

            # After tarball is fully read, yield any remaining buffered items
            if not config_yielded and config_filename and \
                    config_filename in buffered:
                config_data = self._read_and_delete_buffered(
                    buffered, config_filename)
                yield (constants.CONFIG_FILE, config_filename,
                       io.BytesIO(config_data))
                config_yielded = True

            # Yield remaining buffered layers in order
            while next_layer_idx < len(expected_layers):
                layer_path = expected_layers[next_layer_idx]
                layer_digest = os.path.dirname(layer_path)

                if layer_path not in buffered:
                    raise Exception('Layer %s not found in tarball'
                                    % layer_path)

                if not fetch_callback(layer_digest):
                    LOG.info('Fetch callback says skip layer %s'
                             % layer_digest)
                    yield (constants.IMAGE_LAYER, layer_digest, None)
                else:
                    LOG.info('Yielding buffered layer %s' % layer_path)
                    layer_data = self._read_and_delete_buffered(
                        buffered, layer_path)
                    yield (constants.IMAGE_LAYER, layer_digest,
                           io.BytesIO(layer_data))
                next_layer_idx += 1

        finally:
            # Clean up any remaining buffered files
            for path in buffered.values():
                if os.path.exists(path):
                    os.unlink(path)

        LOG.info('Done')
