"""Tests for the dockerpush input module."""

import gzip
import hashlib
import http.server
import json
import os
import threading
import unittest
from unittest import mock

import requests

from occystrap import constants
from occystrap.inputs.dockerpush import (
    EmbeddedRegistryHandler,
    Image,
    _RegistryState,
)


def _start_test_server(state=None):
    """Start a test registry server and return (server, port)."""
    if state is None:
        state = _RegistryState()
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), EmbeddedRegistryHandler)
    server.registry_state = state
    thread = threading.Thread(
        target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _no_proxy_session():
    """Create a requests session that bypasses proxy settings.

    CI runners may have HTTP_PROXY set, which would route
    localhost requests through the proxy and break tests.
    """
    s = requests.Session()
    s.trust_env = False
    return s


class TestEmbeddedRegistryV2Check(unittest.TestCase):
    """Tests for GET /v2/ version check."""

    def setUp(self):
        self.server, self.port = _start_test_server()
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()

    def test_v2_returns_200(self):
        r = self.sess.get('%s/v2/' % self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {})

    def test_v2_has_api_version_header(self):
        r = self.sess.get('%s/v2/' % self.base)
        self.assertEqual(
            r.headers.get('Docker-Distribution-API-Version'),
            'registry/2.0')

    def test_unknown_get_returns_404(self):
        r = self.sess.get('%s/v2/unknown' % self.base)
        self.assertEqual(r.status_code, 404)


class TestEmbeddedRegistryHEAD(unittest.TestCase):
    """Tests for HEAD /v2/{name}/blobs/{digest}."""

    def setUp(self):
        self.server, self.port = _start_test_server()
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()

    def test_head_returns_404_not_cached(self):
        """HEAD returns 404 for unknown digests."""
        r = self.sess.head(
            '%s/v2/test/blobs/sha256:abc123' % self.base)
        self.assertEqual(r.status_code, 404)

    def test_head_returns_200_for_skip_digest(self):
        """HEAD returns 200 for digests in skip_digests."""
        state = self.server.registry_state
        state.skip_digests.add('abc123')
        r = self.sess.head(
            '%s/v2/test/blobs/sha256:abc123' % self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.headers.get('Docker-Content-Digest'),
            'sha256:abc123')

    def test_head_returns_404_for_non_skip_digest(self):
        """HEAD returns 404 for digests NOT in skip set."""
        state = self.server.registry_state
        state.skip_digests.add('abc123')
        r = self.sess.head(
            '%s/v2/test/blobs/sha256:def456' % self.base)
        self.assertEqual(r.status_code, 404)


class TestEmbeddedRegistryBlobUpload(unittest.TestCase):
    """Tests for the blob upload flow (POST/PATCH/PUT)."""

    def setUp(self):
        self.state = _RegistryState()
        self.server, self.port = _start_test_server(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()
        # Clean up any temp files
        for path in self.state.blobs.values():
            try:
                os.unlink(path)
            except OSError:
                pass
        for upload in self.state.uploads.values():
            try:
                os.unlink(upload['path'])
            except OSError:
                pass

    def test_post_creates_upload(self):
        r = self.sess.post(
            '%s/v2/test/blobs/uploads/' % self.base)
        self.assertEqual(r.status_code, 202)
        self.assertIn(
            'Docker-Upload-UUID', r.headers)
        self.assertIn('Location', r.headers)

    def test_chunked_upload_flow(self):
        """Test POST -> PATCH -> PUT chunked upload."""
        blob_data = b'hello world blob data'
        blob_hash = hashlib.sha256(blob_data).hexdigest()

        # POST to start upload
        r = self.sess.post(
            '%s/v2/test/blobs/uploads/' % self.base)
        self.assertEqual(r.status_code, 202)
        location = r.headers['Location']

        # PATCH to send data
        r = self.sess.patch(
            '%s%s' % (self.base, location),
            data=blob_data,
            headers={
                'Content-Type':
                    'application/octet-stream'
            })
        self.assertEqual(r.status_code, 202)

        # PUT to complete with digest
        r = self.sess.put(
            '%s%s?digest=sha256:%s'
            % (self.base, location, blob_hash))
        self.assertEqual(r.status_code, 201)
        self.assertIn(blob_hash, self.state.blobs)

    def test_monolithic_upload(self):
        """Test POST -> PUT monolithic upload."""
        blob_data = b'monolithic blob content'
        blob_hash = hashlib.sha256(blob_data).hexdigest()

        # POST to start upload
        r = self.sess.post(
            '%s/v2/test/blobs/uploads/' % self.base)
        location = r.headers['Location']

        # PUT with all data and digest
        r = self.sess.put(
            '%s%s?digest=sha256:%s'
            % (self.base, location, blob_hash),
            data=blob_data,
            headers={
                'Content-Type':
                    'application/octet-stream'
            })
        self.assertEqual(r.status_code, 201)
        self.assertIn(blob_hash, self.state.blobs)

    def test_put_with_wrong_digest_returns_400(self):
        """Test that digest mismatch returns 400."""
        blob_data = b'some blob data'
        wrong_hash = 'a' * 64

        r = self.sess.post(
            '%s/v2/test/blobs/uploads/' % self.base)
        location = r.headers['Location']

        r = self.sess.put(
            '%s%s?digest=sha256:%s'
            % (self.base, location, wrong_hash),
            data=blob_data)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(wrong_hash, self.state.blobs)


class TestEmbeddedRegistryManifest(unittest.TestCase):
    """Tests for PUT /v2/{name}/manifests/{tag}."""

    def setUp(self):
        self.state = _RegistryState()
        self.server, self.port = _start_test_server(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()

    def test_manifest_put_stores_data(self):
        manifest = json.dumps({
            'schemaVersion': 2,
            'config': {
                'digest': 'sha256:abc',
                'size': 100,
            },
            'layers': [],
        }).encode()

        r = self.sess.put(
            '%s/v2/test/manifests/latest' % self.base,
            data=manifest,
            headers={
                'Content-Type':
                    'application/'
                    'vnd.docker.distribution'
                    '.manifest.v2+json'
            })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(
            self.state.manifest_data, manifest)

    def test_manifest_put_signals_event(self):
        manifest = b'{"schemaVersion": 2}'

        self.sess.put(
            '%s/v2/test/manifests/latest' % self.base,
            data=manifest)
        self.assertTrue(
            self.state.manifest_event.is_set())


class TestImageDockerAPI(unittest.TestCase):
    """Tests for Image class Docker API helpers."""

    def _make_image(self, socket_path='/var/run/docker.sock'):
        return Image(
            'busybox', tag='latest',
            socket_path=socket_path)

    def test_image_properties(self):
        img = self._make_image()
        self.assertEqual(img.image, 'busybox')
        self.assertEqual(img.tag, 'latest')

    @mock.patch(
        'occystrap.inputs.dockerpush'
        '.requests_unixsocket.Session')
    def test_tag_image(self, mock_session_cls):
        mock_session = mock.MagicMock()
        mock_session.request.return_value = mock.MagicMock(
            status_code=201)
        mock_session_cls.return_value = mock_session

        img = self._make_image()
        img._tag_image('localhost:5000/busybox', 'latest')

        mock_session.request.assert_called_once()
        call_args = mock_session.request.call_args
        self.assertEqual(call_args[0][0], 'POST')
        self.assertIn('/images/', call_args[0][1])
        self.assertIn('tag', call_args[0][1])

    @mock.patch(
        'occystrap.inputs.dockerpush'
        '.requests_unixsocket.Session')
    def test_tag_image_404_raises(self, mock_session_cls):
        mock_session = mock.MagicMock()
        mock_session.request.return_value = mock.MagicMock(
            status_code=404)
        mock_session_cls.return_value = mock_session

        img = self._make_image()
        with self.assertRaises(Exception):
            img._tag_image(
                'localhost:5000/busybox', 'latest')

    @mock.patch(
        'occystrap.inputs.dockerpush'
        '.requests_unixsocket.Session')
    def test_untag_image(self, mock_session_cls):
        mock_session = mock.MagicMock()
        mock_session.request.return_value = mock.MagicMock(
            status_code=200)
        mock_session_cls.return_value = mock_session

        img = self._make_image()
        img._untag_image('localhost:5000/busybox', 'latest')

        mock_session.request.assert_called_once()
        call_args = mock_session.request.call_args
        self.assertEqual(call_args[0][0], 'DELETE')
        self.assertIn('noprune=true', call_args[0][1])


class TestImageFetch(unittest.TestCase):
    """Tests for the Image.fetch() method.

    These tests use a real embedded registry but mock the
    Docker daemon API calls.
    """

    def _make_gzip_layer(self, content):
        """Create a gzip-compressed layer with known digest."""
        compressed = gzip.compress(content)
        compressed_hash = hashlib.sha256(
            compressed).hexdigest()
        uncompressed_hash = hashlib.sha256(
            content).hexdigest()
        return compressed, compressed_hash, \
            uncompressed_hash

    def _make_config(self, diff_ids):
        """Create a config blob with the given DiffIDs."""
        config = {
            'rootfs': {
                'type': 'layers',
                'diff_ids': [
                    'sha256:%s' % d for d in diff_ids
                ],
            },
        }
        config_bytes = json.dumps(config).encode()
        config_hash = hashlib.sha256(
            config_bytes).hexdigest()
        return config_bytes, config_hash

    def _make_manifest(self, config_hash, config_size,
                       layers):
        """Create a manifest with config and layers."""
        docker_config_type = \
            constants.MEDIA_TYPE_DOCKER_CONFIG
        docker_layer_type = \
            constants.MEDIA_TYPE_DOCKER_LAYER_GZIP
        manifest = {
            'schemaVersion': 2,
            'config': {
                'mediaType': docker_config_type,
                'digest': 'sha256:%s' % config_hash,
                'size': config_size,
            },
            'layers': [
                {
                    'mediaType': docker_layer_type,
                    'digest':
                        'sha256:%s' % comp_hash,
                    'size': comp_size,
                }
                for comp_hash, comp_size in layers
            ],
        }
        return json.dumps(manifest).encode()

    def _upload_blob_to_server(self, sess, base, data,
                               digest_hex):
        """Upload a blob to the test server."""
        r = sess.post(
            '%s/v2/test/blobs/uploads/' % base)
        location = r.headers['Location']
        sess.put(
            '%s%s?digest=sha256:%s'
            % (base, location, digest_hex),
            data=data)

    @mock.patch(
        'occystrap.inputs.dockerpush'
        '.requests_unixsocket.Session')
    def test_fetch_single_layer(self, mock_session_cls):
        """Test fetching an image with one layer."""
        layer_content = b'layer tar data here'
        compressed, comp_hash, uncomp_hash = \
            self._make_gzip_layer(layer_content)
        config_bytes, config_hash = self._make_config(
            [uncomp_hash])
        manifest = self._make_manifest(
            config_hash, len(config_bytes),
            [(comp_hash, len(compressed))])

        # Mock Docker API responses
        mock_session = mock.MagicMock()

        def mock_request(method, url, stream=False,
                         headers=None, data=None):
            resp = mock.MagicMock()
            if method == 'POST' and '/tag' in url:
                resp.status_code = 201
                return resp
            elif method == 'POST' and '/push' in url:
                resp.status_code = 200
                resp.iter_lines.return_value = [
                    json.dumps(
                        {'status': 'Pushing'}).encode(),
                ]
                return resp
            elif method == 'DELETE':
                resp.status_code = 200
                return resp
            resp.status_code = 404
            return resp

        mock_session.request.side_effect = mock_request
        mock_session_cls.return_value = mock_session

        img = Image('test', tag='latest')
        # We need to start the server ourselves
        # and push the blobs manually since we're mocking
        # Docker's push
        state = _RegistryState()
        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), EmbeddedRegistryHandler)
        server.registry_state = state
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        base = 'http://127.0.0.1:%d' % port
        sess = _no_proxy_session()

        try:
            # Upload blobs to the test server
            self._upload_blob_to_server(
                sess, base, config_bytes, config_hash)
            self._upload_blob_to_server(
                sess, base, compressed, comp_hash)

            # Upload manifest
            sess.put(
                '%s/v2/test/manifests/latest' % base,
                data=manifest)

            # Monkey-patch to use our server
            def fake_start(s):
                server.registry_state = s
                # Copy blobs from our state
                s.blobs.update(state.blobs)
                s.manifest_data = state.manifest_data
                s.manifest_event.set()
                return server

            def fake_stop(s):
                pass  # Don't stop, we do it ourselves

            img._start_server = fake_start
            img._stop_server = fake_stop

            # Consume elements one at a time so
            # file handles stay open (the generator
            # closes them on resume, matching
            # registry.py's pattern)
            elements = []
            layer_data = None
            for elem in img.fetch():
                if elem.element_type == \
                        constants.IMAGE_LAYER \
                        and elem.data is not None:
                    layer_data = elem.data.read()
                elements.append(elem)

            # Should have config + 1 layer
            self.assertEqual(len(elements), 2)
            self.assertEqual(
                elements[0].element_type,
                constants.CONFIG_FILE)
            self.assertEqual(
                elements[0].name,
                '%s.json' % config_hash)
            self.assertEqual(
                elements[1].element_type,
                constants.IMAGE_LAYER)
            self.assertEqual(
                elements[1].name, uncomp_hash)
            # Layer data should be decompressed
            self.assertEqual(
                layer_data, layer_content)
        finally:
            server.shutdown()

    @mock.patch(
        'occystrap.inputs.dockerpush'
        '.requests_unixsocket.Session')
    def test_fetch_multi_layer(self, mock_session_cls):
        """Test fetching an image with three layers."""
        layer_contents = [
            b'base layer data',
            b'middle layer data here',
            b'top layer with more stuff',
        ]
        layers_info = []
        uncomp_hashes = []
        for content in layer_contents:
            compressed, comp_hash, uncomp_hash = \
                self._make_gzip_layer(content)
            layers_info.append(
                (compressed, comp_hash,
                 len(compressed)))
            uncomp_hashes.append(uncomp_hash)

        config_bytes, config_hash = self._make_config(
            uncomp_hashes)
        manifest = self._make_manifest(
            config_hash, len(config_bytes),
            [(info[1], info[2])
             for info in layers_info])

        mock_session = mock.MagicMock()

        def mock_request(method, url, stream=False,
                         headers=None, data=None):
            resp = mock.MagicMock()
            if method == 'POST' and '/tag' in url:
                resp.status_code = 201
            elif method == 'POST' and '/push' in url:
                resp.status_code = 200
                resp.iter_lines.return_value = [
                    json.dumps(
                        {'status': 'Pushing'}
                    ).encode(),
                ]
            elif method == 'DELETE':
                resp.status_code = 200
            else:
                resp.status_code = 404
            return resp

        mock_session.request.side_effect = \
            mock_request
        mock_session_cls.return_value = mock_session

        img = Image('test', tag='latest')
        state = _RegistryState()
        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0),
            EmbeddedRegistryHandler)
        server.registry_state = state
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        base = 'http://127.0.0.1:%d' % port
        sess = _no_proxy_session()

        try:
            # Upload config blob
            self._upload_blob_to_server(
                sess, base,
                config_bytes, config_hash)

            # Upload all layer blobs
            for compressed, comp_hash, _ \
                    in layers_info:
                self._upload_blob_to_server(
                    sess, base,
                    compressed, comp_hash)

            # Upload manifest
            sess.put(
                '%s/v2/test/manifests/latest'
                % base,
                data=manifest)

            def fake_start(s):
                server.registry_state = s
                s.blobs.update(state.blobs)
                s.manifest_data = \
                    state.manifest_data
                s.manifest_event.set()
                return server

            img._start_server = fake_start
            img._stop_server = lambda s: None

            # Consume elements one at a time
            elements = []
            layer_data_list = []
            for elem in img.fetch():
                if elem.element_type == \
                        constants.IMAGE_LAYER \
                        and elem.data is not None:
                    layer_data_list.append(
                        elem.data.read())
                elements.append(elem)

            # Should have config + 3 layers
            self.assertEqual(len(elements), 4)
            self.assertEqual(
                elements[0].element_type,
                constants.CONFIG_FILE)

            # Verify each layer
            for i in range(3):
                self.assertEqual(
                    elements[i + 1].element_type,
                    constants.IMAGE_LAYER)
                self.assertEqual(
                    elements[i + 1].name,
                    uncomp_hashes[i])

            # Verify decompressed layer data
            self.assertEqual(len(layer_data_list), 3)
            for i, content in \
                    enumerate(layer_contents):
                self.assertEqual(
                    layer_data_list[i], content)
        finally:
            server.shutdown()

    @mock.patch(
        'occystrap.inputs.dockerpush'
        '.requests_unixsocket.Session')
    def test_fetch_callback_skip(self, mock_session_cls):
        """Test that fetch_callback=False skips layers."""
        layer_content = b'layer data'
        compressed, comp_hash, uncomp_hash = \
            self._make_gzip_layer(layer_content)
        config_bytes, config_hash = self._make_config(
            [uncomp_hash])
        manifest = self._make_manifest(
            config_hash, len(config_bytes),
            [(comp_hash, len(compressed))])

        mock_session = mock.MagicMock()

        def mock_request(method, url, stream=False,
                         headers=None, data=None):
            resp = mock.MagicMock()
            if method == 'POST' and '/tag' in url:
                resp.status_code = 201
            elif method == 'POST' and '/push' in url:
                resp.status_code = 200
                resp.iter_lines.return_value = []
            elif method == 'DELETE':
                resp.status_code = 200
            else:
                resp.status_code = 404
            return resp

        mock_session.request.side_effect = mock_request
        mock_session_cls.return_value = mock_session

        img = Image('test', tag='latest')
        state = _RegistryState()
        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), EmbeddedRegistryHandler)
        server.registry_state = state
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' \
            % server.server_address[1]
        sess = _no_proxy_session()

        try:
            self._upload_blob_to_server(
                sess, base, config_bytes, config_hash)
            self._upload_blob_to_server(
                sess, base, compressed, comp_hash)
            sess.put(
                '%s/v2/test/manifests/latest' % base,
                data=manifest)

            def fake_start(s):
                server.registry_state = s
                s.blobs.update(state.blobs)
                s.manifest_data = state.manifest_data
                s.manifest_event.set()
                return server

            img._start_server = fake_start
            img._stop_server = lambda s: None

            # Skip all layers
            elements = list(
                img.fetch(
                    fetch_callback=lambda d: False))

            self.assertEqual(len(elements), 2)
            # Config should still have data
            self.assertIsNotNone(elements[0].data)
            # Layer should have None data
            self.assertIsNone(elements[1].data)
        finally:
            server.shutdown()


