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

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import io
import json
import re
import threading
import time

import requests

from occystrap import compression
from occystrap import constants
from occystrap.outputs.base import ImageOutput
from occystrap.progress import LayerProgress
from occystrap import util
from shakenfist_utilities import logs


LOG = logs.setup_console(__name__)


class RegistryWriter(ImageOutput):
    """Pushes images to a Docker/OCI registry.

    This output writer uploads image layers and config to a registry
    using the Docker Registry HTTP API V2, then pushes a manifest to
    make the image available.
    """

    def __init__(self, registry, image, tag, secure=True,
                 username=None, password=None, compression_type=None,
                 max_workers=4, layer_cache=None, filters_hash='none'):
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
            layer_cache: LayerCache instance for cross-invocation
                caching (optional).
            filters_hash: Hash of the filter chain configuration,
                used to key cache entries (default: 'none').
        """
        super().__init__()

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

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers)
        self._config_future = None
        # List of (layer_index, future) tuples
        self._layer_futures = []

        self._config_digest = None
        self._config_size = None

        # Granular timing instrumentation (thread-safe)
        self._timing_lock = threading.Lock()
        self._total_compress_time = 0.0
        self._total_upload_time = 0.0
        self._total_input_bytes = 0
        self._upload_skipped = 0

        # Cross-invocation layer cache
        self._layer_cache = layer_cache
        self._filters_hash = filters_hash
        self._cached_layers = {}  # digest -> cached metadata
        self._cache_hits = 0

        # Original DiffIDs from fetch_callback, consumed
        # in process_image_element order.  Needed because
        # filters may transform layer names (recalculating
        # SHA256 after modifying content), but the cache
        # must be keyed by the original input DiffID.
        self._original_digests = deque()

    @property
    def requires_ordered_layers(self):
        return False

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

        Returns:
            True if the blob already existed (upload skipped),
            False if it was uploaded.
        """
        if self._blob_exists(digest):
            LOG.debug(f'Blob {digest[:19]} already exists, skipping upload')
            return True

        LOG.debug(f'Uploading blob {digest[:19]} ({size} bytes)')

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

        LOG.debug('Blob uploaded successfully')
        return False

    def _compress_and_upload_layer(
            self, layer_data, input_digest=None):
        """Compress a layer and upload it to the registry.

        This method runs in the thread pool to parallelize compression.

        Args:
            layer_data: Uncompressed layer tarball data (bytes).
            input_digest: The input layer DiffID for cache recording
                (optional).

        Returns:
            Dict with layer metadata (mediaType, size, digest).
        """
        input_size = len(layer_data)

        # Compress layer
        t0 = time.time()
        compressed_data = compression.compress_data(
            layer_data, self.compression_type)
        compress_time = time.time() - t0

        # Calculate digest
        h = hashlib.sha256()
        h.update(compressed_data)
        layer_digest = f'sha256:{h.hexdigest()}'
        layer_size = len(compressed_data)

        # Get media type for compression format
        layer_media_type = compression.get_media_type_for_compression(
            self.compression_type)

        # Upload
        t0 = time.time()
        skipped = self._upload_blob(
            layer_digest, io.BytesIO(compressed_data), layer_size)
        upload_time = time.time() - t0

        # Accumulate timing stats and record cache (thread-safe)
        with self._timing_lock:
            self._total_compress_time += compress_time
            self._total_upload_time += upload_time
            self._total_input_bytes += input_size
            if skipped:
                self._upload_skipped += 1

            # Record in layer cache for future invocations
            if self._layer_cache is not None and input_digest:
                self._layer_cache.record(
                    input_digest, self._filters_hash,
                    layer_digest, layer_size, layer_media_type)

        # Return metadata for manifest
        return {
            'mediaType': layer_media_type,
            'size': layer_size,
            'digest': layer_digest
        }

    def fetch_callback(self, digest):
        """Determine whether a layer needs to be fetched.

        If a layer cache is configured and the layer has been
        previously processed with the same filters, check whether
        the registry still has the compressed blob. If so, skip
        fetching entirely.

        The original digest is recorded so that
        process_image_element can map filter-transformed names
        back to the original input DiffID for cache recording.
        """
        self._original_digests.append(digest)

        if self._layer_cache is None:
            return True

        entry = self._layer_cache.lookup(
            digest, self._filters_hash)
        if entry is None:
            return True

        # Check if registry still has this blob
        if self._blob_exists(entry['compressed_digest']):
            LOG.debug(
                'Layer %s cached, registry has %s, skipping',
                digest[:19], entry['compressed_digest'][:19])
            self._cached_layers[digest] = entry
            return False

        LOG.debug(
            'Layer %s cached but registry missing %s, '
            're-processing',
            digest[:19], entry['compressed_digest'][:19])
        return True

    def process_image_element(self, element):
        """Process an image element, uploading it to
        the registry.

        Both compression and uploads are submitted to
        a thread pool for parallel execution. This
        allows multiple layers to compress and upload
        simultaneously.
        """
        # Track start time (can't use _track_element
        # because we track compressed sizes which are
        # only known after compression)
        if self._start_time is None:
            self._start_time = time.time()

        if (element.element_type == constants.CONFIG_FILE
                and element.data is not None):
            LOG.debug('Processing config file')

            element.data.seek(0)
            config_data = element.data.read()

            h = hashlib.sha256()
            h.update(config_data)
            self._config_digest = \
                f'sha256:{h.hexdigest()}'
            self._config_size = len(config_data)

            # Submit config upload to thread pool
            self._config_future = \
                self._executor.submit(
                    self._upload_blob,
                    self._config_digest,
                    io.BytesIO(config_data),
                    self._config_size)

        elif (element.element_type
                == constants.IMAGE_LAYER
                and element.data is not None):
            # Use the original DiffID for cache
            # recording; filters may have transformed
            # `name` by recalculating the hash after
            # modifying content. Falls back to
            # element.name when fetch_callback was not
            # called (e.g., in unit tests).
            if self._original_digests:
                original_digest = (
                    self._original_digests.popleft())
            else:
                original_digest = element.name
            LOG.debug(
                'Processing layer %s'
                % element.name)

            element.data.seek(0)
            layer_data = element.data.read()

            # Submit compression + upload to thread
            # pool for parallel compression
            future = self._executor.submit(
                self._compress_and_upload_layer,
                layer_data, original_digest)
            self._layer_futures.append(
                (element.layer_index, future))

        elif (element.element_type
                == constants.IMAGE_LAYER
                and element.data is None):
            if self._original_digests:
                original_digest = (
                    self._original_digests.popleft())
            else:
                original_digest = element.name
            # Layer was skipped by fetch_callback -
            # check cache
            if original_digest in self._cached_layers:
                entry = \
                    self._cached_layers[original_digest]
                f = Future()
                f.set_result({
                    'mediaType': entry['media_type'],
                    'size': entry['compressed_size'],
                    'digest': entry[
                        'compressed_digest']
                })
                self._layer_futures.append(
                    (element.layer_index, f))
                # _cache_hits is only updated from the
                # main thread (process_image_element is
                # not called from worker threads), so
                # no lock is needed.
                self._cache_hits += 1

    def finalize(self):
        """Push the image manifest to the registry.

        Waits for all parallel compression/uploads to
        complete before pushing the manifest. Layer
        metadata is collected from futures and sorted
        by layer_index to build the manifest in correct
        order.
        """
        total_layers = len(self._layer_futures)
        LOG.info(
            'Waiting for %d layer'
            ' compression/uploads to complete...'
            % total_layers)

        errors = []

        # Wait for config upload first
        if self._config_future:
            try:
                self._config_future.result()
                LOG.debug('Config uploaded')
            except Exception as e:
                errors.append('Config upload: %s' % e)

        # Collect layer metadata from futures
        # Each entry is (layer_index, future)
        indexed_layers = []
        unindexed_layers = []

        with LayerProgress(
                total=total_layers,
                desc='Uploading') as progress:
            for i, (layer_index, future) in enumerate(
                    self._layer_futures):
                try:
                    layer_metadata = future.result()
                    if layer_index is not None:
                        indexed_layers.append(
                            (layer_index,
                             layer_metadata))
                    else:
                        unindexed_layers.append(
                            layer_metadata)
                    progress.update(1)

                except Exception as e:
                    errors.append(
                        'Layer %d: %s' % (i, e))

        self._executor.shutdown(wait=True)

        # Reconstruct layer order from indices
        if indexed_layers:
            indexed_layers.sort(
                key=lambda x: x[0])
            layers = [
                meta for _, meta in indexed_layers]
        else:
            layers = unindexed_layers

        if errors:
            raise Exception(f'Upload failed: {"; ".join(errors)}')

        if not self._config_digest:
            raise Exception('No config file was processed')

        # Store for introspection (e.g. tests)
        self._layers = layers

        LOG.info(f'All {total_layers} layers uploaded, pushing manifest '
                 f'for {self.image}:{self.tag}')

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

        # Save layer cache if configured
        if self._layer_cache is not None:
            self._layer_cache.save()

        # Summary line
        if self._start_time is not None:
            elapsed = time.time() - self._start_time
        else:
            elapsed = 0.0
        total_compressed = self._config_size + sum(
            layer['size'] for layer in self._layers)
        total_input = (
            self._total_input_bytes + self._config_size)
        compressed_mb = total_compressed / 1_000_000
        input_mb = total_input / 1_000_000
        ratio = (total_compressed / total_input * 100
                 if total_input else 0)
        LOG.with_fields({
            'layers': len(self._layers),
            'elapsed_s': round(elapsed, 1),
            'compress_s': round(
                self._total_compress_time, 1),
            'upload_s': round(
                self._total_upload_time, 1),
            'upload_skipped': self._upload_skipped,
            'cache_hits': self._cache_hits,
            'input_mb': round(input_mb, 1),
            'output_mb': round(compressed_mb, 1),
            'ratio_pct': round(ratio),
        }).info('Push complete')
