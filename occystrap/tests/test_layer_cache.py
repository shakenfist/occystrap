import json
import os
import tempfile
import unittest

from occystrap.layer_cache import LayerCache


class LayerCacheTestCase(unittest.TestCase):
    # DiffID keys are bare hex strings at runtime because input
    # sources strip the 'sha256:' prefix before calling
    # fetch_callback.  Compressed digests keep the 'sha256:'
    # prefix since they are used in registry API calls.

    def test_empty_cache(self):
        """Test creating a new empty cache."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')
            cache = LayerCache(path)
            self.assertEqual(0, len(cache))

    def test_record_and_lookup(self):
        """Test recording and looking up a cache entry."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')
            cache = LayerCache(path)

            cache.record(
                'abc111', 'none',
                'sha256:compressed222', 12345,
                'application/vnd.docker.image.rootfs'
                '.diff.tar.gzip')

            entry = cache.lookup('abc111', 'none')
            self.assertIsNotNone(entry)
            self.assertEqual('sha256:compressed222',
                             entry['compressed_digest'])
            self.assertEqual(12345, entry['compressed_size'])
            self.assertEqual(
                'application/vnd.docker.image.rootfs'
                '.diff.tar.gzip',
                entry['media_type'])
            self.assertEqual('none', entry['filters_hash'])

    def test_lookup_miss(self):
        """Test looking up a non-existent entry."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')
            cache = LayerCache(path)
            self.assertIsNone(
                cache.lookup('nonexistent', 'none'))

    def test_lookup_wrong_filters_hash(self):
        """Test that lookup misses with different filters hash."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')
            cache = LayerCache(path)

            cache.record(
                'abc111', 'none',
                'sha256:compressed222', 12345,
                'application/vnd.docker.image.rootfs'
                '.diff.tar.gzip')

            # Same diffid but different filters hash
            entry = cache.lookup(
                'abc111', 'different_hash')
            self.assertIsNone(entry)

    def test_save_and_load(self):
        """Test that cache persists across instances."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')

            # Create and save
            cache1 = LayerCache(path)
            cache1.record(
                'aaa111', 'none',
                'sha256:bbb222', 100, 'media/type')
            cache1.record(
                'ccc333', 'filter1',
                'sha256:ddd444', 200, 'media/type2')
            cache1.save()

            # Load in new instance
            cache2 = LayerCache(path)
            self.assertEqual(2, len(cache2))

            entry = cache2.lookup('aaa111', 'none')
            self.assertIsNotNone(entry)
            self.assertEqual('sha256:bbb222',
                             entry['compressed_digest'])

            entry = cache2.lookup('ccc333', 'filter1')
            self.assertIsNotNone(entry)
            self.assertEqual('sha256:ddd444',
                             entry['compressed_digest'])

    def test_save_not_dirty(self):
        """Test that save is a no-op when no changes made."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')

            cache = LayerCache(path)
            # No records added, save should not create file
            cache.save()
            self.assertFalse(os.path.exists(path))

    def test_save_creates_directory(self):
        """Test that save creates parent directories."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'subdir', 'cache.json')

            cache = LayerCache(path)
            cache.record(
                'aaa111', 'none',
                'sha256:bbb222', 100, 'media/type')
            cache.save()

            self.assertTrue(os.path.exists(path))

    def test_corrupt_cache_file(self):
        """Test that a corrupt cache file is handled gracefully."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')

            # Write garbage
            with open(path, 'w') as f:
                f.write('not json')

            # Should not raise, just warn and start empty
            cache = LayerCache(path)
            self.assertEqual(0, len(cache))

    def test_wrong_version(self):
        """Test that an unknown version is handled gracefully."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')

            with open(path, 'w') as f:
                json.dump({'version': 99, 'layers': {}}, f)

            cache = LayerCache(path)
            self.assertEqual(0, len(cache))

    def test_record_overwrites_existing(self):
        """Test that recording the same diffid overwrites."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')
            cache = LayerCache(path)

            cache.record(
                'aaa111', 'none',
                'sha256:old', 100, 'media/type')
            cache.record(
                'aaa111', 'none',
                'sha256:new', 200, 'media/type2')

            self.assertEqual(1, len(cache))
            entry = cache.lookup('aaa111', 'none')
            self.assertEqual('sha256:new',
                             entry['compressed_digest'])
            self.assertEqual(200, entry['compressed_size'])

    def test_len(self):
        """Test __len__ returns correct count."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'cache.json')
            cache = LayerCache(path)

            self.assertEqual(0, len(cache))
            cache.record(
                'aaa', 'none',
                'sha256:bbb', 1, 'x')
            self.assertEqual(1, len(cache))
            cache.record(
                'ccc', 'none',
                'sha256:ddd', 2, 'x')
            self.assertEqual(2, len(cache))