class TestDigestMapping(unittest.TestCase):
    """Tests for digest mapping persistence."""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_save_and_load_digest_mapping(self):
        """Test round-trip of digest mapping file."""
        cache_path = os.path.join(
            self.temp_dir, 'cache.json')
        from occystrap.layer_cache import LayerCache
        cache = LayerCache(cache_path)

        img = Image(
            'test', tag='latest',
            layer_cache=cache,
            filters_hash='none')

        mappings = {
            'abc123': 'def456',
            'ghi789': 'jkl012',
        }
        img._save_digest_mapping(mappings)

        loaded = img._load_digest_mapping()
        self.assertEqual(loaded, mappings)

    def test_load_missing_file_returns_empty(self):
        """Test loading from nonexistent file."""
        cache_path = os.path.join(
            self.temp_dir, 'missing.json')
        from occystrap.layer_cache import LayerCache
        cache = LayerCache(cache_path)

        img = Image(
            'test', tag='latest',
            layer_cache=cache,
            filters_hash='none')

        loaded = img._load_digest_mapping()
        self.assertEqual(loaded, {})

    def test_no_cache_returns_empty(self):
        """Test that no cache means no mapping."""
        img = Image('test', tag='latest')
        loaded = img._load_digest_mapping()
        self.assertEqual(loaded, {})

    def test_build_skip_digests_empty_without_cache(
            self):
        """Test skip_digests is empty without cache."""
        img = Image('test', tag='latest')
        skip, mapping = img._build_skip_digests(
            lambda d: True)
        self.assertEqual(skip, set())
        self.assertEqual(mapping, {})

    def test_build_skip_digests_with_cache(self):
        """Test skip_digests populated from cache."""
        cache_path = os.path.join(
            self.temp_dir, 'cache.json')
        from occystrap.layer_cache import LayerCache
        cache = LayerCache(cache_path)

        # Record a cached layer
        cache.record(
            'diff_abc', 'none',
            'sha256:comp_xyz', 100,
            'application/vnd.docker.image.'
            'rootfs.diff.tar.gzip')

        img = Image(
            'test', tag='latest',
            layer_cache=cache,
            filters_hash='none')

        # Save a mapping:
        # docker compressed hex -> diffid hex
        img._save_digest_mapping(
            {'docker_comp_hex': 'diff_abc'})

        # Build skip digests. fetch_callback
        # returns False (meaning output says "skip").
        skip, mapping = img._build_skip_digests(
            lambda d: False)
        self.assertIn('docker_comp_hex', skip)


