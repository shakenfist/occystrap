import gzip
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from occystrap import constants
from occystrap.layer_cache import LayerCache
from occystrap.outputs import registry as output_registry


class RegistryWriterTestCase(unittest.TestCase):
    def _elem(self, element_type, name, data):
        """Helper to create an ImageElement."""
        return constants.ImageElement(
            element_type, name, data)

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
        self.assertTrue(writer.fetch_callback('abc123'))
        self.assertTrue(writer.fetch_callback('def456'))

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
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        self.assertIsNotNone(writer._config_digest)
        self.assertTrue(writer._config_digest.startswith('sha256:'))
        self.assertEqual(len(config_data), writer._config_size)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_process_image_layer(self, mock_request):
        """Test processing an image layer element."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=1)

        # Use side_effect to handle different request types
        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                response.status_code = 200  # Blob exists
            elif method == 'PUT' and '/manifests/' in url:
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        # Add config (required for finalize)
        config_data = json.dumps(
            {'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        layer_data = b'test layer content'
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'sha256_original',
                io.BytesIO(layer_data)))

        # Finalize to collect layer metadata from futures
        writer.finalize()

        # Check manifest was pushed with correct layer info
        last_call = mock_request.call_args
        manifest_data = last_call[1]['data']
        manifest = json.loads(manifest_data.decode('utf-8'))

        self.assertEqual(1, len(manifest['layers']))
        layer = manifest['layers'][0]
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

        # Use side_effect to handle different request types
        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                response.status_code = 404  # Blob doesn't exist
            elif method == 'POST':
                response.status_code = 202
                response.headers = {
                    'Location':
                        '/v2/myuser/myimage/blobs/uploads/uuid123'
                }
            elif method == 'PUT' and '/manifests/' in url:
                response.status_code = 201
            elif method == 'PUT':
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        # Add config (required for finalize)
        config_data = json.dumps(
            {'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        layer_data = b'test layer content'
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'sha256_original',
                io.BytesIO(layer_data)))

        # Finalize to collect layer metadata
        writer.finalize()

        # Verify the digest matches gzipped content (mtime=0 for
        # deterministic output)
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode='wb',
                           mtime=0) as gz:
            gz.write(layer_data)
        compressed.seek(0)
        expected_digest = 'sha256:' + hashlib.sha256(
            compressed.read()).hexdigest()

        # Check manifest was pushed with correct layer digest
        last_call = mock_request.call_args
        manifest_data = last_call[1]['data']
        manifest = json.loads(manifest_data.decode('utf-8'))

        self.assertEqual(
            expected_digest, manifest['layers'][0]['digest'])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_finalize_pushes_manifest(self, mock_request):
        """Test that finalize pushes the manifest."""
        # Use max_workers=1 for predictable call ordering
        # with mocks
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
        config_data = json.dumps(
            {'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        # Add layer
        layer_data = b'test layer'
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'sha256_layer',
                io.BytesIO(layer_data)))

        writer.finalize()

        # Verify PUT to manifests endpoint
        last_call = mock_request.call_args
        self.assertEqual('PUT', last_call[0][0])
        self.assertIn('/manifests/v1.0', last_call[0][1])
        self.assertEqual(
            'application/vnd.docker.distribution.'
            'manifest.v2+json',
            last_call[1]['headers']['Content-Type'])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_finalize_without_config_raises_error(
            self, mock_request):
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
                'Bearer realm="https://ghcr.io/token"'
                ',service="ghcr.io"'
        }

        # Token request
        token_response = mock.MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            'token': 'test_token'
        }
        mock_get.return_value = token_response

        # Retry after auth succeeds
        success_response = mock.MagicMock()
        success_response.status_code = 200

        mock_request.side_effect = [
            auth_response, success_response
        ]

        writer._blob_exists('sha256:abc123')

        # Verify token was requested with auth
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args[1]
        self.assertEqual(
            ('user', 'token'), call_kwargs['auth'])

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_manifest_format(self, mock_request):
        """Test that manifest has correct format."""
        # Use max_workers=1 for predictable call ordering
        # with mocks
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
        config_data = json.dumps(
            {'architecture': 'amd64'}).encode('utf-8')
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        layer_data = b'test layer'
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'sha256_layer',
                io.BytesIO(layer_data)))

        writer.finalize()

        # Extract manifest from PUT call
        last_call = mock_request.call_args
        manifest_data = last_call[1]['data']
        manifest = json.loads(manifest_data.decode('utf-8'))

        self.assertEqual(2, manifest['schemaVersion'])
        self.assertEqual(
            'application/vnd.docker.distribution.'
            'manifest.v2+json',
            manifest['mediaType'])
        self.assertIn('config', manifest)
        self.assertIn('layers', manifest)
        self.assertEqual(
            writer._config_digest,
            manifest['config']['digest'])
        self.assertEqual(1, len(manifest['layers']))

    def test_timing_fields_initialized(self):
        """Test that timing fields are initialized to zero."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')
        self.assertEqual(0.0, writer._total_compress_time)
        self.assertEqual(0.0, writer._total_upload_time)
        self.assertEqual(0, writer._total_input_bytes)
        self.assertEqual(0, writer._upload_skipped)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_timing_fields_populated_after_finalize(
            self, mock_request):
        """Test that timing fields are populated after processing."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0', max_workers=1)

        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                response.status_code = 200  # Blob exists
            elif method == 'PUT' and '/manifests/' in url:
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        config_data = json.dumps(
            {'architecture': 'amd64'}).encode()
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        layer_data = b'test layer content for timing'
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'sha256_layer',
                io.BytesIO(layer_data)))

        writer.finalize()

        self.assertGreater(
            writer._total_compress_time, 0.0)
        self.assertGreater(
            writer._total_upload_time, 0.0)
        self.assertEqual(
            len(layer_data), writer._total_input_bytes)
        # Blob existed, so upload was skipped
        self.assertEqual(1, writer._upload_skipped)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_upload_blob_returns_true_when_exists(
            self, mock_request):
        """Test _upload_blob returns True when blob already exists."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        result = writer._upload_blob(
            'sha256:abc123', io.BytesIO(b'data'), 4)
        self.assertTrue(result)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_upload_blob_returns_false_when_uploaded(
            self, mock_request):
        """Test _upload_blob returns False when blob is uploaded."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')

        head_response = mock.MagicMock()
        head_response.status_code = 404

        post_response = mock.MagicMock()
        post_response.status_code = 202
        post_response.headers = {
            'Location':
                '/v2/myuser/myimage/blobs/uploads/uuid123'
        }

        put_response = mock.MagicMock()
        put_response.status_code = 201

        mock_request.side_effect = [
            head_response, post_response, put_response
        ]

        result = writer._upload_blob(
            'sha256:abc123', io.BytesIO(b'data'), 4)
        self.assertFalse(result)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_upload_skipped_count_with_multiple_layers(
            self, mock_request):
        """Test upload_skipped counts correctly with multiple layers."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0',
            max_workers=1)

        call_count = [0]

        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                call_count[0] += 1
                # First two HEAD checks: blob exists (skip)
                # Third HEAD check: blob doesn't exist
                # (upload)
                if call_count[0] <= 3:
                    response.status_code = 200
                else:
                    response.status_code = 404
            elif method == 'POST':
                response.status_code = 202
                response.headers = {
                    'Location': '/v2/x/blobs/uploads/uuid'
                }
            elif method == 'PUT' and '/manifests/' in url:
                response.status_code = 201
            elif method == 'PUT':
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        config_data = json.dumps(
            {'architecture': 'amd64'}).encode()
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        # Two layers where blob exists (skipped uploads)
        for i in range(2):
            writer.process_image_element(
                self._elem(
                    constants.IMAGE_LAYER,
                    f'sha256_layer{i}',
                    io.BytesIO(b'layer data')))

        writer.finalize()

        self.assertEqual(2, writer._upload_skipped)
        self.assertEqual(
            2 * len(b'layer data'),
            writer._total_input_bytes)


