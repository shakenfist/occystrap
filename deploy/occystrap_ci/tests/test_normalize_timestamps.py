import hashlib
import io
import os
import tarfile
import tempfile
import unittest

from occystrap import constants
from occystrap.outputs import tarfile as output_tarfile
from occystrap.filters import TimestampNormalizer


class NormalizeTimestampsTestCase(unittest.TestCase):
    def test_normalize_timestamps_changes_hash(self):
        """Test that normalizing timestamps changes the layer hash."""
        # Create a simple layer tarball with files that have timestamps
        with tempfile.NamedTemporaryFile(delete=False) as layer_tf:
            try:
                with tarfile.open(fileobj=layer_tf, mode='w') as layer_tar:
                    # Add a test file with a specific timestamp
                    ti = tarfile.TarInfo('test.txt')
                    ti.size = 11
                    ti.mtime = 1609459200  # 2021-01-01 00:00:00
                    layer_tar.addfile(ti, io.BytesIO(b'hello world'))

                layer_tf.flush()
                layer_tf.seek(0)

                # Calculate original hash
                original_hash = hashlib.sha256()
                data = layer_tf.read()
                original_hash.update(data)
                original_sha = original_hash.hexdigest()

                # Create TarWriter wrapped with TimestampNormalizer
                with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                    try:
                        tw = output_tarfile.TarWriter(
                            'test/image', 'latest', output_tf.name)
                        normalizer = TimestampNormalizer(tw, timestamp=0)

                        # Normalize the layer using the filter's internal method
                        layer_tf.seek(0)
                        normalized_data, new_sha = normalizer._normalize_layer(
                            open(layer_tf.name, 'rb'))

                        # Verify the hash changed
                        self.assertNotEqual(original_sha, new_sha)

                        # Verify all timestamps in the normalized layer are 0
                        normalized_data.seek(0)
                        with tarfile.open(fileobj=normalized_data,
                                          mode='r') as normalized_tar:
                            for member in normalized_tar:
                                self.assertEqual(0, member.mtime)

                        # Clean up
                        normalized_data.close()
                        os.unlink(normalized_data.name)

                    finally:
                        if os.path.exists(output_tf.name):
                            os.unlink(output_tf.name)

            finally:
                os.unlink(layer_tf.name)

    def test_normalize_timestamps_same_hash_for_same_content(self):
        """Test that two layers with same content but different timestamps
        produce the same hash after normalization.
        """
        def create_layer_with_timestamp(mtime):
            tf = tempfile.NamedTemporaryFile(delete=False)
            with tarfile.open(fileobj=tf, mode='w') as layer_tar:
                ti = tarfile.TarInfo('test.txt')
                ti.size = 11
                ti.mtime = mtime
                layer_tar.addfile(ti, io.BytesIO(b'hello world'))
            tf.flush()
            tf.close()
            return tf.name

        layer1_path = create_layer_with_timestamp(1609459200)
        layer2_path = create_layer_with_timestamp(1704067200)

        try:
            with tempfile.NamedTemporaryFile(delete=False) as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    normalizer = TimestampNormalizer(tw, timestamp=0)

                    # Normalize both layers
                    data1, sha1 = normalizer._normalize_layer(
                        open(layer1_path, 'rb'))
                    data2, sha2 = normalizer._normalize_layer(
                        open(layer2_path, 'rb'))

                    # Verify same content with different original timestamps
                    # produces same hash after normalization
                    self.assertEqual(sha1, sha2)

                    # Clean up
                    data1.close()
                    data2.close()
                    os.unlink(data1.name)
                    os.unlink(data2.name)

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer1_path)
            os.unlink(layer2_path)

    def test_filter_integration_with_tarwriter(self):
        """Test that TimestampNormalizer properly integrates with TarWriter."""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as \
                output_tf:
            try:
                tw = output_tarfile.TarWriter(
                    'test/image', 'latest', output_tf.name)
                normalizer = TimestampNormalizer(tw, timestamp=42)

                # Create a test layer
                with tempfile.NamedTemporaryFile(delete=False) as layer_tf:
                    try:
                        with tarfile.open(fileobj=layer_tf, mode='w') as \
                                layer_tar:
                            ti = tarfile.TarInfo('test.txt')
                            ti.size = 11
                            ti.mtime = 1609459200
                            layer_tar.addfile(ti, io.BytesIO(b'hello world'))

                        layer_tf.flush()
                        layer_tf.seek(0)

                        # Process the layer through the filter
                        normalizer.process_image_element(
                            constants.IMAGE_LAYER, 'originalhash',
                            open(layer_tf.name, 'rb'))

                        # Finalize
                        normalizer.finalize()

                        # Verify the manifest contains a layer path
                        self.assertEqual(1, len(tw.tar_manifest[0]['Layers']))

                        # Verify the layer path doesn't use the original hash
                        layer_path = tw.tar_manifest[0]['Layers'][0]
                        self.assertNotIn('originalhash', layer_path)

                    finally:
                        os.unlink(layer_tf.name)

            finally:
                if os.path.exists(output_tf.name):
                    os.unlink(output_tf.name)
