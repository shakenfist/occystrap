# A simple implementation of a docker registry client. Fetches an image to a tarball.
# With a big nod to https://github.com/NotGlop/docker-drag/blob/master/docker_pull.py

# https://docs.docker.com/registry/spec/manifest-v2-2/ documents the image manifest
# format, noting that the response format you get back varies based on what you have
# in your accept header for the request.

# https://github.com/opencontainers/image-spec/blob/main/media-types.md documents
# the new OCI mime types.

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
import re
from requests.exceptions import ChunkedEncodingError, ConnectionError
from shakenfist_utilities import logs
import tempfile
import threading
import time

from occystrap import compression
from occystrap import constants
from occystrap import util
from occystrap.inputs.base import (
    ImageInput, ImageInputError, always_fetch)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # Exponential backoff: 2^attempt seconds

LOG = logs.setup_console(__name__)

DELETED_FILE_RE = re.compile(r'.*/\.wh\.(.*)$')


class Image(ImageInput):
    def __init__(self, registry, image, tag, os='linux', architecture='amd64',
                 variant='', secure=True, username=None, password=None,
                 max_workers=4, temp_dir=None):
        self.registry = registry
        self._image = image
        self._tag = tag
        self.os = os
        self.architecture = architecture
        self.variant = variant
        self.secure = secure
        self.username = username
        self.password = password
        self.max_workers = max_workers
        self.temp_dir = temp_dir

        self._cached_auth = None
        self._auth_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # Cached manifest and config for get_manifest()/get_config()
        self._manifest = None
        self._manifest_media_type = None
        self._config = None
        self._config_bytes = None

    @property
    def image(self):
        """Return the image name."""
        return self._image

    @property
    def tag(self):
        """Return the image tag."""
        return self._tag

    def request_url(self, method, url, headers=None, data=None, stream=False):
        """Make an authenticated request to the registry.

        Thread-safe: uses _auth_lock to protect _cached_auth updates.
        """
        if not headers:
            headers = {}

        with self._auth_lock:
            if self._cached_auth:
                headers.update({'Authorization': 'Bearer %s' % self._cached_auth})

        try:
            return util.request_url(method, url, headers=headers, data=data,
                                    stream=stream)
        except util.UnauthorizedException as e:
            auth_re = re.compile('Bearer realm="([^"]*)",service="([^"]*)"')
            m = auth_re.match(e.args[5].get('Www-Authenticate', ''))
            if m:
                auth_url = ('%s?service=%s&scope=repository:%s:pull'
                            % (m.group(1), m.group(2), self.image))
                # If credentials are provided, use Basic auth for token request
                if self.username and self.password:
                    r = util.request_url(
                        'GET', auth_url,
                        auth=(self.username, self.password))
                else:
                    r = util.request_url('GET', auth_url)
                token = r.json().get('token')
                headers.update({'Authorization': 'Bearer %s' % token})
                with self._auth_lock:
                    self._cached_auth = token

            return util.request_url(
                method, url, headers=headers, data=data, stream=stream)

    def _url_scheme(self):
        """Return 'https' or 'http' based on self.secure."""
        return 'https' if self.secure else 'http'

    def get_manifest(self):
        """Fetch and return the distribution manifest.

        Resolves manifest lists (multi-arch) to the
        platform-specific manifest matching this
        instance's os/architecture/variant.

        Caches the result for subsequent calls.
        """
        if self._manifest is not None:
            return self._manifest

        moniker = self._url_scheme()

        r = self.request_url(
            'GET',
            '%s://%s/v2/%s/manifests/%s'
            % (moniker, self.registry,
               self.image, self.tag),
            headers={
                'Accept': '%s,%s,%s,%s' % (
                    constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
                    constants.MEDIA_TYPE_DOCKER_MANIFEST_LIST_V2,
                    constants.MEDIA_TYPE_OCI_MANIFEST,
                    constants.MEDIA_TYPE_OCI_INDEX)
            })

        content_type = r.headers['Content-Type']

        if content_type in [
                constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
                constants.MEDIA_TYPE_OCI_MANIFEST]:
            self._manifest = r.json()
            self._manifest_media_type = content_type

        elif content_type in [
                constants.MEDIA_TYPE_DOCKER_MANIFEST_LIST_V2,
                constants.MEDIA_TYPE_OCI_INDEX]:
            manifests = r.json()['manifests']
            for m in manifests:
                plat = m['platform']
                if plat.get('variant'):
                    LOG.debug(
                        'Found manifest for %s on'
                        ' %s %s'
                        % (plat['os'],
                           plat['architecture'],
                           plat['variant']))
                else:
                    LOG.debug(
                        'Found manifest for %s on'
                        ' %s'
                        % (plat['os'],
                           plat['architecture']))

            for m in manifests:
                if (m['platform']['os'] == self.os
                        and m['platform'][
                            'architecture']
                        == self.architecture
                        and m['platform'].get(
                            'variant', '')
                        == self.variant):
                    LOG.debug(
                        'Fetching matching manifest')
                    r2 = self.request_url(
                        'GET',
                        '%s://%s/v2/%s/manifests/%s'
                        % (moniker, self.registry,
                           self.image,
                           m['digest']),
                        headers={
                            'Accept': (
                                '%s, %s' % (
                                    constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
                                    constants.MEDIA_TYPE_OCI_MANIFEST))
                        })
                    self._manifest = r2.json()
                    self._manifest_media_type = \
                        r2.headers['Content-Type']
                    return self._manifest

            raise ImageInputError(
                'Could not find a matching manifest'
                ' for this os / architecture / variant')
        else:
            raise ImageInputError(
                'Unknown manifest content type %s!'
                % content_type)

        return self._manifest

    def get_config(self):
        """Fetch and return the parsed image config.

        Uses get_manifest() to find the config digest,
        then fetches the config blob from the registry.
        Verifies the blob's SHA256 digest matches the
        manifest's config descriptor.

        Caches the result for subsequent calls.
        """
        if self._config is not None:
            return self._config

        manifest = self.get_manifest()
        if manifest is None:
            return None

        config_digest = manifest['config']['digest']
        moniker = self._url_scheme()

        r = self.request_url(
            'GET',
            '%s://%s/v2/%s/blobs/%s'
            % (moniker, self.registry,
               self.image, config_digest))

        self._config_bytes = r.content
        h = hashlib.sha256()
        h.update(self._config_bytes)
        expected = config_digest.split(':')[1]
        if h.hexdigest() != expected:
            raise ImageInputError(
                'Hash verification failed for'
                ' config blob (%s vs %s)'
                % (expected, h.hexdigest()))

        self._config = r.json()
        return self._config

    def _download_layer(self, layer, moniker):
        """Download a single layer to a temp file.

        This method is designed to be called from a ThreadPoolExecutor for
        parallel layer downloads. It handles decompression, hash verification,
        and retry logic.

        Args:
            layer: Layer metadata dict with 'digest', 'size', 'mediaType'.
            moniker: URL scheme ('http' or 'https').

        Returns:
            Tuple of (layer_filename, temp_file_path) on success.

        Raises:
            Exception on failure after all retries exhausted.
        """
        layer_filename = layer['digest'].split(':')[1]

        LOG.debug('Fetching layer %s (%d bytes)'
                  % (layer['digest'], layer['size']))

        # Detect compression from media type (fallback to gzip for compat)
        layer_media_type = layer.get('mediaType')
        compression_type = compression.detect_compression_from_media_type(
            layer_media_type)
        if compression_type == constants.COMPRESSION_UNKNOWN:
            compression_type = constants.COMPRESSION_GZIP
        LOG.debug('Layer compression: %s' % compression_type)

        # Retry logic for streaming downloads which can fail mid-transfer
        last_exception = None
        for attempt in range(MAX_RETRIES + 1):
            tf = None
            try:
                r = self.request_url(
                    'GET',
                    '%(moniker)s://%(registry)s/v2/%(image)s/blobs/%(layer)s'
                    % {
                        'moniker': moniker,
                        'registry': self.registry,
                        'image': self.image,
                        'layer': layer['digest']
                    },
                    stream=True)

                # Use streaming decompressor based on detected compression.
                h = hashlib.sha256()
                d = compression.StreamingDecompressor(compression_type)

                tf = tempfile.NamedTemporaryFile(delete=False, dir=self.temp_dir)
                LOG.debug('Temporary file for layer is %s' % tf.name)
                for chunk in r.iter_content(8192):
                    tf.write(d.decompress(chunk))
                    h.update(chunk)
                # Flush any remaining data
                remaining = d.flush()
                if remaining:
                    tf.write(remaining)
                tf.close()

                if h.hexdigest() != layer_filename:
                    LOG.error('Hash verification failed for layer (%s vs %s)'
                              % (layer_filename, h.hexdigest()))
                    os.unlink(tf.name)
                    raise Exception('Hash verification failed for layer %s'
                                    % layer_filename)

                return (layer_filename, tf.name)

            except (ChunkedEncodingError, ConnectionError) as e:
                last_exception = e
                # Clean up temp file if it exists
                if tf is not None and tf.name and os.path.exists(tf.name):
                    try:
                        tf.close()
                    except Exception:
                        pass
                    os.unlink(tf.name)

                if attempt < MAX_RETRIES:
                    wait_time = RETRY_BACKOFF_BASE ** attempt
                    LOG.warning(
                        'Layer download failed (attempt %d/%d): %s. '
                        'Retrying in %d seconds...'
                        % (attempt + 1, MAX_RETRIES + 1, str(e), wait_time))
                    time.sleep(wait_time)
                else:
                    LOG.error('Layer download failed after %d attempts: %s'
                              % (MAX_RETRIES + 1, str(e)))
                    raise last_exception

        # Should not reach here, but just in case
        raise Exception('Layer download failed unexpectedly')

    def fetch(self, fetch_callback=always_fetch,
              ordered=True):
        LOG.info('Fetching manifest')
        manifest = self.get_manifest()
        moniker = self._url_scheme()

        LOG.info('Fetching config file')
        self.get_config()

        config_digest = manifest['config']['digest']
        config_filename = (
            '%s.json' % config_digest.split(':')[1])
        yield constants.ImageElement(
            constants.CONFIG_FILE, config_filename,
            io.BytesIO(self._config_bytes))

        LOG.info('There are %d image layers'
                 % len(manifest['layers']))

        # Submit all layer downloads in parallel
        # Each entry is (layer_idx, layer_filename,
        # future_or_none) where None means skip
        layer_futures = []
        for layer_idx, layer in enumerate(
                manifest['layers']):
            layer_filename = \
                layer['digest'].split(':')[1]
            if not fetch_callback(layer_filename):
                LOG.debug(
                    'Fetch callback says skip'
                    ' layer %s' % layer['digest'])
                layer_futures.append(
                    (layer_idx, layer_filename, None))
            else:
                future = self._executor.submit(
                    self._download_layer, layer,
                    moniker)
                layer_futures.append(
                    (layer_idx, layer_filename,
                     future))

        if not ordered:
            # Yield layers as they complete for
            # maximum throughput
            from concurrent.futures import \
                as_completed
            # Build future -> (index, filename) map
            future_map = {}
            for layer_idx, layer_filename, future \
                    in layer_futures:
                if future is None:
                    # Skipped layers yield immediately
                    idx = layer_idx
                    yield constants.ImageElement(
                        constants.IMAGE_LAYER,
                        layer_filename, None,
                        layer_index=idx)
                else:
                    future_map[future] = (
                        layer_idx, layer_filename)

            for future in as_completed(future_map):
                layer_idx, layer_filename = \
                    future_map[future]
                try:
                    result_filename, temp_file_path \
                        = future.result()
                    try:
                        with open(
                                temp_file_path,
                                'rb') as f:
                            yield \
                                constants.ImageElement(
                                    constants.IMAGE_LAYER,
                                    result_filename, f,
                                    layer_index=layer_idx
                                )
                    finally:
                        os.unlink(temp_file_path)
                except Exception:
                    for ft in future_map:
                        ft.cancel()
                    raise
        else:
            # Yield results in order, waiting for
            # each download to complete
            for layer_idx, layer_filename, future \
                    in layer_futures:
                if future is None:
                    yield constants.ImageElement(
                        constants.IMAGE_LAYER,
                        layer_filename, None)
                else:
                    try:
                        result_filename, \
                            temp_file_path = \
                            future.result()
                        try:
                            with open(
                                    temp_file_path,
                                    'rb') as f:
                                yield \
                                    constants.ImageElement(
                                        constants.IMAGE_LAYER,
                                        result_filename,
                                        f)
                        finally:
                            os.unlink(temp_file_path)
                    except Exception:
                        for _, _, remaining_future \
                                in layer_futures:
                            if remaining_future \
                                    is not None:
                                remaining_future.cancel()
                        raise

        LOG.info('Done')
