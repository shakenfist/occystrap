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
# same format that inputs/tarfile.py reads. We stream the tarball and parse
# it on the fly.
#
# API Limitation: Unlike the registry API (inputs/registry.py) which can fetch
# individual layer blobs via GET /v2/<name>/blobs/<digest>, the Docker Engine
# API only provides /images/{name}/get which returns a complete tarball. There
# is no endpoint to fetch individual image components (config, layers)
# separately. This is a fundamental limitation of the Docker Engine API.
# See: https://github.com/moby/moby/issues/24851
#
# The tarball streaming approach used here is the official supported method
# and matches what 'docker save' does internally.

import io
import json
import logging
import os
import tarfile

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

    def fetch(self, fetch_callback=always_fetch):
        """Fetch image layers from the local Docker daemon.

        This uses the Docker Engine API to export the image as a tarball
        (equivalent to 'docker save') and streams/parses it on the fly.
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

        # Stream the image tarball from Docker
        LOG.info('Streaming image tarball from Docker daemon')
        r = self._request('GET', '/images/%s/get' % ref, stream=True)

        # We need to buffer the stream into a file-like object for tarfile
        # because tarfile needs to seek. We use a temporary file approach
        # similar to the registry input.
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            LOG.info('Buffering image to temporary file %s' % tf.name)
            for chunk in r.iter_content(8192):
                tf.write(chunk)
            temp_path = tf.name

        try:
            # Parse the tarball (same format as 'docker save')
            with tarfile.open(temp_path, 'r') as tar:
                # Read manifest.json
                manifest_member = tar.getmember('manifest.json')
                manifest_file = tar.extractfile(manifest_member)
                manifest = json.loads(manifest_file.read().decode('utf-8'))

                # Yield config file
                config_filename = manifest[0]['Config']
                LOG.info('Reading config file %s' % config_filename)
                config_member = tar.getmember(config_filename)
                config_file = tar.extractfile(config_member)
                config_data = config_file.read()
                yield (constants.CONFIG_FILE, config_filename,
                       io.BytesIO(config_data))

                # Yield each layer
                layers = manifest[0]['Layers']
                LOG.info('There are %d image layers' % len(layers))

                for layer_path in layers:
                    # Layer path is like "abc123/layer.tar"
                    layer_digest = os.path.dirname(layer_path)
                    if not fetch_callback(layer_digest):
                        LOG.info('Fetch callback says skip layer %s'
                                 % layer_digest)
                        yield (constants.IMAGE_LAYER, layer_digest, None)
                        continue

                    LOG.info('Reading layer %s' % layer_path)
                    layer_member = tar.getmember(layer_path)
                    layer_file = tar.extractfile(layer_member)
                    layer_data = layer_file.read()
                    yield (constants.IMAGE_LAYER, layer_digest,
                           io.BytesIO(layer_data))

        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)

        LOG.info('Done')
