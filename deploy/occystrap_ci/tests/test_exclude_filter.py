import hashlib
import io
import os
import tarfile
import tempfile
import unittest

from occystrap import constants
from occystrap.outputs import tarfile as output_tarfile
from occystrap.filters import ExcludeFilter


class ExcludeFilterTestCase(unittest.TestCase):
    def _create_layer_with_files(self, files):
        """Create a layer tarball containing the specified files.

        Args:
            files: Dict of {path: content} pairs.

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
                    layer_tar.addfile(ti, io.BytesIO(data))
        tf.flush()
        tf.close()
        return tf.name

    def _get_layer_files(self, layer_path):
        """Get list of file paths in a layer tarball."""
        with open(layer_path, 'rb') as f:
            with tarfile.open(fileobj=f, mode='r') as tar:
                return [m.name for m in tar]

    def test_exclude_single_pattern(self):
        """Test excluding files with a single glob pattern."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
            'app/utils.py': 'def util(): pass',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*'])

                    with open(layer_path, 'rb') as f:
                        filtered_data, _ = exclude_filter._filter_layer(f)

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

    def test_exclude_multiple_patterns(self):
        """Test excluding files with multiple glob patterns."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
            '.git/config': '[core]',
            '.git/objects/abc123': b'\x00',
            'README.md': '# Project',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*', '.git/*'])

                    with open(layer_path, 'rb') as f:
                        filtered_data, _ = exclude_filter._filter_layer(f)

                    filtered_data.seek(0)
                    with tarfile.open(fileobj=filtered_data, mode='r') as tar:
                        files = [m.name for m in tar]

                    self.assertIn('app/main.py', files)
                    self.assertIn('README.md', files)
                    pycache_file = 'app/__pycache__/main.cpython-311.pyc'
                    self.assertNotIn(pycache_file, files)
                    self.assertNotIn('.git/config', files)
                    self.assertNotIn('.git/objects/abc123', files)

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_exclude_no_matches(self):
        """Test that excluding a pattern that doesn't match leaves files."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/utils.py': 'def util(): pass',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*.nonexistent'])

                    with open(layer_path, 'rb') as f:
                        filtered_data, _ = exclude_filter._filter_layer(f)

                    filtered_data.seek(0)
                    with tarfile.open(fileobj=filtered_data, mode='r') as tar:
                        files = [m.name for m in tar]

                    self.assertIn('app/main.py', files)
                    self.assertIn('app/utils.py', files)
                    self.assertEqual(2, len(files))

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_exclude_sha_recalculation(self):
        """Test that excluding files changes the SHA hash."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
        })

        try:
            with open(layer_path, 'rb') as f:
                original_hash = hashlib.sha256()
                original_hash.update(f.read())
                original_sha = original_hash.hexdigest()

            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*'])

                    with open(layer_path, 'rb') as f:
                        result = exclude_filter._filter_layer(f)
                        filtered_data, new_sha = result

                    self.assertNotEqual(original_sha, new_sha)

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_exclude_integration_with_tarwriter(self):
        """Test that ExcludeFilter properly integrates with TarWriter."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                    output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*'])

                    exclude_filter.process_image_element(
                        constants.IMAGE_LAYER, 'originalhash',
                        open(layer_path, 'rb'))

                    exclude_filter.finalize()

                    self.assertEqual(1, len(tw.tar_manifest[0]['Layers']))

                    layer_name = tw.tar_manifest[0]['Layers'][0]
                    self.assertNotIn('originalhash', layer_name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_exclude_preserves_directories(self):
        """Test that directories not matching patterns are preserved."""
        layer_path = self._create_layer_with_files({
            'app': None,
            'app/main.py': 'print("hello")',
            'app/__pycache__': None,
            'app/__pycache__/main.cpython-311.pyc': b'\x00\x00',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(
                        tw, patterns=['*__pycache__*'])

                    with open(layer_path, 'rb') as f:
                        filtered_data, _ = exclude_filter._filter_layer(f)

                    filtered_data.seek(0)
                    with tarfile.open(fileobj=filtered_data, mode='r') as tar:
                        files = [m.name for m in tar]

                    self.assertIn('app', files)
                    self.assertIn('app/main.py', files)
                    self.assertNotIn('app/__pycache__', files)
                    pycache_file = 'app/__pycache__/main.cpython-311.pyc'
                    self.assertNotIn(pycache_file, files)

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)

    def test_exclude_pyc_files(self):
        """Test excluding .pyc files with glob pattern."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/main.pyc': b'\x00\x00',
            'app/utils.py': 'def util(): pass',
            'app/utils.pyc': b'\x00\x00',
        })

        try:
            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    exclude_filter = ExcludeFilter(tw, patterns=['*.pyc'])

                    with open(layer_path, 'rb') as f:
                        filtered_data, _ = exclude_filter._filter_layer(f)

                    filtered_data.seek(0)
                    with tarfile.open(fileobj=filtered_data, mode='r') as tar:
                        files = [m.name for m in tar]

                    self.assertIn('app/main.py', files)
                    self.assertIn('app/utils.py', files)
                    self.assertNotIn('app/main.pyc', files)
                    self.assertNotIn('app/utils.pyc', files)

                    filtered_data.close()
                    os.unlink(filtered_data.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)