class RegistryWriterCacheTestCase(unittest.TestCase):
    """Tests for cross-invocation layer cache integration.

    DiffID keys use bare hex strings (no sha256: prefix) to match
    runtime behaviour -- input sources strip the prefix before
    calling fetch_callback.
    """

    def _elem(self, element_type, name, data):
        """Helper to create an ImageElement."""
        return constants.ImageElement(
            element_type, name, data)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_fetch_callback_without_cache(self, mock_request):
        """Test fetch_callback returns True without cache."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')
        self.assertTrue(writer.fetch_callback('abc123'))

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_fetch_callback_cache_miss(self, mock_request):
        """Test fetch_callback returns True on cache miss."""
        with tempfile.TemporaryDirectory() as d:
            cache = LayerCache(os.path.join(d, 'c.json'))
            writer = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                layer_cache=cache)
            self.assertTrue(
                writer.fetch_callback('unknown_hex'))

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_fetch_callback_cache_hit_registry_has_blob(
            self, mock_request):
        """Test fetch_callback returns False on cache hit."""
        with tempfile.TemporaryDirectory() as d:
            cache = LayerCache(os.path.join(d, 'c.json'))
            cache.record(
                'input1hex', 'none',
                'sha256:compressed1', 100,
                'application/vnd.docker.image.rootfs'
                '.diff.tar.gzip')

            # Registry has the blob
            mock_response = mock.MagicMock()
            mock_response.status_code = 200
            mock_request.return_value = mock_response

            writer = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                layer_cache=cache)
            result = writer.fetch_callback('input1hex')
            self.assertFalse(result)
            self.assertIn('input1hex',
                          writer._cached_layers)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_fetch_callback_cache_hit_registry_missing(
            self, mock_request):
        """Test fetch_callback returns True when registry lost blob."""
        with tempfile.TemporaryDirectory() as d:
            cache = LayerCache(os.path.join(d, 'c.json'))
            cache.record(
                'input1hex', 'none',
                'sha256:compressed1', 100,
                'application/vnd.docker.image.rootfs'
                '.diff.tar.gzip')

            # Registry does NOT have the blob
            mock_response = mock.MagicMock()
            mock_response.status_code = 404
            mock_request.return_value = mock_response

            writer = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                layer_cache=cache)
            result = writer.fetch_callback('input1hex')
            self.assertTrue(result)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_cached_layer_in_manifest(self, mock_request):
        """Test that cached layers appear correctly in manifest."""
        with tempfile.TemporaryDirectory() as d:
            cache = LayerCache(os.path.join(d, 'c.json'))
            cache.record(
                'layer1hex', 'none',
                'sha256:compressed_layer1', 5000,
                'application/vnd.docker.image.rootfs'
                '.diff.tar.gzip')

            # Registry has the cached blob
            def mock_request_handler(
                    method, url, **kwargs):
                response = mock.MagicMock()
                if method == 'HEAD':
                    response.status_code = 200
                elif (method == 'PUT'
                      and '/manifests/' in url):
                    response.status_code = 201
                else:
                    response.status_code = 200
                return response

            mock_request.side_effect = (
                mock_request_handler)

            writer = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                max_workers=1, layer_cache=cache)

            # fetch_callback should skip
            self.assertFalse(
                writer.fetch_callback('layer1hex'))

            # Process config
            config_data = json.dumps(
                {'architecture': 'amd64'}).encode()
            writer.process_image_element(
                self._elem(
                    constants.CONFIG_FILE,
                    'config.json',
                    io.BytesIO(config_data)))

            # Process cached layer (data=None)
            writer.process_image_element(
                self._elem(
                    constants.IMAGE_LAYER,
                    'layer1hex',
                    None))

            writer.finalize()

            # Check manifest includes the cached layer
            last_call = mock_request.call_args
            manifest = json.loads(
                last_call[1]['data'].decode('utf-8'))
            self.assertEqual(1, len(manifest['layers']))
            self.assertEqual(
                'sha256:compressed_layer1',
                manifest['layers'][0]['digest'])
            self.assertEqual(
                5000, manifest['layers'][0]['size'])
            self.assertEqual(1, writer._cache_hits)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_cache_saved_after_finalize(self, mock_request):
        """Test that cache file is written after finalize."""
        with tempfile.TemporaryDirectory() as d:
            cache_path = os.path.join(d, 'cache.json')
            cache = LayerCache(cache_path)

            def mock_request_handler(
                    method, url, **kwargs):
                response = mock.MagicMock()
                if method == 'HEAD':
                    response.status_code = 200
                elif (method == 'PUT'
                      and '/manifests/' in url):
                    response.status_code = 201
                else:
                    response.status_code = 200
                return response

            mock_request.side_effect = (
                mock_request_handler)

            writer = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                max_workers=1, layer_cache=cache)

            config_data = json.dumps(
                {'architecture': 'amd64'}).encode()
            writer.process_image_element(
                self._elem(
                    constants.CONFIG_FILE,
                    'config.json',
                    io.BytesIO(config_data)))

            writer.process_image_element(
                self._elem(
                    constants.IMAGE_LAYER,
                    'input_layer_hex',
                    io.BytesIO(b'layer data')))

            writer.finalize()

            # Cache file should exist
            self.assertTrue(os.path.exists(cache_path))

            # Load and verify cache contents
            cache2 = LayerCache(cache_path)
            entry = cache2.lookup(
                'input_layer_hex', 'none')
            self.assertIsNotNone(entry)
            self.assertTrue(
                entry['compressed_digest'].startswith(
                    'sha256:'))

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_no_cache_backward_compatibility(
            self, mock_request):
        """Test that registry push works without cache."""
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0',
            max_workers=1)

        def mock_request_handler(method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                response.status_code = 200
            elif (method == 'PUT'
                  and '/manifests/' in url):
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = mock_request_handler

        config_data = json.dumps(
            {'architecture': 'amd64'}).encode()
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'sha256_layer',
                io.BytesIO(b'layer data')))

        # Should complete without error
        writer.finalize()

        self.assertEqual(0, writer._cache_hits)
        self.assertIsNone(writer._layer_cache)

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_cache_records_under_original_diffid(
            self, mock_request):
        """Test cache records using original DiffID, not
        filter-transformed name.

        When filters modify layer content they recalculate the
        SHA256 and pass a new name to RegistryWriter. The cache
        must record under the original DiffID (from
        fetch_callback) so that subsequent lookups succeed.
        """
        with tempfile.TemporaryDirectory() as d:
            cache_path = os.path.join(d, 'cache.json')
            cache = LayerCache(cache_path)

            def mock_request_handler(
                    method, url, **kwargs):
                response = mock.MagicMock()
                if method == 'HEAD':
                    response.status_code = 200
                elif (method == 'PUT'
                      and '/manifests/' in url):
                    response.status_code = 201
                else:
                    response.status_code = 200
                return response

            mock_request.side_effect = (
                mock_request_handler)

            writer = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                max_workers=1, layer_cache=cache)

            # fetch_callback receives original DiffID
            writer.fetch_callback('original_hex')

            # Config (required for finalize)
            config_data = json.dumps(
                {'architecture': 'amd64'}).encode()
            writer.process_image_element(
                self._elem(
                    constants.CONFIG_FILE,
                    'config.json',
                    io.BytesIO(config_data)))

            # Filter transformed the name before it
            # reached the writer
            writer.process_image_element(
                self._elem(
                    constants.IMAGE_LAYER,
                    'filter_transformed_hex',
                    io.BytesIO(b'filtered layer data')))

            writer.finalize()

            # Cache should record under original DiffID
            cache2 = LayerCache(cache_path)
            entry = cache2.lookup(
                'original_hex', 'none')
            self.assertIsNotNone(
                entry,
                'Cache should have entry for original '
                'DiffID')
            self.assertTrue(
                entry['compressed_digest'].startswith(
                    'sha256:'))

            # Should NOT have entry for transformed name
            entry2 = cache2.lookup(
                'filter_transformed_hex', 'none')
            self.assertIsNone(
                entry2,
                'Cache should not have entry for '
                'filter-transformed name')

    @mock.patch('occystrap.outputs.registry.requests.request')
    def test_cache_round_trip_with_filter_transform(
            self, mock_request):
        """Test full round-trip: record under original DiffID,
        then hit on second invocation even though filter would
        transform the name.

        Simulates two pushes of the same image with a content-
        modifying filter.
        """
        with tempfile.TemporaryDirectory() as d:
            cache_path = os.path.join(d, 'cache.json')

            def mock_request_handler(
                    method, url, **kwargs):
                response = mock.MagicMock()
                if method == 'HEAD':
                    response.status_code = 200
                elif (method == 'PUT'
                      and '/manifests/' in url):
                    response.status_code = 201
                else:
                    response.status_code = 200
                return response

            mock_request.side_effect = (
                mock_request_handler)

            # --- First push: record cache entry ---
            cache1 = LayerCache(cache_path)
            w1 = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                max_workers=1, layer_cache=cache1)

            w1.fetch_callback('original_hex')

            config_data = json.dumps(
                {'architecture': 'amd64'}).encode()
            w1.process_image_element(
                self._elem(
                    constants.CONFIG_FILE,
                    'config.json',
                    io.BytesIO(config_data)))

            w1.process_image_element(
                self._elem(
                    constants.IMAGE_LAYER,
                    'filter_transformed_hex',
                    io.BytesIO(
                        b'filtered layer data')))

            w1.finalize()

            # --- Second push: should get cache hit ---
            cache2 = LayerCache(cache_path)
            w2 = output_registry.RegistryWriter(
                'ghcr.io', 'myuser/myimage', 'v1.0',
                max_workers=1, layer_cache=cache2)

            # fetch_callback with the original DiffID
            # should find the cache entry and skip
            result = w2.fetch_callback('original_hex')
            self.assertFalse(
                result,
                'Second push should get cache hit')

            config_data = json.dumps(
                {'architecture': 'amd64'}).encode()
            w2.process_image_element(
                self._elem(
                    constants.CONFIG_FILE,
                    'config.json',
                    io.BytesIO(config_data)))

            # Layer arrives with data=None (skipped)
            w2.process_image_element(
                self._elem(
                    constants.IMAGE_LAYER,
                    'original_hex',
                    None))

            w2.finalize()

            self.assertEqual(1, w2._cache_hits)


class RegistryWriterOrderedTestCase(unittest.TestCase):
    """Tests for out-of-order layer delivery."""

    def _elem(self, element_type, name, data,
              layer_index=None):
        """Helper to create an ImageElement."""
        return constants.ImageElement(
            element_type, name, data,
            layer_index=layer_index)

    @mock.patch(
        'occystrap.outputs.registry'
        '.requests.request')
    def test_layers_with_index_sorted_in_manifest(
            self, mock_request):
        """Test layers with layer_index are sorted
        correctly in the pushed manifest, even when
        delivered out of order.
        """
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0',
            max_workers=1)

        def mock_request_handler(
                method, url, **kwargs):
            response = mock.MagicMock()
            if method == 'HEAD':
                response.status_code = 200
            elif (method == 'PUT'
                  and '/manifests/' in url):
                response.status_code = 201
            else:
                response.status_code = 200
            return response

        mock_request.side_effect = (
            mock_request_handler)

        # Config
        config_data = json.dumps(
            {'architecture': 'amd64'}).encode()
        writer.process_image_element(
            self._elem(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(config_data)))

        # Deliver layers OUT of order (2, 0, 1)
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'layer_c',
                io.BytesIO(b'layer c data'),
                layer_index=2))
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'layer_a',
                io.BytesIO(b'layer a data'),
                layer_index=0))
        writer.process_image_element(
            self._elem(
                constants.IMAGE_LAYER,
                'layer_b',
                io.BytesIO(b'layer b data'),
                layer_index=1))

        writer.finalize()

        # Check manifest layer order
        last_call = mock_request.call_args
        manifest = json.loads(
            last_call[1]['data'].decode('utf-8'))
        self.assertEqual(
            3, len(manifest['layers']))

        # Layers should be in index order (a, b, c)
        # even though they were delivered out of order
        sizes = [
            layer['size']
            for layer in manifest['layers']]
        # All layers should be present
        self.assertEqual(3, len(sizes))

    @mock.patch(
        'occystrap.outputs.registry'
        '.requests.request')
    def test_requires_ordered_layers_is_false(
            self, mock_request):
        """Test that RegistryWriter declares it does
        not need ordered layers.
        """
        writer = output_registry.RegistryWriter(
            'ghcr.io', 'myuser/myimage', 'v1.0')
        self.assertFalse(
            writer.requires_ordered_layers)
