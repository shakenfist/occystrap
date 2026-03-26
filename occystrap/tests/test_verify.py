"""Tests for post-write output verification."""

import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from occystrap import constants
from occystrap.outputs.directory import DirWriter
from occystrap.outputs.tarfile import TarWriter
from occystrap.outputs.docker import DockerWriter
from occystrap.outputs.registry import RegistryWriter
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


class TestTarWriterVerify(unittest.TestCase):
    """Test TarWriter.verify() with real tarballs."""

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

    def _process_image(self, writer):
        """Process a config and one layer."""
        writer.process_image_element(
            constants.ImageElement(
                constants.CONFIG_FILE,
                'abc123.json',
                io.BytesIO(self._make_config())))
        writer.process_image_element(
            constants.ImageElement(
                constants.IMAGE_LAYER,
                'def456',
                self._make_layer_tar()))
        writer.finalize()

    def test_verify_passes_for_correct_tarball(self):
        """verify() returns no errors for a correctly
        written tarball."""
        with tempfile.NamedTemporaryFile(
                suffix='.tar', delete=False) as f:
            tar_path = f.name

        try:
            writer = TarWriter(
                'test/image', 'latest', tar_path)
            self._process_image(writer)

            results = writer.verify()
            self.assertFalse(
                results.has_errors,
                'Expected no errors but got: %s'
                % [r['message'] for r in results.results
                   if r['severity'] == 'error'])
        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_verify_full_passes_for_correct_tarball(self):
        """verify(full=True) returns no errors for a
        correctly written tarball."""
        with tempfile.NamedTemporaryFile(
                suffix='.tar', delete=False) as f:
            tar_path = f.name

        try:
            writer = TarWriter(
                'test/image', 'latest', tar_path)
            self._process_image(writer)

            results = writer.verify(full=True)
            self.assertFalse(
                results.has_errors,
                'Expected no errors but got: %s'
                % [r['message'] for r in results.results
                   if r['severity'] == 'error'])
        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_verify_detects_missing_tarball(self):
        """verify() reports error when tarball doesn't
        exist."""
        writer = TarWriter.__new__(TarWriter)
        writer.image_path = '/nonexistent/file.tar'
        writer.tar_manifest = [{
            'Layers': [], 'Config': 'a.json'}]

        results = writer.verify()
        self.assertTrue(results.has_errors)
        self.assertTrue(
            any('Tarball missing' in r['message']
                for r in results.results))

    def test_verify_detects_missing_layer_entry(self):
        """verify() reports error when a layer entry is
        missing from the tarball."""
        with tempfile.NamedTemporaryFile(
                suffix='.tar', delete=False) as f:
            tar_path = f.name

        try:
            writer = TarWriter(
                'test/image', 'latest', tar_path)
            # Only write config, no layers
            writer.process_image_element(
                constants.ImageElement(
                    constants.CONFIG_FILE,
                    'abc123.json',
                    io.BytesIO(self._make_config())))
            # Manually add a fake layer to manifest
            writer.tar_manifest[0]['Layers'].append(
                'missing_layer/layer.tar')
            writer.finalize()

            results = writer.verify()
            self.assertTrue(results.has_errors)
            self.assertTrue(
                any('Layer entry missing'
                    in r['message']
                    for r in results.results))
        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_verify_info_on_success(self):
        """verify() includes info message on success."""
        with tempfile.NamedTemporaryFile(
                suffix='.tar', delete=False) as f:
            tar_path = f.name

        try:
            writer = TarWriter(
                'test/image', 'latest', tar_path)
            self._process_image(writer)

            results = writer.verify()
            info_msgs = [
                r['message'] for r in results.results
                if r['severity'] == 'info']
            self.assertTrue(
                any('verified' in m for m in info_msgs))
        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)


