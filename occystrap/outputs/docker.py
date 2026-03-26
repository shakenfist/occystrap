# Load images into the local Docker or Podman daemon
# via the Docker Engine API. This communicates over a
# Unix domain socket (default: /var/run/docker.sock).
#
# Docker Engine API documentation:
# https://docs.docker.com/engine/api/
#
# Podman compatibility:
# Podman provides a Docker-compatible API via
# podman.socket. Use the socket option to point to
# the Podman socket:
# - Rootful: /run/podman/podman.sock
# - Rootless: /run/user/<uid>/podman/podman.sock
# See: https://docs.podman.io/en/latest/markdown/
#      podman-system-service.1.html
#
# The API accepts images in the same format as
# 'docker load', which is the v1.2 tarball format
# that outputs/tarfile.py creates.

import io
import json
import os
import tarfile
import tempfile
from urllib.parse import quote

import requests_unixsocket

from occystrap import constants
from occystrap.check import CheckResults
from occystrap.outputs.base import ImageOutput
from shakenfist_utilities import logs


LOG = logs.setup_console(__name__)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'


class DockerWriter(ImageOutput):
    """Loads images into the local Docker daemon.

    This output writer builds a v1.2 format tarball and
    loads it into the Docker daemon using the POST
    /images/load API endpoint. This is equivalent to
    running 'docker load'.

    Uses USTAR format for the outer tarball which
    contains only short paths (SHA256 hashes and small
    filenames), avoiding PAX extended headers.
    """

    def __init__(self, image, tag,
                 socket_path=DEFAULT_SOCKET_PATH,
                 temp_dir=None):
        """Initialize the Docker writer.

        Args:
            image: The image name.
            tag: The image tag.
            socket_path: Path to the Docker socket
                (default: /var/run/docker.sock).
            temp_dir: Directory for temporary files
                (default: system temp directory).
        """
        super().__init__(temp_dir=temp_dir)

        self.image = image
        self.tag = tag
        self.socket_path = socket_path
        self._session = None

        self._temp_file = tempfile.NamedTemporaryFile(
            delete=False, dir=self.temp_dir)
        self._image_tar = tarfile.open(
            fileobj=self._temp_file, mode='w',
            format=tarfile.USTAR_FORMAT)

        self._tar_manifest = [{
            'Layers': [],
            'RepoTags': [
                '%s:%s' % (self.image.split('/')[-1],
                           self.tag)]
        }]

        # For out-of-order layer delivery
        self._indexed_layers = []

    @property
    def requires_ordered_layers(self):
        return False

    def _get_session(self):
        if self._session is None:
            self._session = \
                requests_unixsocket.Session()
        return self._session

    def _socket_url(self, path):
        encoded_socket = self.socket_path.replace(
            '/', '%2F')
        return 'http+unix://%s%s' % (
            encoded_socket, path)

    def fetch_callback(self, digest):
        """Always fetch all layers."""
        return True

    def process_image_element(self, element):
        """Process an image element, adding it to the
        tarball."""
        if element.element_type == constants.CONFIG_FILE:
            LOG.debug('Adding config file to tarball')

            ti = tarfile.TarInfo(element.name)
            ti.size = len(element.data.read())
            element.data.seek(0)
            self._image_tar.addfile(ti, element.data)
            self._tar_manifest[0]['Config'] = \
                element.name
            self._track_element(
                element.element_type, ti.size)

        elif element.element_type == \
                constants.IMAGE_LAYER:
            LOG.debug('Adding layer to tarball')

            layer_name = element.name + '/layer.tar'
            ti = tarfile.TarInfo(layer_name)
            element.data.seek(0, os.SEEK_END)
            ti.size = element.data.tell()
            element.data.seek(0)
            self._image_tar.addfile(ti, element.data)
            self._track_element(
                element.element_type, ti.size)

            if element.layer_index is not None:
                self._indexed_layers.append(
                    (element.layer_index, layer_name))
            else:
                self._tar_manifest[0][
                    'Layers'].append(layer_name)

    def finalize(self):
        """Write manifest and load the image into
        Docker."""
        # Reconstruct layer order from indices
        if self._indexed_layers:
            self._indexed_layers.sort(
                key=lambda x: x[0])
            self._tar_manifest[0]['Layers'] = [
                name for _, name
                in self._indexed_layers]

        LOG.debug('Writing manifest to tarball')
        encoded_manifest = json.dumps(
            self._tar_manifest).encode('utf-8')
        ti = tarfile.TarInfo('manifest.json')
        ti.size = len(encoded_manifest)
        self._image_tar.addfile(
            ti, io.BytesIO(encoded_manifest))
        self._image_tar.close()

        temp_path = self._temp_file.name
        self._temp_file.close()

        try:
            LOG.info(
                'Loading image into Docker daemon'
                ' at %s' % self.socket_path)
            session = self._get_session()
            url = self._socket_url('/images/load')

            with open(temp_path, 'rb') as f:
                r = session.post(
                    url,
                    data=f,
                    headers={
                        'Content-Type':
                            'application/x-tar'})

            if r.status_code != 200:
                raise Exception(
                    'Docker API error %d: %s'
                    % (r.status_code, r.text))

            LOG.info(
                'Image loaded successfully: %s:%s'
                % (self.image, self.tag))
            self._log_summary()

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def verify(self, full=False):
        """Verify the image was loaded into Docker.

        Queries the Docker API to confirm the image
        exists with the expected tag and config digest.
        """
        results = CheckResults()

        # Derive expected image ID from config filename
        config_name = self._tar_manifest[0].get('Config')
        if config_name:
            expected_id = 'sha256:%s' % (
                config_name.replace('.json', ''))
        else:
            expected_id = None

        try:
            session = self._get_session()
            image_ref = '%s:%s' % (
                self.image.split('/')[-1], self.tag)
            url = self._socket_url(
                '/images/%s/json'
                % quote(image_ref, safe=''))
            r = session.get(url)

            if r.status_code == 404:
                results.error(
                    'verify.docker',
                    'Image %s not found in Docker'
                    ' daemon' % image_ref)
            elif r.status_code != 200:
                results.warning(
                    'verify.docker',
                    'Docker API returned %d for %s'
                    % (r.status_code, image_ref))
            elif expected_id:
                data = r.json()
                actual_id = data.get('Id', '')
                if actual_id != expected_id:
                    results.error(
                        'verify.docker',
                        'Image ID mismatch: expected'
                        ' %s, got %s'
                        % (expected_id, actual_id))
                else:
                    results.info(
                        'verify.ok',
                        'Image %s verified in Docker'
                        ' daemon' % image_ref)
        except (ConnectionError, OSError) as e:
            results.warning(
                'verify.docker',
                'Cannot connect to Docker daemon'
                ' at %s: %s'
                % (self.socket_path, e))

        return results
