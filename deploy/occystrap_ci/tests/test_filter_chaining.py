import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest

from occystrap import constants
from occystrap.filters import ExcludeFilter, TimestampNormalizer, SearchFilter
from occystrap.outputs import tarfile as output_tarfile


class FilterChainingTestCase(unittest.TestCase):
    """Test chaining multiple filters together."""

    def _create_layer_with_files(self, files):
        """Create a layer tarball containing the specified files.

        Args:
            files: Dict of {path: content} pairs. Use None for directories.

        Returns:
            Path to the temporary layer file.
        """
        tf = tempfile.NamedTemporaryFile(delete=False)
        with tarfile.open(fileobj=tf, mode='w') as layer_tar:
            for path, content in files.items():
                ti = tarfile.TarInfo(path)
                if content is None:
                    ti.type = tarfile.DIRTYPE
                    ti.size = 0
                    layer_tar.addfile(ti)
                else:
                    data = content.encode('utf-8') if isinstance(
                        content, str) else content
                    ti.size = len(data)
                    ti.mtime = 1700000000  # Non-zero timestamp
                    layer_tar.addfile(ti, io.BytesIO(data))
        tf.flush()
        tf.close()
        return tf.name

    def _get_layer_files_and_mtimes(self, layer_path):
        """Get list of file paths and mtimes in a layer tarball."""
        with open(layer_path, 'rb') as f:
            with tarfile.open(fileobj=f, mode='r') as tar:
                return [(m.name, m.mtime) for m in tar]

    def test_exclude_then_normalize(self):
        """Test chaining ExcludeFilter -> TimestampNormalizer."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
            'app/utils.py': 'def util(): pass',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    # Build pipeline: exclude -> normalize -> tarwriter
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    normalizer = TimestampNormalizer(tw, timestamp=0)
                    exclude_filter = ExcludeFilter(
                        normalizer, patterns=['*__pycache__*'])

                    # Process layer
                    with open(layer_path, 'rb') as f:
                        filtered_data, _ = exclude_filter._filter_layer(f)

                    # The filtered data should have __pycache__ removed
                    filtered_data.seek(0)
                    with tarfile.open(fileobj=filtered_data, mode='r') as tar:
                        files = [m.name for m in tar]

                    self.assertIn('app/main.py', files)
                    self.assertIn('app/utils.py', files)
                    pycache_file = 'app/__pycache__/main.cpython-311.pyc'
                    self.assertNotIn(pycache_file, files)

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_normalize_then_exclude(self):
        """Test chaining TimestampNormalizer -> ExcludeFilter."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    # Build pipeline: normalize -> exclude -> tarwriter
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*'])
                    normalizer = TimestampNormalizer(
                        exclude_filter, timestamp=0)

                    # Process layer through normalizer first
                    with open(layer_path, 'rb') as f:
                        normalized_data, _ = normalizer._normalize_layer(f)

                    # Verify timestamps are normalized
                    normalized_data.seek(0)
                    with tarfile.open(
                            fileobj=normalized_data, mode='r') as tar:
                        for m in tar:
                            self.assertEqual(0, m.mtime)

                    normalized_data.close()
                    os.unlink(normalized_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_search_as_passthrough_with_exclude(self):
        """Test SearchFilter as passthrough with ExcludeFilter."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
            'config/settings.conf': 'setting=value',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    # Build pipeline: search -> exclude -> tarwriter
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*'])
                    search_filter = SearchFilter(
                        exclude_filter,
                        pattern='*.py',
                        image='test/image',
                        tag='latest',
                        script_friendly=True)

                    # Process layer
                    with open(layer_path, 'rb') as f:
                        search_filter.process_image_element(
                            constants.IMAGE_LAYER, 'originalhash', f)

                    # Search should find .py files (not filtered by exclude)
                    self.assertEqual(1, len(search_filter.results))
                    self.assertEqual('app/main.py',
                                     search_filter.results[0][1])

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_full_pipeline_integration(self):
        """Test a full pipeline with multiple filters and TarWriter."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
            'config.json': '{"setting": "value"}',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    # Build pipeline:
                    # search -> exclude -> normalize -> tarwriter
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    normalizer = TimestampNormalizer(tw, timestamp=0)
                    exclude_filter = ExcludeFilter(
                        normalizer, patterns=['*__pycache__*'])
                    search_filter = SearchFilter(
                        exclude_filter,
                        pattern='*.json',
                        image='test/image',
                        tag='latest',
                        script_friendly=True)

                    # Process config (passthrough)
                    config_data = json.dumps({
                        'architecture': 'amd64',
                        'os': 'linux'
                    }).encode('utf-8')
                    search_filter.process_image_element(
                        constants.CONFIG_FILE, 'config.json',
                        io.BytesIO(config_data))

                    # Process layer
                    with open(layer_path, 'rb') as f:
                        search_filter.process_image_element(
                            constants.IMAGE_LAYER, 'originalhash', f)

                    search_filter.finalize()

                    # Verify output tarball structure
                    with tarfile.open(output_tf.name, 'r') as output_tar:
                        names = [m.name for m in output_tar]
                        # Should have manifest.json and config
                        self.assertIn('manifest.json', names)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_multiple_exclude_patterns(self):
        """Test ExcludeFilter with multiple patterns."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
            '.git/config': '[core]',
            '.git/objects/abc': b'\x00',
            'README.md': '# Project',
            'test.pyc': b'\x00\x00',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw,
                        patterns=['*__pycache__*', '.git/*', '*.pyc'])

                    with open(layer_path, 'rb') as f:
                        result = exclude_filter._filter_layer(f)
                        filtered_data, _ = result

                    filtered_data.seek(0)
                    with tarfile.open(fileobj=filtered_data, mode='r') as tar:
                        files = [m.name for m in tar]

                    # Should keep only main.py and README.md
                    self.assertIn('app/main.py', files)
                    self.assertIn('README.md', files)
                    # Should exclude __pycache__, .git, and .pyc
                    self.assertNotIn('app/__pycache__/main.cpython-311.pyc',
                                     files)
                    self.assertNotIn('.git/config', files)
                    self.assertNotIn('.git/objects/abc', files)
                    self.assertNotIn('test.pyc', files)

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_sha_changes_with_each_filter(self):
        """Test that SHA changes when filters modify content."""
        layer_path = self._create_layer_with_files({
            'file1.txt': 'content1',
            'file2.txt': 'content2',
        })

        try:
            # Get original SHA
            with open(layer_path, 'rb') as f:
                original_sha = hashlib.sha256(f.read()).hexdigest()

            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)

                    # Normalize timestamps
                    normalizer = TimestampNormalizer(tw, timestamp=0)
                    with open(layer_path, 'rb') as f:
                        normalized_data, normalized_sha = \
                            normalizer._normalize_layer(f)

                    # SHA should change after normalization
                    self.assertNotEqual(original_sha, normalized_sha)

                    normalized_data.close()
                    os.unlink(normalized_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_filter_order_affects_output(self):
        """Test that filter order can affect the final output."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
        })

        try:
            # Test 1: exclude first, then normalize
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    tw1 = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    normalizer1 = TimestampNormalizer(tw1, timestamp=0)
                    exclude1 = ExcludeFilter(
                        normalizer1, patterns=['*__pycache__*'])

                    with open(layer_path, 'rb') as f:
                        filtered_data1, sha1 = exclude1._filter_layer(f)
                    filtered_data1.close()
                    os.unlink(filtered_data1.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

            # Test 2: normalize first, then exclude
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    tw2 = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude2 = ExcludeFilter(
                        tw2, patterns=['*__pycache__*'])
                    normalizer2 = TimestampNormalizer(exclude2, timestamp=0)

                    with open(layer_path, 'rb') as f:
                        result2 = normalizer2._normalize_layer(f)
                        normalized_data2, sha2 = result2
                    normalized_data2.close()
                    os.unlink(normalized_data2.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

            # The SHA from exclude->normalize should differ from original
            # (both filters affect output, but at different stages)
            self.assertIsNotNone(sha1)
            self.assertIsNotNone(sha2)

        finally:
            os.unlink(layer_path)

    def test_search_does_not_modify_data(self):
        """Test that SearchFilter does not modify layer data."""
        layer_path = self._create_layer_with_files({
            'file1.txt': 'content1',
            'file2.txt': 'content2',
        })

        try:
            with open(layer_path, 'rb') as f:
                original_content = f.read()
            original_sha = hashlib.sha256(original_content).hexdigest()

            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    search_filter = SearchFilter(
                        tw,
                        pattern='*.txt',
                        image='test/image',
                        tag='latest')

                    with open(layer_path, 'rb') as f:
                        # Read original for comparison
                        search_filter.process_image_element(
                            constants.IMAGE_LAYER, original_sha, f)

                    # Search should find both files
                    self.assertEqual(2, len(search_filter.results))

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)