class TestDockerWriterVerify(unittest.TestCase):
    """Test DockerWriter.verify() with mocked Docker API."""

    def test_verify_passes_when_image_exists(self):
        """verify() returns no errors when Docker reports
        the image exists with correct ID."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'Id': 'sha256:abc123'
        }
        mock_session.get.return_value = mock_response

        writer = DockerWriter.__new__(DockerWriter)
        writer.image = 'test/image'
        writer.tag = 'latest'
        writer.socket_path = '/var/run/docker.sock'
        writer._session = mock_session
        writer._tar_manifest = [{
            'Config': 'abc123.json',
            'Layers': [],
        }]

        results = writer.verify()
        self.assertFalse(results.has_errors)
        self.assertTrue(
            any('verified' in r['message']
                for r in results.results
                if r['severity'] == 'info'))

    def test_verify_detects_missing_image(self):
        """verify() reports error when Docker returns
        404."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 404
        mock_session.get.return_value = mock_response

        writer = DockerWriter.__new__(DockerWriter)
        writer.image = 'test/image'
        writer.tag = 'latest'
        writer.socket_path = '/var/run/docker.sock'
        writer._session = mock_session
        writer._tar_manifest = [{
            'Config': 'abc123.json',
            'Layers': [],
        }]

        results = writer.verify()
        self.assertTrue(results.has_errors)
        self.assertTrue(
            any('not found' in r['message']
                for r in results.results))

    def test_verify_detects_id_mismatch(self):
        """verify() reports error when image ID doesn't
        match expected config digest."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'Id': 'sha256:wrong_digest'
        }
        mock_session.get.return_value = mock_response

        writer = DockerWriter.__new__(DockerWriter)
        writer.image = 'test/image'
        writer.tag = 'latest'
        writer.socket_path = '/var/run/docker.sock'
        writer._session = mock_session
        writer._tar_manifest = [{
            'Config': 'abc123.json',
            'Layers': [],
        }]

        results = writer.verify()
        self.assertTrue(results.has_errors)
        self.assertTrue(
            any('mismatch' in r['message']
                for r in results.results))

    def test_verify_warns_on_connection_error(self):
        """verify() warns (not errors) when Docker daemon
        is unreachable."""
        mock_session = mock.MagicMock()
        mock_session.get.side_effect = \
            ConnectionError('refused')

        writer = DockerWriter.__new__(DockerWriter)
        writer.image = 'test/image'
        writer.tag = 'latest'
        writer.socket_path = '/var/run/docker.sock'
        writer._session = mock_session
        writer._tar_manifest = [{
            'Config': 'abc123.json',
            'Layers': [],
        }]

        results = writer.verify()
        self.assertFalse(results.has_errors)
        self.assertTrue(
            any('Cannot connect' in r['message']
                for r in results.results
                if r['severity'] == 'warning'))


class TestRegistryWriterVerify(unittest.TestCase):
    """Test RegistryWriter.verify() with mocked requests."""

    def _make_writer(self):
        """Create a RegistryWriter with mock client."""
        mock_client = mock.MagicMock()
        writer = RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0',
            client=mock_client)
        # Simulate state after finalize()
        writer._config_digest = 'sha256:configabc'
        writer._config_size = 100
        writer._layers = [
            {'digest': 'sha256:layer1',
             'size': 1000,
             'mediaType': 'application/vnd.docker.'
             'image.rootfs.diff.tar.gzip'},
            {'digest': 'sha256:layer2',
             'size': 2000,
             'mediaType': 'application/vnd.docker.'
             'image.rootfs.diff.tar.gzip'},
        ]
        return writer

    def _mock_request_all_ok(self, writer):
        """Mock _request to return success for all calls."""
        manifest_json = {
            'config': {
                'digest': 'sha256:configabc',
            },
            'layers': [
                {'digest': 'sha256:layer1'},
                {'digest': 'sha256:layer2'},
            ],
        }

        def side_effect(method, url, headers=None,
                        data=None, stream=False):
            r = mock.MagicMock()
            if method == 'HEAD':
                r.status_code = 200
            elif method == 'GET' and '/manifests/' in url:
                r.status_code = 200
                r.json.return_value = manifest_json
            else:
                r.status_code = 200
            return r

        writer._request = mock.MagicMock(
            side_effect=side_effect)

    @mock.patch('occystrap.outputs.registry.'
                'util.create_client')
    def test_verify_passes_all_ok(self, mock_create):
        """verify() passes when all blobs and manifest
        are present."""
        mock_create.return_value = (
            mock.MagicMock(), None)
        writer = self._make_writer()
        self._mock_request_all_ok(writer)

        results = writer.verify()
        self.assertFalse(
            results.has_errors,
            'Expected no errors but got: %s'
            % [r['message'] for r in results.results
               if r['severity'] == 'error'])
        self.assertTrue(
            any('verified' in r['message']
                for r in results.results
                if r['severity'] == 'info'))

    @mock.patch('occystrap.outputs.registry.'
                'util.create_client')
    def test_verify_detects_missing_layer(
            self, mock_create):
        """verify() reports error when a layer blob
        returns 404."""
        mock_create.return_value = (
            mock.MagicMock(), None)
        writer = self._make_writer()

        def side_effect(method, url, headers=None,
                        data=None, stream=False):
            r = mock.MagicMock()
            if method == 'HEAD' \
                    and 'sha256:layer2' in url:
                r.status_code = 404
            elif method == 'HEAD':
                r.status_code = 200
            elif method == 'GET':
                r.status_code = 200
                r.json.return_value = {
                    'config': {
                        'digest': 'sha256:configabc'},
                    'layers': [
                        {'digest': 'sha256:layer1'},
                        {'digest': 'sha256:layer2'}],
                }
            else:
                r.status_code = 200
            return r

        writer._request = mock.MagicMock(
            side_effect=side_effect)

        results = writer.verify()
        self.assertTrue(results.has_errors)
        self.assertTrue(
            any('Layer blob not found'
                in r['message']
                for r in results.results))

    @mock.patch('occystrap.outputs.registry.'
                'util.create_client')
    def test_verify_detects_manifest_config_mismatch(
            self, mock_create):
        """verify() reports error when manifest config
        digest doesn't match."""
        mock_create.return_value = (
            mock.MagicMock(), None)
        writer = self._make_writer()

        def side_effect(method, url, headers=None,
                        data=None, stream=False):
            r = mock.MagicMock()
            if method == 'HEAD':
                r.status_code = 200
            elif method == 'GET':
                r.status_code = 200
                r.json.return_value = {
                    'config': {
                        'digest': 'sha256:WRONG'},
                    'layers': [
                        {'digest': 'sha256:layer1'},
                        {'digest': 'sha256:layer2'}],
                }
            else:
                r.status_code = 200
            return r

        writer._request = mock.MagicMock(
            side_effect=side_effect)

        results = writer.verify()
        self.assertTrue(results.has_errors)
        self.assertTrue(
            any('config digest mismatch'
                in r['message']
                for r in results.results))

    @mock.patch('occystrap.outputs.registry.'
                'util.create_client')
    def test_verify_detects_missing_manifest(
            self, mock_create):
        """verify() reports error when manifest GET
        returns 404."""
        mock_create.return_value = (
            mock.MagicMock(), None)
        writer = self._make_writer()

        def side_effect(method, url, headers=None,
                        data=None, stream=False):
            r = mock.MagicMock()
            if method == 'HEAD':
                r.status_code = 200
            elif method == 'GET' \
                    and '/manifests/' in url:
                r.status_code = 404
            else:
                r.status_code = 200
            return r

        writer._request = mock.MagicMock(
            side_effect=side_effect)

        results = writer.verify()
        self.assertTrue(results.has_errors)
        self.assertTrue(
            any('Manifest not found'
                in r['message']
                for r in results.results))

    @mock.patch('occystrap.outputs.registry.'
                'util.create_client')
    def test_verify_warns_on_network_error(
            self, mock_create):
        """verify() warns (not errors) when network
        fails during verification."""
        mock_create.return_value = (
            mock.MagicMock(), None)
        writer = self._make_writer()
        writer._request = mock.MagicMock(
            side_effect=ConnectionError('refused'))

        results = writer.verify()
        self.assertFalse(results.has_errors)
        self.assertTrue(
            any('Verification failed'
                in r['message']
                for r in results.results
                if r['severity'] == 'warning'))
