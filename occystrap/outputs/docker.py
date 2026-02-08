# Load images into the local Docker or Podman daemon via the Docker Engine API.
# This communicates over a Unix domain socket (default: /var/run/docker.sock).
#
# Docker Engine API documentation:
# https://docs.docker.com/engine/api/
#
# Podman compatibility:
# Podman provides a Docker-compatible API via podman.socket. Use the socket
# option to point to the Podman socket:
# - Rootful: /run/podman/podman.sock
# - Rootless: /run/user/<uid>/podman/podman.sock
# See: https://docs.podman.io/en/latest/markdown/podman-system-service.1.html
#
# The API accepts images in the same format as 'docker load', which is the
# v1.2 tarball format that outputs/tarfile.py creates.

import io
import json
import logging
import os
import tarfile
import tempfile

import requests_unixsocket

from occystrap import constants
from occystrap.outputs.base import ImageOutput


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'


class DockerWriter(ImageOutput):
    """Loads images into the local Docker daemon.

    This output writer builds a v1.2 format tarball and loads it into
    the Docker daemon using the POST /images/load API endpoint. This is
    equivalent to running 'docker load'.

    Uses USTAR format for the outer tarball which contains only short paths
    (SHA256 hashes and small filenames), avoiding PAX extended headers.
    """

    def __init__(self, image, tag, socket_path=DEFAULT_SOCKET_PATH,
                 temp_dir=None):
        """Initialize the Docker writer.

        Args:
            image: The image name.
            tag: The image tag.
            socket_path: Path to the Docker socket
                (default: /var/run/docker.sock).
            temp_dir: Directory for temporary files (default:
                system temp directory).
        """
        super().__init__(temp_dir=temp_dir)

        self.image = image
        self.tag = tag
        self.socket_path = socket_path
        self._session = None

        self._temp_file = tempfile.NamedTemporaryFile(
            delete=False, dir=self.temp_dir)
        self._image_tar = tarfile.open(fileobj=self._temp_file, mode='w',
                                       format=tarfile.USTAR_FORMAT)

        self._tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]

    def _get_session(self):
        if self._session is None:
            self._session = requests_unixsocket.Session()
        return self._session

    def _socket_url(self, path):
        encoded_socket = self.socket_path.replace('/', '%2F')
        return 'http+unix://%s%s' % (encoded_socket, path)

    def fetch_callback(self, digest):
        """Always fetch all layers."""
        return True

    def process_image_element(self, element_type, name, data):
        """Process an image element, adding it to the tarball."""
        if element_type == constants.CONFIG_FILE:
            LOG.info('Adding config file to tarball')

            ti = tarfile.TarInfo(name)
            ti.size = len(data.read())
            data.seek(0)
            self._image_tar.addfile(ti, data)
            self._tar_manifest[0]['Config'] = name
            self._track_element(element_type, ti.size)

        elif element_type == constants.IMAGE_LAYER:
            LOG.info('Adding layer to tarball')

            name += '/layer.tar'
            ti = tarfile.TarInfo(name)
            data.seek(0, os.SEEK_END)
            ti.size = data.tell()
            data.seek(0)
            self._image_tar.addfile(ti, data)
            self._tar_manifest[0]['Layers'].append(name)
            self._track_element(element_type, ti.size)

    def finalize(self):
        """Write manifest and load the image into Docker."""
        LOG.info('Writing manifest to tarball')
        encoded_manifest = json.dumps(self._tar_manifest).encode('utf-8')
        ti = tarfile.TarInfo('manifest.json')
        ti.size = len(encoded_manifest)
        self._image_tar.addfile(ti, io.BytesIO(encoded_manifest))
        self._image_tar.close()

        temp_path = self._temp_file.name
        self._temp_file.close()

        try:
            LOG.info('Loading image into Docker daemon at %s' % self.socket_path)
            session = self._get_session()
            url = self._socket_url('/images/load')

            with open(temp_path, 'rb') as f:
                r = session.post(
                    url,
                    data=f,
                    headers={'Content-Type': 'application/x-tar'}
                )

            if r.status_code != 200:
                raise Exception(
                    'Docker API error %d: %s' % (r.status_code, r.text))

            LOG.info('Image loaded successfully: %s:%s'
                     % (self.image, self.tag))
            self._log_summary()

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
