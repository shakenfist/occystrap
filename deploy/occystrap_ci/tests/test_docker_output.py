import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from occystrap import constants
from occystrap.outputs import docker as output_docker


class DockerWriterTestCase(unittest.TestCase):
    def test_initialization(self):
        """Test DockerWriter initializes with correct attributes."""
        writer = output_docker.DockerWriter('myimage', 'v1.0')
        try:
            self.assertEqual('myimage', writer.image)
            self.assertEqual('v1.0', writer.tag)
            self.assertEqual('/var/run/docker.sock', writer.socket_path)
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    def test_initialization_custom_socket(self):
        """Test DockerWriter accepts custom socket path."""
        writer = output_docker.DockerWriter(
            'myimage', 'v1.0',
            socket_path='/run/podman/podman.sock')
        try:
            self.assertEqual('/run/podman/podman.sock', writer.socket_path)
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    def test_fetch_callback_always_true(self):
        """Test that fetch_callback always returns True."""
        writer = output_docker.DockerWriter('myimage', 'v1.0')
        try:
            self.assertTrue(writer.fetch_callback('sha256:abc123'))
            self.assertTrue(writer.fetch_callback('sha256:def456'))
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    def test_socket_url_encoding(self):
        """Test that socket path is correctly URL-encoded."""
        writer = output_docker.DockerWriter('myimage', 'v1.0')
        try:
            url = writer._socket_url('/images/load')
            self.assertIn('%2Fvar%2Frun%2Fdocker.sock', url)
            self.assertTrue(url.endswith('/images/load'))
            self.assertTrue(url.startswith('http+unix://'))
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    def test_process_config_file(self):
        """Test processing a config file element."""
        writer = output_docker.DockerWriter('test/image', 'latest')
        try:
            config_data = json.dumps({
                'architecture': 'amd64',
                'os': 'linux'
            }).encode('utf-8')

            writer.process_image_element(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data))

            self.assertEqual('config.json', writer._tar_manifest[0]['Config'])
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    def test_process_image_layer(self):
        """Test processing an image layer element."""
        writer = output_docker.DockerWriter('test/image', 'latest')
        try:
            # Create a simple layer tarball
            layer_tf = tempfile.NamedTemporaryFile(delete=False)
            try:
                with tarfile.open(fileobj=layer_tf, mode='w') as layer_tar:
                    ti = tarfile.TarInfo('test.txt')
                    data = b'test content'
                    ti.size = len(data)
                    layer_tar.addfile(ti, io.BytesIO(data))
                layer_tf.close()

                with open(layer_tf.name, 'rb') as f:
                    writer.process_image_element(
                        constants.IMAGE_LAYER,
                        'sha256_abc123',
                        f)

                self.assertEqual(
                    ['sha256_abc123/layer.tar'],
                    writer._tar_manifest[0]['Layers'])
            finally:
                os.unlink(layer_tf.name)
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    def test_manifest_repo_tags(self):
        """Test that manifest contains correct RepoTags."""
        writer = output_docker.DockerWriter('myorg/myimage', 'v2.0')
        try:
            self.assertEqual(
                ['myimage:v2.0'],
                writer._tar_manifest[0]['RepoTags'])
        finally:
            writer._image_tar.close()
            os.unlink(writer._temp_file.name)

    @mock.patch('occystrap.outputs.docker.requests_unixsocket.Session')
    def test_finalize_creates_valid_tarball(self, mock_session_class):
        """Test that finalize creates a valid docker-loadable tarball."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_session.post.return_value = mock_response
        mock_session_class.return_value = mock_session

        writer = output_docker.DockerWriter('test/image', 'latest')

        # Add config
        config_data = json.dumps({'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        # Add layer
        layer_tf = tempfile.NamedTemporaryFile(delete=False)
        try:
            with tarfile.open(fileobj=layer_tf, mode='w') as layer_tar:
                ti = tarfile.TarInfo('test.txt')
                data = b'test content'
                ti.size = len(data)
                layer_tar.addfile(ti, io.BytesIO(data))
            layer_tf.close()

            with open(layer_tf.name, 'rb') as f:
                writer.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256_abc123',
                    f)
        finally:
            os.unlink(layer_tf.name)

        writer.finalize()

        # Verify the API was called correctly
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        self.assertIn('/images/load', call_args[0][0])
        self.assertEqual(
            'application/x-tar',
            call_args[1]['headers']['Content-Type'])

    @mock.patch('occystrap.outputs.docker.requests_unixsocket.Session')
    def test_finalize_api_error_handling(self, mock_session_class):
        """Test that finalize raises exception on API error."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal server error'
        mock_session.post.return_value = mock_response
        mock_session_class.return_value = mock_session

        writer = output_docker.DockerWriter('test/image', 'latest')

        # Add minimal content
        config_data = b'{}'
        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        with self.assertRaises(Exception) as ctx:
            writer.finalize()

        self.assertIn('500', str(ctx.exception))
        self.assertIn('Internal server error', str(ctx.exception))

    @mock.patch('occystrap.outputs.docker.requests_unixsocket.Session')
    def test_finalize_cleans_up_temp_file(self, mock_session_class):
        """Test that finalize removes temporary file after completion."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_session.post.return_value = mock_response
        mock_session_class.return_value = mock_session

        writer = output_docker.DockerWriter('test/image', 'latest')
        temp_path = writer._temp_file.name

        config_data = b'{}'
        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        writer.finalize()

        self.assertFalse(os.path.exists(temp_path))

    @mock.patch('occystrap.outputs.docker.requests_unixsocket.Session')
    def test_finalize_cleans_up_on_error(self, mock_session_class):
        """Test that temp file is cleaned up even on API error."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Error'
        mock_session.post.return_value = mock_response
        mock_session_class.return_value = mock_session

        writer = output_docker.DockerWriter('test/image', 'latest')
        temp_path = writer._temp_file.name

        config_data = b'{}'
        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        try:
            writer.finalize()
        except Exception:
            pass

        self.assertFalse(os.path.exists(temp_path))
