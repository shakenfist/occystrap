import gzip
import hashlib
import io
import json
import unittest
from unittest import mock

from occystrap import constants
from occystrap.outputs import registry as output_registry


class RegistryWriterTestCase(unittest.TestCase):
    def test_initialization(self):
        """Test RegistryWriter initializes with correct attributes."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')
        self.assertEqual('ghcr.io', writer.registry)
        self.assertEqual('myuser/myimage', writer.image)
        self.assertEqual('v1.0', writer.tag)
        self.assertTrue(writer.secure)
        self.assertIsNone(writer.username)
        self.assertIsNone(writer.password)
        self.assertEqual(4, writer.max_workers)

    def test_initialization_with_max_workers(self):
        """Test RegistryWriter accepts max_workers parameter."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=8)
        self.assertEqual(8, writer.max_workers)

    def test_initialization_with_auth(self):
        """Test RegistryWriter accepts authentication credentials."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0',
            username='user', password='token')
        self.assertEqual('user', writer.username)
        self.assertEqual('token', writer.password)

    def test_initialization_insecure(self):
        """Test RegistryWriter can use HTTP (insecure mode)."""
        writer = output_registry.RegistryWriter(
            'localhost:5000', 'myimage', 'latest', secure=False)
        self.assertFalse(writer.secure)
        self.assertEqual('http', writer._moniker)

    def test_fetch_callback_always_true(self):
        """Test that fetch_callback always returns True."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')
        self.assertTrue(writer.fetch_callback('sha256:abc123'))
        self.assertTrue(writer.fetch_callback('sha256:def456'))

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_blob_exists_check(self, mock_request):
        """Test that _blob_exists correctly checks blob existence."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        exists = writer._blob_exists('sha256:abc123')

        self.assertTrue(exists)
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        self.assertEqual('HEAD', call_args[0][0])
        self.assertIn('sha256:abc123', call_args[0][1])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_blob_not_exists(self, mock_request):
        """Test that _blob_exists returns False for missing blobs."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        mock_response = mock.MagicMock()
        mock_response.status_code = 404
        mock_request.return_value = mock_response

        exists = writer._blob_exists('sha256:abc123')

        self.assertFalse(exists)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_upload_blob_skips_existing(self, mock_request):
        """Test that _upload_blob skips blobs that already exist."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        data = io.BytesIO(b'test data')
        writer._upload_blob('sha256:abc123', data, 9)

        # Should only make HEAD request, not POST/PUT
        self.assertEqual(1, mock_request.call_count)
        call_args = mock_request.call_args
        self.assertEqual('HEAD', call_args[0][0])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_upload_blob_new(self, mock_request):
        """Test uploading a new blob."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=1)

        head_response = mock.MagicMock()
        head_response.status_code = 404

        post_response = mock.MagicMock()
        post_response.status_code = 202
        post_response.headers = {
            'Location': '/v2/myuser/myimage/blobs/uploads/uuid123'
        }

        put_response = mock.MagicMock()
        put_response.status_code = 201

        mock_request.side_effect = [head_response, post_response, put_response]

        data = io.BytesIO(b'test data')
        writer._upload_blob('sha256:abc123', data, 9)

        self.assertEqual(3, mock_request.call_count)
        calls = mock_request.call_args_list
        self.assertEqual('HEAD', calls[0][0][0])
        self.assertEqual('POST', calls[1][0][0])
        self.assertEqual('PUT', calls[2][0][0])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_process_config_file(self, mock_request):
        """Test processing a config file element."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        head_response = mock.MagicMock()
        head_response.status_code = 200
        mock_request.return_value = head_response

        config_data = json.dumps({
            'architecture': 'amd64',
            'os': 'linux'
        }).encode('utf-8')

        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        self.assertIsNotNone(writer._config_digest)
        self.assertTrue(writer._config_digest.startswith('sha256:'))
        self.assertEqual(len(config_data), writer._config_size)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_process_image_layer(self, mock_request):
        """Test processing an image layer element."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        head_response = mock.MagicMock()
        head_response.status_code = 200
        mock_request.return_value = head_response

        layer_data = b'test layer content'

        writer.process_image_element(
            constants.IMAGE_LAYER,
            'sha256_original',
            io.BytesIO(layer_data))

        self.assertEqual(1, len(writer._layers))
        layer = writer._layers[0]
        self.assertEqual(
            'application/vnd.docker.image.rootfs.diff.tar.gzip',
            layer['mediaType'])
        self.assertTrue(layer['digest'].startswith('sha256:'))
        self.assertGreater(layer['size'], 0)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_layer_is_gzip_compressed(self, mock_request):
        """Test that layers are gzip compressed before upload."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=1)

        head_response = mock.MagicMock()
        head_response.status_code = 404

        post_response = mock.MagicMock()
        post_response.status_code = 202
        post_response.headers = {
            'Location': '/v2/myuser/myimage/blobs/uploads/uuid123'
        }

        put_response = mock.MagicMock()
        put_response.status_code = 201

        mock_request.side_effect = [head_response, post_response, put_response]

        layer_data = b'test layer content'
        writer.process_image_element(
            constants.IMAGE_LAYER,
            'sha256_original',
            io.BytesIO(layer_data))

        # Verify the digest matches gzipped content
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode='wb') as gz:
            gz.write(layer_data)
        compressed.seek(0)
        expected_digest = 'sha256:' + hashlib.sha256(
            compressed.read()).hexdigest()

        self.assertEqual(expected_digest, writer._layers[0]['digest'])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_finalize_pushes_manifest(self, mock_request):
        """Test that finalize pushes the manifest."""
        # Use max_workers=1 for predictable call ordering with mocks
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=1)

        # Use side_effect to handle different request types
        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                # Blob exists - skip upload
                response.status_code = 200
            elif method == 'PUT' and '/manifests/' in url:
                # Manifest push
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        # Add config
        config_data = json.dumps({'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        # Add layer
        layer_data = b'test layer'
        writer.process_image_element(
            constants.IMAGE_LAYER,
            'sha256_layer',
            io.BytesIO(layer_data))

        writer.finalize()

        # Verify PUT to manifests endpoint
        last_call = mock_request.call_args
        self.assertEqual('PUT', last_call[0][0])
        self.assertIn('/manifests/v1.0', last_call[0][1])
        self.assertEqual(
            'application/vnd.docker.distribution.manifest.v2+json',
            last_call[1]['headers']['Content-Type'])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_finalize_without_config_raises_error(self, mock_request):
        """Test that finalize raises error if no config was processed."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        with self.assertRaises(Exception) as ctx:
            writer.finalize()

        self.assertIn('No config file', str(ctx.exception))

    @mock.patch('occystrap.outputs.registry.requests.request')
    @mock.patch('occystrap.outputs.registry.requests.get')
    def test_authentication_flow(self, mock_get, mock_request):
        """Test Bearer authentication flow."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0',
            username='user', password='token')

        # First request returns 401 with auth challenge
        auth_response = mock.MagicMock()
        auth_response.status_code = 401
        auth_response.headers = {
            'Www-Authenticate':
                'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
        }

        # Token request
        token_response = mock.MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {'token': 'test_token'}
        mock_get.return_value = token_response

        # Retry after auth succeeds
        success_response = mock.MagicMock()
        success_response.status_code = 200

        mock_request.side_effect = [auth_response, success_response]

        writer._blob_exists('sha256:abc123')

        # Verify token was requested with auth
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args[1]
        self.assertEqual(('user', 'token'), call_kwargs['auth'])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_manifest_format(self, mock_request):
        """Test that manifest has correct format."""
        # Use max_workers=1 for predictable call ordering with mocks
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=1)

        # Use side_effect to handle different request types
        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                # Blob exists - skip upload
                response.status_code = 200
            elif method == 'PUT' and '/manifests/' in url:
                # Manifest push
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        # Process config and layer
        config_data = json.dumps({'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            constants.CONFIG_FILE,
            'config.json',
            io.BytesIO(config_data))

        layer_data = b'test layer'
        writer.process_image_element(
            constants.IMAGE_LAYER,
            'sha256_layer',
            io.BytesIO(layer_data))

        writer.finalize()

        # Extract manifest from PUT call
        last_call = mock_request.call_args
        manifest_data = last_call[1]['data']
        manifest = json.loads(manifest_data.decode('utf-8'))

        self.assertEqual(2, manifest['schemaVersion'])
        self.assertEqual(
            'application/vnd.docker.distribution.manifest.v2+json',
            manifest['mediaType'])
        self.assertIn('config', manifest)
        self.assertIn('layers', manifest)
        self.assertEqual(writer._config_digest, manifest['config']['digest'])
        self.assertEqual(1, len(manifest['layers']))