class TestTLSContextGeneration(unittest.TestCase):
    """Tests for self-signed TLS certificate generation."""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_generate_tls_context_returns_ssl_context(
            self):
        """Test that _generate_tls_context produces
        a valid ssl.SSLContext."""
        import ssl
        img = Image(
            'test', tag='latest',
            temp_dir=self.temp_dir)
        ctx = img._generate_tls_context()
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_generate_tls_context_cleans_up_files(
            self):
        """Test that cert/key files are deleted after
        loading into context."""
        img = Image(
            'test', tag='latest',
            temp_dir=self.temp_dir)
        img._generate_tls_context()
        # No .pem files should remain
        pem_files = [
            f for f in os.listdir(self.temp_dir)
            if f.endswith('.pem')]
        self.assertEqual(pem_files, [])


class TestURIParsing(unittest.TestCase):
    """Tests for dockerpush URI parsing."""

    def test_parse_dockerpush_uri(self):
        from occystrap import uri
        spec = uri.parse_uri('dockerpush://busybox:latest')
        image, tag, socket = \
            uri.parse_dockerpush_uri(spec)
        self.assertEqual(image, 'busybox')
        self.assertEqual(tag, 'latest')
        self.assertEqual(
            socket, '/var/run/docker.sock')

    def test_parse_dockerpush_uri_with_socket(self):
        from occystrap import uri
        spec = uri.parse_uri(
            'dockerpush://busybox:v1'
            '?socket=/run/podman/podman.sock')
        image, tag, socket = \
            uri.parse_dockerpush_uri(spec)
        self.assertEqual(image, 'busybox')
        self.assertEqual(tag, 'v1')
        self.assertEqual(
            socket, '/run/podman/podman.sock')

    def test_dockerpush_in_input_schemes(self):
        from occystrap import uri
        self.assertIn(
            'dockerpush', uri.INPUT_SCHEMES)

    def test_wrong_scheme_raises(self):
        from occystrap import uri
        spec = uri.parse_uri('docker://busybox:latest')
        with self.assertRaises(uri.URIParseError):
            uri.parse_dockerpush_uri(spec)


if __name__ == '__main__':
    unittest.main()
