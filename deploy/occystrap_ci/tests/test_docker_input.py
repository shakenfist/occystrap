import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from occystrap import constants
from occystrap.inputs import docker as input_docker


class DockerInputTestCase(unittest.TestCase):
    def test_initialization(self):
        """Test Image initializes with correct attributes."""
        img = input_docker.Image('myimage', 'v1.0')
        self.assertEqual('myimage', img.image)
        self.assertEqual('v1.0', img.tag)
        self.assertEqual('/var/run/docker.sock', img.socket_path)

    def test_initialization_default_tag(self):
        """Test Image defaults to 'latest' tag."""
        img = input_docker.Image('myimage')
        self.assertEqual('latest', img.tag)

    def test_initialization_custom_socket(self):
        """Test Image accepts custom socket path."""
        img = input_docker.Image(
            'myimage', 'v1.0',
            socket_path='/run/podman/podman.sock')
        self.assertEqual('/run/podman/podman.sock', img.socket_path)

    def test_image_property(self):
        """Test image property returns correct value."""
        img = input_docker.Image('myorg/myimage', 'v2.0')
        self.assertEqual('myorg/myimage', img.image)

    def test_tag_property(self):
        """Test tag property returns correct value."""
        img = input_docker.Image('myimage', 'v3.0')
        self.assertEqual('v3.0', img.tag)

    def test_socket_url_encoding(self):
        """Test that socket path is correctly URL-encoded."""
        img = input_docker.Image('myimage', 'v1.0')
        url = img._socket_url('/images/myimage:v1.0/json')
        self.assertIn('%2Fvar%2Frun%2Fdocker.sock', url)
        self.assertTrue(url.endswith('/images/myimage:v1.0/json'))
        self.assertTrue(url.startswith('http+unix://'))

    def test_get_image_reference(self):
        """Test that image reference is correctly formatted."""
        img = input_docker.Image('myimage', 'v1.0')
        self.assertEqual('myimage:v1.0', img._get_image_reference())

    @mock.patch('occystrap.inputs.docker.requests_unixsocket.Session')
    def test_inspect(self, mock_session_class):
        """Test inspect returns image metadata."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'Id': 'sha256:abc123',
            'RepoTags': ['myimage:v1.0'],
            'Architecture': 'amd64'
        }
        mock_session.request.return_value = mock_response
        mock_session_class.return_value = mock_session

        img = input_docker.Image('myimage', 'v1.0')
        metadata = img.inspect()

        self.assertEqual('sha256:abc123', metadata['Id'])
        self.assertEqual('amd64', metadata['Architecture'])

    @mock.patch('occystrap.inputs.docker.requests_unixsocket.Session')
    def test_inspect_image_not_found(self, mock_session_class):
        """Test inspect raises exception for missing image."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 404
        mock_session.request.return_value = mock_response
        mock_session_class.return_value = mock_session

        img = input_docker.Image('nonexistent', 'v1.0')

        with self.assertRaises(Exception) as ctx:
            img.inspect()

        self.assertIn('Image not found', str(ctx.exception))

    @mock.patch('occystrap.inputs.docker.requests_unixsocket.Session')
    def test_request_api_error(self, mock_session_class):
        """Test _request raises exception on API error."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal server error'
        mock_session.request.return_value = mock_response
        mock_session_class.return_value = mock_session

        img = input_docker.Image('myimage', 'v1.0')

        with self.assertRaises(Exception) as ctx:
            img._request('GET', '/images/myimage:v1.0/json')

        self.assertIn('500', str(ctx.exception))
        self.assertIn('Internal server error', str(ctx.exception))

    def _create_docker_save_tarball(self, config_data, layers):
        """Create a tarball in docker-save format.

        Args:
            config_data: Dict for config.json content.
            layers: List of (layer_id, layer_content) tuples.

        Returns:
            Path to the temporary tarball.
        """
        tf = tempfile.NamedTemporaryFile(delete=False, suffix='.tar')
        with tarfile.open(fileobj=tf, mode='w') as tar:
            # Write config
            config_filename = 'abc123.json'
            config_json = json.dumps(config_data).encode('utf-8')
            ti = tarfile.TarInfo(config_filename)
            ti.size = len(config_json)
            tar.addfile(ti, io.BytesIO(config_json))

            # Write layers
            layer_paths = []
            for layer_id, layer_content in layers:
                # Create layer tarball
                layer_tf = io.BytesIO()
                with tarfile.open(fileobj=layer_tf, mode='w') as layer_tar:
                    for path, content in layer_content.items():
                        lti = tarfile.TarInfo(path)
                        data = content.encode('utf-8') if isinstance(
                            content, str) else content
                        lti.size = len(data)
                        layer_tar.addfile(lti, io.BytesIO(data))
                layer_tf.seek(0)

                # Add to main tarball
                layer_path = '%s/layer.tar' % layer_id
                layer_paths.append(layer_path)
                layer_data = layer_tf.read()
                ti = tarfile.TarInfo(layer_path)
                ti.size = len(layer_data)
                tar.addfile(ti, io.BytesIO(layer_data))

            # Write manifest
            manifest = [{
                'Config': config_filename,
                'RepoTags': ['myimage:v1.0'],
                'Layers': layer_paths
            }]
            manifest_json = json.dumps(manifest).encode('utf-8')
            ti = tarfile.TarInfo('manifest.json')
            ti.size = len(manifest_json)
            tar.addfile(ti, io.BytesIO(manifest_json))

        tf.close()
        return tf.name

    @mock.patch('occystrap.inputs.docker.requests_unixsocket.Session')
    def test_fetch_yields_config_and_layers(self, mock_session_class):
        """Test fetch yields config file and layers."""
        tarball_path = self._create_docker_save_tarball(
            config_data={'architecture': 'amd64', 'os': 'linux'},
            layers=[
                ('layer1', {'file1.txt': 'content1'}),
                ('layer2', {'file2.txt': 'content2'}),
            ])

        try:
            mock_session = mock.MagicMock()

            # Inspect response
            inspect_response = mock.MagicMock()
            inspect_response.status_code = 200
            inspect_response.json.return_value = {'Id': 'sha256:abc'}

            # Tarball response
            with open(tarball_path, 'rb') as f:
                tarball_content = f.read()

            tarball_response = mock.MagicMock()
            tarball_response.status_code = 200
            tarball_response.iter_content.return_value = [tarball_content]

            mock_session.request.side_effect = [
                inspect_response,
                tarball_response
            ]
            mock_session_class.return_value = mock_session

            img = input_docker.Image('myimage', 'v1.0')
            elements = list(img.fetch())

            # Should yield config + 2 layers
            self.assertEqual(3, len(elements))

            # First element is config
            self.assertEqual(constants.CONFIG_FILE, elements[0][0])
            self.assertIn('.json', elements[0][1])

            # Next two are layers
            self.assertEqual(constants.IMAGE_LAYER, elements[1][0])
            self.assertEqual('layer1', elements[1][1])
            self.assertIsNotNone(elements[1][2])

            self.assertEqual(constants.IMAGE_LAYER, elements[2][0])
            self.assertEqual('layer2', elements[2][1])
            self.assertIsNotNone(elements[2][2])

        finally:
            os.unlink(tarball_path)

    @mock.patch('occystrap.inputs.docker.requests_unixsocket.Session')
    def test_fetch_respects_callback(self, mock_session_class):
        """Test fetch respects fetch_callback for layer skipping."""
        tarball_path = self._create_docker_save_tarball(
            config_data={'architecture': 'amd64'},
            layers=[
                ('layer1', {'file1.txt': 'content1'}),
                ('layer2', {'file2.txt': 'content2'}),
            ])

        try:
            mock_session = mock.MagicMock()

            inspect_response = mock.MagicMock()
            inspect_response.status_code = 200
            inspect_response.json.return_value = {'Id': 'sha256:abc'}

            with open(tarball_path, 'rb') as f:
                tarball_content = f.read()

            tarball_response = mock.MagicMock()
            tarball_response.status_code = 200
            tarball_response.iter_content.return_value = [tarball_content]

            mock_session.request.side_effect = [
                inspect_response,
                tarball_response
            ]
            mock_session_class.return_value = mock_session

            img = input_docker.Image('myimage', 'v1.0')

            # Skip layer1
            def skip_layer1(digest):
                return digest != 'layer1'

            elements = list(img.fetch(fetch_callback=skip_layer1))

            # Should yield config + 2 layers (one skipped)
            self.assertEqual(3, len(elements))

            # Layer1 should have None data (skipped)
            layer1_element = [e for e in elements if e[1] == 'layer1'][0]
            self.assertIsNone(layer1_element[2])

            # Layer2 should have data
            layer2_element = [e for e in elements if e[1] == 'layer2'][0]
            self.assertIsNotNone(layer2_element[2])

        finally:
            os.unlink(tarball_path)

    def test_always_fetch_returns_true(self):
        """Test the always_fetch helper function."""
        self.assertTrue(input_docker.always_fetch('sha256:abc'))
        self.assertTrue(input_docker.always_fetch('anything'))
