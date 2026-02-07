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

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import logging
import re
import threading

import requests

from occystrap import compression
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
                 username=None, password=None, compression_type=None,
                 max_workers=4):
        """Initialize the registry writer.

        Args:
            registry: Registry hostname (e.g., 'docker.io', 'ghcr.io').
            image: Image name/path (e.g., 'library/busybox', 'myuser/myimage').
            tag: Image tag (e.g., 'latest', 'v1.0').
            secure: If True, use HTTPS (default). If False, use HTTP.
            username: Username for authentication (optional).
            password: Password/token for authentication (optional).
            compression_type: Compression for layers ('gzip' or 'zstd').
                Defaults to 'gzip' for maximum compatibility.
            max_workers: Number of parallel upload threads (default: 4).
        """
        self.registry = registry
        self.image = image
        self.tag = tag
        self.secure = secure
        self.username = username
        self.password = password
        self.compression_type = compression_type or constants.COMPRESSION_GZIP
        self.max_workers = max_workers

        self._cached_auth = None
        self._moniker = 'https' if secure else 'http'
        self._auth_lock = threading.Lock()

        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._upload_futures = []

        self._config_digest = None
        self._config_size = None
        self._layers = []

    def _request(self, method, url, headers=None, data=None, stream=False):
        """Make an authenticated request to the registry.

        Thread-safe: uses _auth_lock to protect _cached_auth updates.
        """
        if not headers:
            headers = {}

        headers['User-Agent'] = util.get_user_agent()

        with self._auth_lock:
            if self._cached_auth:
                headers['Authorization'] = f'Bearer {self._cached_auth}'

        r = requests.request(method, url, headers=headers, data=data,
                             stream=stream)

        if r.status_code == 401:
            auth_header = r.headers.get('Www-Authenticate', '')
            auth_re = re.compile(r'Bearer realm="([^"]*)",service="([^"]*)"')
            m = auth_re.match(auth_header)
            if m:
                scope = f'repository:{self.image}:pull,push'
                auth_url = f'{m.group(1)}?service={m.group(2)}&scope={scope}'
                if self.username and self.password:
                    auth_r = requests.get(auth_url,
                                          auth=(self.username, self.password))
                else:
                    auth_r = requests.get(auth_url)

                if auth_r.status_code == 200:
                    token = auth_r.json().get('token')
                    with self._auth_lock:
                        self._cached_auth = token
                    headers['Authorization'] = f'Bearer {token}'

                    r = requests.request(method, url, headers=headers,
                                         data=data, stream=stream)

        return r

    def _blob_exists(self, digest):
        """Check if a blob already exists in the registry."""
        url = f'{self._moniker}://{self.registry}/v2/{self.image}/blobs/{digest}'
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
            LOG.info(f'Blob {digest[:19]} already exists, skipping upload')
            return

        LOG.info(f'Uploading blob {digest[:19]} ({size} bytes)')

        url = f'{self._moniker}://{self.registry}/v2/{self.image}/blobs/uploads/'
        r = self._request('POST', url)

        if r.status_code not in (200, 202):
            raise Exception(f'Failed to initiate blob upload: {r.status_code} '
                            f'{r.text}')

        location = r.headers.get('Location')
        if not location:
            raise Exception('No Location header in upload response')

        if not location.startswith('http'):
            location = f'{self._moniker}://{self.registry}{location}'

        if '?' in location:
            upload_url = f'{location}&digest={digest}'
        else:
            upload_url = f'{location}?digest={digest}'

        data.seek(0)
        r = self._request('PUT', upload_url,
                          headers={'Content-Type': 'application/octet-stream',
                                   'Content-Length': str(size)},
                          data=data)

        if r.status_code not in (200, 201, 202):
            raise Exception(f'Failed to upload blob: {r.status_code} {r.text}')

        LOG.info('Blob uploaded successfully')

    def fetch_callback(self, digest):
        """Always fetch all layers for pushing."""
        return True

    def process_image_element(self, element_type, name, data):
        """Process an image element, uploading it to the registry.

        Uploads are submitted to a thread pool for parallel execution.
        Layer metadata is recorded immediately to preserve order in the
        manifest.
        """
        if element_type == constants.CONFIG_FILE and data is not None:
            LOG.info('Processing config file')

            data.seek(0)
            config_data = data.read()

            h = hashlib.sha256()
            h.update(config_data)
            self._config_digest = f'sha256:{h.hexdigest()}'
            self._config_size = len(config_data)

            # Submit upload to thread pool
            future = self._executor.submit(
                self._upload_blob, self._config_digest,
                io.BytesIO(config_data), self._config_size)
            self._upload_futures.append(future)

        elif element_type == constants.IMAGE_LAYER and data is not None:
            LOG.info(f'Processing layer {name}')

            data.seek(0)
            layer_data = data.read()

            # Compress layer with configured compression type (synchronous)
            compressed_data = compression.compress_data(
                layer_data, self.compression_type)

            h = hashlib.sha256()
            h.update(compressed_data)
            layer_digest = f'sha256:{h.hexdigest()}'
            layer_size = len(compressed_data)

            # Use appropriate media type for compression format
            layer_media_type = compression.get_media_type_for_compression(
                self.compression_type)

            # Record layer metadata immediately to preserve order
            self._layers.append({
                'mediaType': layer_media_type,
                'size': layer_size,
                'digest': layer_digest
            })

            # Submit upload to thread pool
            future = self._executor.submit(
                self._upload_blob, layer_digest,
                io.BytesIO(compressed_data), layer_size)
            self._upload_futures.append(future)

    def finalize(self):
        """Push the image manifest to the registry.

        Waits for all parallel uploads to complete before pushing the
        manifest. If any upload fails, an exception is raised.
        """
        # Wait for all uploads to complete
        LOG.info(f'Waiting for {len(self._upload_futures)} uploads to '
                 'complete...')
        errors = []
        for future in as_completed(self._upload_futures):
            try:
                future.result()
            except Exception as e:
                errors.append(str(e))

        self._executor.shutdown(wait=True)

        if errors:
            raise Exception(f'Upload failed: {"; ".join(errors)}')

        if not self._config_digest:
            raise Exception('No config file was processed')

        LOG.info(f'Pushing manifest for {self.image}:{self.tag}')

        manifest = {
            'schemaVersion': 2,
            'mediaType': constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
            'config': {
                'mediaType': constants.MEDIA_TYPE_DOCKER_CONFIG,
                'size': self._config_size,
                'digest': self._config_digest
            },
            'layers': self._layers
        }

        manifest_json = json.dumps(manifest, separators=(',', ':'))

        url = (f'{self._moniker}://{self.registry}/v2/{self.image}'
               f'/manifests/{self.tag}')
        r = self._request(
            'PUT', url,
            headers={
                'Content-Type': constants.MEDIA_TYPE_DOCKER_MANIFEST_V2
            },
            data=manifest_json.encode('utf-8'))

        if r.status_code not in (200, 201, 202):
            raise Exception(f'Failed to push manifest: {r.status_code} '
                            f'{r.text}')

        LOG.info(f'Image pushed successfully: {self.registry}/{self.image}'
                 f':{self.tag}')
