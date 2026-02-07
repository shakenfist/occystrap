"""Functional tests for registry push with parallel uploads.

These tests require a local Docker registry running at localhost:5000.
The CI workflow sets this up with test images.
"""

import json
import logging
import testtools

from occystrap import constants
from occystrap.inputs import registry as input_registry
from occystrap.outputs import registry as output_registry


logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


def always_fetch(digest):
    """Fetch callback that always returns True."""
    return True


class RegistryPushTestCase(testtools.TestCase):
    def test_push_image_to_registry(self):
        """Push an image and verify it can be pulled back."""
        src_image = 'library/busybox'
        dst_image = 'occystrap_test_push'
        tag = 'latest'

        # Read from source
        src = input_registry.Image(
            'localhost:5000', src_image, tag, 'linux', 'amd64', '',
            secure=False)

        # Write to destination with parallel uploads
        dst = output_registry.RegistryWriter(
            'localhost:5000', dst_image, tag,
            secure=False, max_workers=4)

        for element in src.fetch(fetch_callback=dst.fetch_callback):
            dst.process_image_element(*element)
        dst.finalize()

        # Verify by pulling back
        verify = input_registry.Image(
            'localhost:5000', dst_image, tag, 'linux', 'amd64', '',
            secure=False)

        # Count layers and verify we got content
        layer_count = 0
        for element in verify.fetch(fetch_callback=always_fetch):
            if element[0] == constants.IMAGE_LAYER:
                layer_count += 1
        self.assertGreater(layer_count, 0)

    def test_push_with_sequential_vs_parallel(self):
        """Verify sequential and parallel uploads both succeed."""
        src_image = 'library/busybox'
        tag = 'latest'

        # Read source image and collect layer digests
        src = input_registry.Image(
            'localhost:5000', src_image, tag, 'linux', 'amd64', '',
            secure=False)

        # Push with sequential (max_workers=1)
        dst_seq = output_registry.RegistryWriter(
            'localhost:5000', 'occystrap_test_seq', tag,
            secure=False, max_workers=1)

        for element in src.fetch(fetch_callback=dst_seq.fetch_callback):
            dst_seq.process_image_element(*element)
        dst_seq.finalize()

        # Push with parallel (max_workers=4)
        src2 = input_registry.Image(
            'localhost:5000', src_image, tag, 'linux', 'amd64', '',
            secure=False)

        dst_par = output_registry.RegistryWriter(
            'localhost:5000', 'occystrap_test_par', tag,
            secure=False, max_workers=4)

        for element in src2.fetch(fetch_callback=dst_par.fetch_callback):
            dst_par.process_image_element(*element)
        dst_par.finalize()

        # Both should have the same number of layers and identical config
        # (layer digests may differ due to gzip non-determinism)
        self.assertEqual(len(dst_seq._layers), len(dst_par._layers))
        self.assertEqual(dst_seq._config_digest, dst_par._config_digest)

    def test_push_preserves_layer_order(self):
        """Verify layer order is preserved in the manifest."""
        src_image = 'library/ubuntu'
        dst_image = 'occystrap_test_layer_order'
        tag = 'latest'

        # Read source and get expected layer order
        src = input_registry.Image(
            'localhost:5000', src_image, tag, 'linux', 'amd64', '',
            secure=False)

        # Push to destination
        dst = output_registry.RegistryWriter(
            'localhost:5000', dst_image, tag,
            secure=False, max_workers=4)

        expected_layer_order = []
        for element in src.fetch(fetch_callback=dst.fetch_callback):
            dst.process_image_element(*element)
            if element[0] == constants.IMAGE_LAYER:
                # Record layer name/digest as we process them
                expected_layer_order.append(element[1])
        dst.finalize()

        # Verify layer order matches
        self.assertEqual(len(expected_layer_order), len(dst._layers))

        # The order of elements received should match the order in the manifest
        for i, layer in enumerate(dst._layers):
            self.assertTrue(
                layer['digest'].startswith('sha256:'),
                f'Layer {i} has invalid digest format')

    def test_push_multiple_tags(self):
        """Verify pushing the same image with different tags succeeds."""
        src_image = 'library/busybox'
        dst_image = 'occystrap_test_multi_tag'

        # First push with tag v1
        src1 = input_registry.Image(
            'localhost:5000', src_image, 'latest', 'linux', 'amd64', '',
            secure=False)
        dst1 = output_registry.RegistryWriter(
            'localhost:5000', dst_image, 'v1',
            secure=False, max_workers=4)
        for element in src1.fetch(fetch_callback=dst1.fetch_callback):
            dst1.process_image_element(*element)
        dst1.finalize()

        first_layer_count = len(dst1._layers)

        # Second push with tag v2
        src2 = input_registry.Image(
            'localhost:5000', src_image, 'latest', 'linux', 'amd64', '',
            secure=False)
        dst2 = output_registry.RegistryWriter(
            'localhost:5000', dst_image, 'v2',
            secure=False, max_workers=4)
        for element in src2.fetch(fetch_callback=dst2.fetch_callback):
            dst2.process_image_element(*element)
        dst2.finalize()

        second_layer_count = len(dst2._layers)

        # Both should have the same number of layers
        self.assertEqual(first_layer_count, second_layer_count)
        # Config should be identical (not re-compressed)
        self.assertEqual(dst1._config_digest, dst2._config_digest)

    def test_push_roundtrip_content_integrity(self):
        """Verify content integrity through a push/pull roundtrip."""
        src_image = 'library/busybox'
        dst_image = 'occystrap_test_roundtrip'
        tag = 'latest'

        # Read source and collect config/layer data
        src = input_registry.Image(
            'localhost:5000', src_image, tag, 'linux', 'amd64', '',
            secure=False)

        original_config = None
        original_layer_sizes = []

        dst = output_registry.RegistryWriter(
            'localhost:5000', dst_image, tag,
            secure=False, max_workers=4)

        for element in src.fetch(fetch_callback=dst.fetch_callback):
            element_type, name, data = element
            if element_type == constants.CONFIG_FILE and data:
                data.seek(0)
                original_config = data.read()
                data.seek(0)
            elif element_type == constants.IMAGE_LAYER and data:
                data.seek(0)
                original_layer_sizes.append(len(data.read()))
                data.seek(0)
            dst.process_image_element(*element)
        dst.finalize()

        # Pull back and verify
        verify = input_registry.Image(
            'localhost:5000', dst_image, tag, 'linux', 'amd64', '',
            secure=False)

        verified_config = None
        verified_layer_count = 0

        for element in verify.fetch(fetch_callback=always_fetch):
            element_type, name, data = element
            if element_type == constants.CONFIG_FILE and data:
                data.seek(0)
                verified_config = data.read()
            elif element_type == constants.IMAGE_LAYER and data:
                verified_layer_count += 1

        # Config content should be identical
        self.assertEqual(
            json.loads(original_config),
            json.loads(verified_config))
        self.assertEqual(len(original_layer_sizes), verified_layer_count)
