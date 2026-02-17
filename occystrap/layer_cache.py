"""Cross-invocation layer cache for registry push operations.

This module provides a persistent cache that maps input layer DiffIDs
to their compressed output digests. When pushing multiple images that
share base layers, the cache allows subsequent invocations to skip
layers that have already been processed (filtered, compressed, and
uploaded).

Cache entries are keyed by (input_diffid, filters_hash) so that the
same layer processed with different filter configurations gets separate
cache entries.
"""

import datetime
import json
import os
import tempfile

from shakenfist_utilities import logs


LOG = logs.setup_console(__name__)


class LayerCache:
    """Persistent cache mapping input DiffIDs to compressed digests.

    The cache is stored as a JSON file on disk and loaded/saved
    across invocations. The lookup() method is called from the main
    thread (via fetch_callback). The record() method is called from
    thread pool workers but is protected by the registry writer's
    _timing_lock to ensure thread safety.

    Note: the cache currently has no eviction policy or size
    limit. For typical container image workloads the cache stays
    small (one entry per unique layer), but long-lived caches
    across many unrelated images may grow unbounded.
    TODO: add an optional max-age or max-entries eviction policy.
    """

    def __init__(self, path):
        """Load or create a layer cache at the given path.

        Args:
            path: Path to the JSON cache file. Created if it does
                not exist.
        """
        self._path = path
        self._dirty = False
        self._data = {'version': 1, 'layers': {}}

        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    loaded = json.load(f)
                if loaded.get('version') == 1:
                    self._data = loaded
                    LOG.info(
                        'Loaded layer cache with %d entries '
                        'from %s',
                        len(self._data['layers']), path)
                else:
                    LOG.warning(
                        'Ignoring layer cache %s: unknown '
                        'version %s',
                        path, loaded.get('version'))
            except (json.JSONDecodeError, OSError) as e:
                LOG.warning(
                    'Could not load layer cache %s: %s', path, e)

    def lookup(self, diffid, filters_hash):
        """Look up a cached layer by DiffID and filters hash.

        Args:
            diffid: The input layer DiffID as bare hex (input
                sources strip the 'sha256:' prefix).
            filters_hash: Hash of the filter chain configuration.

        Returns:
            Dict with cached metadata (compressed_digest,
            compressed_size, media_type) or None if not found.
        """
        entry = self._data['layers'].get(diffid)
        if entry is None:
            return None
        if entry.get('filters_hash') != filters_hash:
            return None
        return entry

    def record(self, diffid, filters_hash, compressed_digest,
               compressed_size, media_type):
        """Record a layer mapping in the cache.

        Must be called under the registry writer's _timing_lock
        when called from a thread pool worker.

        Args:
            diffid: The input layer DiffID as bare hex.
            filters_hash: Hash of the filter chain configuration.
            compressed_digest: SHA256 digest of the compressed blob.
            compressed_size: Size of the compressed blob in bytes.
            media_type: Media type of the compressed blob.
        """
        self._data['layers'][diffid] = {
            'compressed_digest': compressed_digest,
            'compressed_size': compressed_size,
            'media_type': media_type,
            'filters_hash': filters_hash,
            'timestamp': datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
        }
        self._dirty = True

    def save(self):
        """Save the cache to disk atomically.

        Uses a temporary file and rename to avoid corruption if
        the process is interrupted during write.
        """
        if not self._dirty:
            return

        cache_dir = os.path.dirname(self._path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=cache_dir or '.', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp_path, self._path)
                self._dirty = False
                LOG.info(
                    'Saved layer cache with %d entries to %s',
                    len(self._data['layers']), self._path)
            except BaseException:
                os.unlink(tmp_path)
                raise
        except OSError as e:
            LOG.warning('Could not save layer cache: %s', e)

    @property
    def path(self):
        """Return the cache file path."""
        return self._path

    def __len__(self):
        """Return the number of cached entries."""
        return len(self._data['layers'])
