# Push images to a Docker/OCI registry via the Docker Registry HTTP API V2.
#
# Docker Registry API documentation:
# https://docs.docker.com/registry/spec/api/
#
# OCI Distribution Spec:
# https://github.com/opencontainers/distribution-spec/blob/main/spec.md
#
# The push process:
# 1. For each layer blob:
#    a. Check if blob exists: HEAD /v2/<name>/blobs/<digest>
#    b. If not, initiate upload: POST /v2/<name>/blobs/uploads/
#    c. Upload blob: PUT <location>?digest=<digest>
# 2. Upload config blob (same as layer)
# 3. Push manifest: PUT /v2/<name>/manifests/<tag>

import gzip
import hashlib
import io
import json
import logging
import re

import requests

from occystrap import constants
from occystrap.outputs.base import ImageOutput
from occystrap import util


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class RegistryWriter(ImageOutput):
    """Pushes images to a Docker/OCI registry.

    This output writer uploads image layers and config to a registry
    using the Docker Registry HTTP API V2, then pushes a manifest to
    make the image available.
    """

    def __init__(self, registry, image, tag, secure=True,
                 username=None, password=None):
        """Initialize the registry writer.

        Args:
            registry: Registry hostname (e.g., 'docker.io', 'ghcr.io').
            image: Image name/path (e.g., 'library/busybox', 'myuser/myimage').
            tag: Image tag (e.g., 'latest', 'v1.0').
            secure: If True, use HTTPS (default). If False, use HTTP.
            username: Username for authentication (optional).
            password: Password/token for authentication (optional).
        """
        self.registry = registry
        self.image = image
        self.tag = tag
        self.secure = secure
        self.username = username
        self.password = password

        self._cached_auth = None
        self._moniker = 'https' if secure else 'http'

        self._config_digest = None
        self._config_size = None
        self._layers = []

    def _request(self, method, url, headers=None, data=None, stream=False):
        """Make an authenticated request to the registry."""
        if not headers:
            headers = {}

        headers['User-Agent'] = util.get_user_agent()

        if self._cached_auth:
            headers['Authorization'] = 'Bearer %s' % self._cached_auth

        r = requests.request(method, url, headers=headers, data=data,
                             stream=stream)

        if r.status_code == 401:
            auth_header = r.headers.get('Www-Authenticate', '')
            auth_re = re.compile(r'Bearer realm="([^"]*)",service="([^"]*)"')
            m = auth_re.match(auth_header)
            if m:
                scope = 'repository:%s:pull,push' % self.image
                auth_url = '%s?service=%s&scope=%s' % (m.group(1), m.group(2),
                                                       scope)
                if self.username and self.password:
                    auth_r = requests.get(auth_url,
                                          auth=(self.username, self.password))
                else:
                    auth_r = requests.get(auth_url)

                if auth_r.status_code == 200:
                    token = auth_r.json().get('token')
                    self._cached_auth = token
                    headers['Authorization'] = 'Bearer %s' % token

                    r = requests.request(method, url, headers=headers,
                                         data=data, stream=stream)

        return r

    def _blob_exists(self, digest):
        """Check if a blob already exists in the registry."""
        url = '%s://%s/v2/%s/blobs/%s' % (self._moniker, self.registry,
                                          self.image, digest)
        r = self._request('HEAD', url)
        return r.status_code == 200

    def _upload_blob(self, digest, data, size):
        """Upload a blob to the registry.

        Args:
            digest: The sha256 digest of the blob (e.g., 'sha256:abc123...').
            data: File-like object containing the blob data.
            size: Size of the blob in bytes.
        """
        if self._blob_exists(digest):
            LOG.info('Blob %s already exists, skipping upload' % digest[:19])
            return

        LOG.info('Uploading blob %s (%d bytes)' % (digest[:19], size))

        url = '%s://%s/v2/%s/blobs/uploads/' % (self._moniker, self.registry,
                                                self.image)
        r = self._request('POST', url)

        if r.status_code not in (200, 202):
            raise Exception('Failed to initiate blob upload: %d %s'
                            % (r.status_code, r.text))

        location = r.headers.get('Location')
        if not location:
            raise Exception('No Location header in upload response')

        if not location.startswith('http'):
            location = '%s://%s%s' % (self._moniker, self.registry, location)

        if '?' in location:
            upload_url = '%s&digest=%s' % (location, digest)
        else:
            upload_url = '%s?digest=%s' % (location, digest)

        data.seek(0)
        r = self._request('PUT', upload_url,
                          headers={'Content-Type': 'application/octet-stream',
                                   'Content-Length': str(size)},
                          data=data)

        if r.status_code not in (200, 201, 202):
            raise Exception('Failed to upload blob: %d %s'
                            % (r.status_code, r.text))

        LOG.info('Blob uploaded successfully')

    def fetch_callback(self, digest):
        """Always fetch all layers for pushing."""
        return True

    def process_image_element(self, element_type, name, data):
        """Process an image element, uploading it to the registry."""
        if element_type == constants.CONFIG_FILE and data is not None:
            LOG.info('Processing config file')

            data.seek(0)
            config_data = data.read()

            h = hashlib.sha256()
            h.update(config_data)
            self._config_digest = 'sha256:%s' % h.hexdigest()
            self._config_size = len(config_data)

            self._upload_blob(self._config_digest, io.BytesIO(config_data),
                              self._config_size)

        elif element_type == constants.IMAGE_LAYER and data is not None:
            LOG.info('Processing layer %s' % name)

            data.seek(0)
            layer_data = data.read()

            compressed = io.BytesIO()
            with gzip.GzipFile(fileobj=compressed, mode='wb') as gz:
                gz.write(layer_data)
            compressed.seek(0)
            compressed_data = compressed.read()

            h = hashlib.sha256()
            h.update(compressed_data)
            layer_digest = 'sha256:%s' % h.hexdigest()
            layer_size = len(compressed_data)

            self._upload_blob(layer_digest, io.BytesIO(compressed_data),
                              layer_size)

            self._layers.append({
                'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                'size': layer_size,
                'digest': layer_digest
            })

    def finalize(self):
        """Push the image manifest to the registry."""
        if not self._config_digest:
            raise Exception('No config file was processed')

        LOG.info('Pushing manifest for %s:%s' % (self.image, self.tag))

        manifest = {
            'schemaVersion': 2,
            'mediaType': 'application/vnd.docker.distribution.manifest.v2+json',
            'config': {
                'mediaType': 'application/vnd.docker.container.image.v1+json',
                'size': self._config_size,
                'digest': self._config_digest
            },
            'layers': self._layers
        }

        manifest_json = json.dumps(manifest, separators=(',', ':'))

        url = '%s://%s/v2/%s/manifests/%s' % (self._moniker, self.registry,
                                              self.image, self.tag)
        r = self._request(
            'PUT', url,
            headers={
                'Content-Type':
                    'application/vnd.docker.distribution.manifest.v2+json'
            },
            data=manifest_json.encode('utf-8'))

        if r.status_code not in (200, 201, 202):
            raise Exception('Failed to push manifest: %d %s'
                            % (r.status_code, r.text))

        LOG.info('Image pushed successfully: %s/%s:%s'
                 % (self.registry, self.image, self.tag))
