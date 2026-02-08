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
import logging
import os
import re
from requests.exceptions import ChunkedEncodingError, ConnectionError
import sys
import tempfile
import threading
import time

from occystrap import compression
from occystrap import constants
from occystrap import util
from occystrap.inputs.base import ImageInput

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # Exponential backoff: 2^attempt seconds

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DELETED_FILE_RE = re.compile(r'.*/\.wh\.(.*)$')


def always_fetch():
    return True


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

        LOG.info('Fetching layer %s (%d bytes)'
                 % (layer['digest'], layer['size']))

        # Detect compression from media type (fallback to gzip for compat)
        layer_media_type = layer.get('mediaType')
        compression_type = compression.detect_compression_from_media_type(
            layer_media_type)
        if compression_type == constants.COMPRESSION_UNKNOWN:
            compression_type = constants.COMPRESSION_GZIP
        LOG.info('Layer compression: %s' % compression_type)

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
                LOG.info('Temporary file for layer is %s' % tf.name)
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

    def fetch(self, fetch_callback=always_fetch):
        LOG.info('Fetching manifest')
        moniker = 'https'
        if not self.secure:
            moniker = 'http'

        r = self.request_url(
            'GET',
            '%(moniker)s://%(registry)s/v2/%(image)s/manifests/%(tag)s'
            % {
                'moniker': moniker,
                'registry': self.registry,
                'image': self.image,
                'tag': self.tag
            },
            headers={
                'Accept': ('%s,%s,%s,%s' % (
                    constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
                    constants.MEDIA_TYPE_DOCKER_MANIFEST_LIST_V2,
                    constants.MEDIA_TYPE_OCI_MANIFEST,
                    constants.MEDIA_TYPE_OCI_INDEX))
            })

        config_digest = None
        if r.headers['Content-Type'] in [
                constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
                constants.MEDIA_TYPE_OCI_MANIFEST]:
            manifest = r.json()
            config_digest = manifest['config']['digest']
        elif r.headers['Content-Type'] in [
                constants.MEDIA_TYPE_DOCKER_MANIFEST_LIST_V2,
                constants.MEDIA_TYPE_OCI_INDEX]:
            for m in r.json()['manifests']:
                if 'variant' in m['platform']:
                    LOG.info('Found manifest for %s on %s %s'
                             % (m['platform']['os'],
                                m['platform']['architecture'],
                                m['platform']['variant']))
                else:
                    LOG.info('Found manifest for %s on %s'
                             % (m['platform']['os'],
                                m['platform']['architecture']))

                if (m['platform']['os'] == self.os and
                    m['platform']['architecture'] == self.architecture and
                        m['platform'].get('variant', '') == self.variant):
                    LOG.info('Fetching matching manifest')
                    r = self.request_url(
                        'GET',
                        '%(moniker)s://%(registry)s/v2/%(image)s/manifests/%(tag)s'
                        % {
                            'moniker': moniker,
                            'registry': self.registry,
                            'image': self.image,
                            'tag': m['digest']
                        },
                        headers={
                            'Accept': ('%s, %s' % (
                                constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
                                constants.MEDIA_TYPE_OCI_MANIFEST))
                        })
                    manifest = r.json()
                    config_digest = manifest['config']['digest']

            if not config_digest:
                raise Exception('Could not find a matching manifest for this '
                                'os / architecture / variant')
        else:
            raise Exception('Unknown manifest content type %s!' %
                            r.headers['Content-Type'])

        LOG.info('Fetching config file')
        r = self.request_url(
            'GET',
            '%(moniker)s://%(registry)s/v2/%(image)s/blobs/%(config)s'
            % {
                'moniker': moniker,
                'registry': self.registry,
                'image': self.image,
                'config': config_digest
            })
        config = r.content
        h = hashlib.sha256()
        h.update(config)
        if h.hexdigest() != config_digest.split(':')[1]:
            LOG.error('Hash verification failed for image config blob (%s vs %s)'
                      % (config_digest.split(':')[1], h.hexdigest()))
            sys.exit(1)

        config_filename = ('%s.json' % config_digest.split(':')[1])
        yield (constants.CONFIG_FILE, config_filename,
               io.BytesIO(config))

        LOG.info('There are %d image layers' % len(manifest['layers']))

        # Submit all layer downloads in parallel
        # Each entry is (layer_filename, future_or_none) where None means skip
        layer_futures = []
        for layer in manifest['layers']:
            layer_filename = layer['digest'].split(':')[1]
            if not fetch_callback(layer_filename):
                LOG.info('Fetch callback says skip layer %s' % layer['digest'])
                layer_futures.append((layer_filename, None))
            else:
                future = self._executor.submit(
                    self._download_layer, layer, moniker)
                layer_futures.append((layer_filename, future))

        # Yield results in order, waiting for each download to complete
        for layer_filename, future in layer_futures:
            if future is None:
                yield (constants.IMAGE_LAYER, layer_filename, None)
            else:
                # Wait for this specific download to complete
                try:
                    result_filename, temp_file_path = future.result()
                    try:
                        with open(temp_file_path, 'rb') as f:
                            yield (constants.IMAGE_LAYER, result_filename, f)
                    finally:
                        os.unlink(temp_file_path)
                except Exception:
                    # Clean up any remaining futures on error
                    for _, remaining_future in layer_futures:
                        if remaining_future is not None:
                            remaining_future.cancel()
                    raise

        LOG.info('Done')
