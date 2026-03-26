"""Tests for post-write output verification."""

import io
import json
import os
import tarfile
import tempfile
import unittest

from occystrap import constants
from occystrap.outputs.directory import DirWriter
from occystrap.outputs.base import ImageOutput


class TestImageOutputVerify(unittest.TestCase):
    """Test the base ImageOutput.verify() default."""

    def test_default_verify_returns_empty_results(self):
        """Base verify() returns CheckResults with no errors."""
        # Create a minimal concrete subclass
        class MinimalOutput(ImageOutput):
            def fetch_callback(self, digest):
                return True

            def process_image_element(self, element):
                pass

            def finalize(self):
                pass

        output = MinimalOutput()
        results = output.verify()
        self.assertFalse(results.has_errors)
        self.assertEqual(0, results.error_count)

    def test_default_verify_full_returns_empty_results(self):
        """Base verify(full=True) also returns no errors."""
        class MinimalOutput(ImageOutput):
            def fetch_callback(self, digest):
                return True

            def process_image_element(self, element):
                pass

            def finalize(self):
                pass

        output = MinimalOutput()
        results = output.verify(full=True)
        self.assertFalse(results.has_errors)


class TestDirWriterVerify(unittest.TestCase):
    """Test DirWriter.verify() with real files on disk."""

    def _make_layer_tar(self, content=b'hello world'):
        """Create a minimal valid tar in memory."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tf:
            info = tarfile.TarInfo(name='test.txt')
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        buf.seek(0)
        return buf

    def _make_config(self):
        """Create a minimal config JSON."""
        return json.dumps({
            'rootfs': {
                'type': 'layers',
                'diff_ids': ['sha256:abc123']
            }
        }).encode('utf-8')

    def _process_image(self, writer, config_data=None,
                       layer_data=None):
        """Process a config and one layer through the writer."""
        if config_data is None:
            config_data = self._make_config()
        if layer_data is None:
            layer_data = self._make_layer_tar()

        writer.process_image_element(
            constants.ImageElement(
                constants.CONFIG_FILE,
                'abc123.json',
                io.BytesIO(config_data)))
        writer.process_image_element(
            constants.ImageElement(
                constants.IMAGE_LAYER,
                'def456',
                layer_data))
        writer.finalize()

    def test_verify_passes_for_correct_output(self):
        """verify() returns no errors for a correctly
        written image."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            results = writer.verify()
            self.assertFalse(
                results.has_errors,
                'Expected no errors but got: %s'
                % [r['message'] for r in results.results
                   if r['severity'] == 'error'])

    def test_verify_full_passes_for_correct_output(self):
        """verify(full=True) returns no errors for a
        correctly written image."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            results = writer.verify(full=True)
            self.assertFalse(
                results.has_errors,
                'Expected no errors but got: %s'
                % [r['message'] for r in results.results
                   if r['severity'] == 'error'])

    def test_verify_detects_missing_manifest(self):
        """verify() reports error when manifest is missing."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            # Delete the manifest
            manifest_path = os.path.join(
                d, 'manifest.json')
            os.unlink(manifest_path)

            results = writer.verify()
            self.assertTrue(results.has_errors)
            self.assertTrue(
                any('Manifest file missing'
                    in r['message']
                    for r in results.results))

    def test_verify_detects_missing_layer(self):
        """verify() reports error when a layer file is
        missing."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            # Delete the layer file
            layer_path = os.path.join(
                d, 'def456', 'layer.tar')
            os.unlink(layer_path)

            results = writer.verify()
            self.assertTrue(results.has_errors)
            self.assertTrue(
                any('Layer file missing'
                    in r['message']
                    for r in results.results))

    def test_verify_detects_wrong_layer_size(self):
        """verify() reports error when layer size doesn't
        match expectation."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            # Overwrite layer with different content
            layer_path = os.path.join(
                d, 'def456', 'layer.tar')
            with open(layer_path, 'wb') as f:
                f.write(b'corrupted')

            results = writer.verify()
            self.assertTrue(results.has_errors)
            self.assertTrue(
                any('Layer size mismatch'
                    in r['message']
                    for r in results.results))

    def test_verify_detects_missing_config(self):
        """verify() reports error when config file is
        missing."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            # Delete the config file
            config_path = os.path.join(
                d, 'abc123.json')
            os.unlink(config_path)

            results = writer.verify()
            self.assertTrue(results.has_errors)
            self.assertTrue(
                any('Config file missing'
                    in r['message']
                    for r in results.results))

    def test_verify_full_detects_corrupt_tar(self):
        """verify(full=True) reports error when a layer
        is not a valid tarball."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            # Overwrite layer with invalid tar data.
            # Use random-ish bytes (not all zeros, since
            # all-zeros is a valid empty tar).
            layer_path = os.path.join(
                d, 'def456', 'layer.tar')
            original_size = os.path.getsize(layer_path)
            with open(layer_path, 'wb') as f:
                f.write(b'\xff\xfe\xfd' * (
                    original_size // 3 + 1))

            results = writer.verify(full=True)
            self.assertTrue(results.has_errors)
            self.assertTrue(
                any('not a valid tar'
                    in r['message']
                    for r in results.results))

    def test_verify_info_message_on_success(self):
        """verify() includes an info message when all
        checks pass."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d)
            self._process_image(writer)

            results = writer.verify()
            info_msgs = [
                r['message'] for r in results.results
                if r['severity'] == 'info']
            self.assertTrue(
                any('verified' in m for m in info_msgs))

    def test_verify_with_unique_names(self):
        """verify() works with unique_names=True manifest
        naming."""
        with tempfile.TemporaryDirectory() as d:
            writer = DirWriter(
                'test/image', 'latest', d,
                unique_names=True)
            self._process_image(writer)

            results = writer.verify()
            self.assertFalse(
                results.has_errors,
                'Expected no errors but got: %s'
                % [r['message'] for r in results.results
                   if r['severity'] == 'error'])
