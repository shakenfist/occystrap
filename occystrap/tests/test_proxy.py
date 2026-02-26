"""Tests for the proxy registry module."""

import gzip
import hashlib
import http.server
import json
import os
import threading
import tempfile
import unittest
from unittest import mock

import requests

from occystrap import constants
from occystrap.proxy import (
    DEFAULT_MAX_CONCURRENT,
    _ProxyState,
    _ProxyInput,
    ProxyRegistryHandler,
)


def _start_test_proxy(state=None):
    """Start a test proxy server, return
    (server, port)."""
    if state is None:
        state = _ProxyState()
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), ProxyRegistryHandler)
    server.proxy_state = state
    thread = threading.Thread(
        target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _no_proxy_session():
    """Create a requests session that bypasses proxy
    settings."""
    s = requests.Session()
    s.trust_env = False
    return s


def _upload_blob(sess, base, repo, data):
    """Upload a blob via the V2 API and return the
    digest hex."""
    blob_hash = hashlib.sha256(data).hexdigest()

    r = sess.post(
        '%s/v2/%s/blobs/uploads/' % (base, repo))
    assert r.status_code == 202
    location = r.headers['Location']

    r = sess.put(
        '%s%s?digest=sha256:%s'
        % (base, location, blob_hash),
        data=data,
        headers={
            'Content-Type':
                'application/octet-stream'
        })
    assert r.status_code == 201
    return blob_hash


def _make_test_image(layer_data_list=None):
    """Build a minimal but valid image manifest,
    config, and layers.

    Returns (manifest_bytes, config_bytes, config_hex,
             layer_blobs) where layer_blobs is a list of
             (compressed_bytes, compressed_hex,
              diff_id_hex) tuples.
    """
    if layer_data_list is None:
        layer_data_list = [b'layer-one-data']

    layer_blobs = []
    diff_ids = []

    for layer_data in layer_data_list:
        # DiffID is hash of uncompressed data
        diff_id = hashlib.sha256(
            layer_data).hexdigest()
        diff_ids.append('sha256:%s' % diff_id)

        # Compress
        compressed = gzip.compress(layer_data)
        compressed_hex = hashlib.sha256(
            compressed).hexdigest()

        layer_blobs.append(
            (compressed, compressed_hex, diff_id))

    config = {
        'architecture': 'amd64',
        'os': 'linux',
        'rootfs': {
            'type': 'layers',
            'diff_ids': diff_ids,
        },
    }
    config_bytes = json.dumps(config).encode()
    config_hex = hashlib.sha256(
        config_bytes).hexdigest()

    mt_manifest = constants.MEDIA_TYPE_DOCKER_MANIFEST_V2
    mt_config = constants.MEDIA_TYPE_DOCKER_CONFIG
    mt_layer = constants.MEDIA_TYPE_DOCKER_LAYER_GZIP

    manifest = {
        'schemaVersion': 2,
        'mediaType': mt_manifest,
        'config': {
            'mediaType': mt_config,
            'digest': 'sha256:%s' % config_hex,
            'size': len(config_bytes),
        },
        'layers': [
            {
                'mediaType': mt_layer,
                'digest': 'sha256:%s' % cb[1],
                'size': len(cb[0]),
            }
            for cb in layer_blobs
        ],
    }
    manifest_bytes = json.dumps(manifest).encode()

    return (manifest_bytes, config_bytes,
            config_hex, layer_blobs)


class TestProxyV2Check(unittest.TestCase):
    """Tests for GET /v2/ version check."""

    def setUp(self):
        self.server, self.port = _start_test_proxy()
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
            r.headers.get(
                'Docker-Distribution-API-Version'),
            'registry/2.0')

    def test_unknown_get_returns_404(self):
        r = self.sess.get(
            '%s/v2/unknown' % self.base)
        self.assertEqual(r.status_code, 404)


class TestProxyHEAD(unittest.TestCase):
    """Tests for HEAD /v2/{name}/blobs/{digest}."""

    def setUp(self):
        self.state = _ProxyState()
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()

    def test_head_returns_404_unknown(self):
        r = self.sess.head(
            '%s/v2/test/blobs/sha256:abc123'
            % self.base)
        self.assertEqual(r.status_code, 404)

    def test_head_returns_200_for_uploaded_blob(self):
        """HEAD returns 200 after a blob is uploaded."""
        blob_data = b'test blob data'
        blob_hex = _upload_blob(
            self.sess, self.base, 'test', blob_data)

        r = self.sess.head(
            '%s/v2/test/blobs/sha256:%s'
            % (self.base, blob_hex))
        self.assertEqual(r.status_code, 200)


class TestProxyBlobUpload(unittest.TestCase):
    """Tests for the blob upload flow."""

    def setUp(self):
        self.state = _ProxyState()
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()
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
        blob_data = b'hello world blob data'
        blob_hash = hashlib.sha256(
            blob_data).hexdigest()

        r = self.sess.post(
            '%s/v2/test/blobs/uploads/' % self.base)
        self.assertEqual(r.status_code, 202)
        location = r.headers['Location']

        r = self.sess.patch(
            '%s%s' % (self.base, location),
            data=blob_data,
            headers={
                'Content-Type':
                    'application/octet-stream'
            })
        self.assertEqual(r.status_code, 202)

        r = self.sess.put(
            '%s%s?digest=sha256:%s'
            % (self.base, location, blob_hash))
        self.assertEqual(r.status_code, 201)
        self.assertIn(
            blob_hash, self.state.blobs)

    def test_monolithic_upload(self):
        blob_data = b'monolithic blob content'
        blob_hash = hashlib.sha256(
            blob_data).hexdigest()

        r = self.sess.post(
            '%s/v2/test/blobs/uploads/' % self.base)
        location = r.headers['Location']

        r = self.sess.put(
            '%s%s?digest=sha256:%s'
            % (self.base, location, blob_hash),
            data=blob_data,
            headers={
                'Content-Type':
                    'application/octet-stream'
            })
        self.assertEqual(r.status_code, 201)
        self.assertIn(
            blob_hash, self.state.blobs)

    def test_digest_mismatch_returns_400(self):
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
        self.assertNotIn(
            wrong_hash, self.state.blobs)


class TestProxyManifest(unittest.TestCase):
    """Tests for manifest PUT with pipeline
    processing."""

    def setUp(self):
        self.state = _ProxyState(
            downstream_uri='localhost:9999')
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()
        for path in self.state.blobs.values():
            try:
                os.unlink(path)
            except OSError:
                pass

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_manifest_put_processes_image(
            self, mock_builder_cls):
        """Manifest PUT triggers pipeline processing
        and returns 201 on success."""
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder

        mock_output = mock.MagicMock()
        mock_output.requires_ordered_layers = True
        mock_output.fetch_callback = \
            mock.MagicMock(return_value=True)
        mock_builder.build_output.return_value = \
            mock_output

        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image()

        # Upload config blob
        _upload_blob(
            self.sess, self.base, 'test/myimage',
            config_bytes)

        # Upload layer blobs
        for compressed, _, _ in layer_blobs:
            _upload_blob(
                self.sess, self.base,
                'test/myimage', compressed)

        # Push manifest
        r = self.sess.put(
            '%s/v2/test/myimage/manifests/latest'
            % self.base,
            data=manifest_bytes,
            headers={
                'Content-Type':
                    constants
                    .MEDIA_TYPE_DOCKER_MANIFEST_V2
            })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(
            self.state.images_processed, 1)
        self.assertEqual(
            self.state.images_failed, 0)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_manifest_put_returns_500_on_error(
            self, mock_builder_cls):
        """Manifest PUT returns 500 when pipeline
        processing fails."""
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_builder.build_output.side_effect = \
            Exception('downstream unreachable')

        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image()

        _upload_blob(
            self.sess, self.base, 'test/myimage',
            config_bytes)
        for compressed, _, _ in layer_blobs:
            _upload_blob(
                self.sess, self.base,
                'test/myimage', compressed)

        r = self.sess.put(
            '%s/v2/test/myimage/manifests/latest'
            % self.base,
            data=manifest_bytes)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(
            self.state.images_failed, 1)

    def test_manifest_list_returns_400(self):
        """Manifest list (multi-arch) is rejected."""
        mt = constants.MEDIA_TYPE_DOCKER_MANIFEST_V2
        manifest_list = json.dumps({
            'schemaVersion': 2,
            'manifests': [
                {
                    'mediaType': mt,
                    'digest': 'sha256:abc',
                    'size': 100,
                }
            ],
        }).encode()

        r = self.sess.put(
            '%s/v2/test/manifests/latest'
            % self.base,
            data=manifest_list)
        self.assertEqual(r.status_code, 400)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_blobs_cleaned_up_after_processing(
            self, mock_builder_cls):
        """Blobs referenced by a manifest are cleaned
        up after processing."""
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder

        mock_output = mock.MagicMock()
        mock_output.requires_ordered_layers = True
        mock_output.fetch_callback = \
            mock.MagicMock(return_value=True)
        mock_builder.build_output.return_value = \
            mock_output

        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image()

        _upload_blob(
            self.sess, self.base, 'test/img',
            config_bytes)
        for compressed, _, _ in layer_blobs:
            _upload_blob(
                self.sess, self.base,
                'test/img', compressed)

        # Verify blobs exist before manifest
        self.assertTrue(len(self.state.blobs) > 0)

        r = self.sess.put(
            '%s/v2/test/img/manifests/latest'
            % self.base,
            data=manifest_bytes)
        self.assertEqual(r.status_code, 201)

        # Blobs should be cleaned up
        self.assertEqual(len(self.state.blobs), 0)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_manifest_path_parsing(
            self, mock_builder_cls):
        """Repository name and tag are extracted
        correctly from the manifest path."""
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder

        mock_output = mock.MagicMock()
        mock_output.requires_ordered_layers = True
        mock_output.fetch_callback = \
            mock.MagicMock(return_value=True)
        mock_builder.build_output.return_value = \
            mock_output

        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image()

        _upload_blob(
            self.sess, self.base,
            'kolla/nova-api', config_bytes)
        for compressed, _, _ in layer_blobs:
            _upload_blob(
                self.sess, self.base,
                'kolla/nova-api', compressed)

        r = self.sess.put(
            '%s/v2/kolla/nova-api/manifests/2024.1'
            % self.base,
            data=manifest_bytes)
        self.assertEqual(r.status_code, 201)

        # Verify the downstream URI was built
        # correctly
        call_args = \
            mock_builder.build_output.call_args
        dest_spec = call_args[0][0]
        # The dest spec should reference
        # kolla/nova-api
        self.assertIn(
            'kolla/nova-api',
            str(dest_spec))


class TestProxyInput(unittest.TestCase):
    """Tests for _ProxyInput synthetic input."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_yields_config_and_layers(self):
        """ProxyInput yields config then layers in
        order."""
        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image(
                [b'layer-one', b'layer-two'])

        # Write blobs to temp files
        blobs = {}
        blobs[config_hex] = os.path.join(
            self.tmpdir, 'config')
        with open(blobs[config_hex], 'wb') as f:
            f.write(config_bytes)

        for compressed, compressed_hex, _ in \
                layer_blobs:
            blob_path = os.path.join(
                self.tmpdir, compressed_hex)
            with open(blob_path, 'wb') as f:
                f.write(compressed)
            blobs[compressed_hex] = blob_path

        proxy_input = _ProxyInput(
            'test', 'latest', manifest_bytes,
            blobs, temp_dir=self.tmpdir)

        elements = list(proxy_input.fetch())

        # First element should be config
        self.assertEqual(
            elements[0].element_type,
            constants.CONFIG_FILE)

        # Remaining elements should be layers
        for i in range(1, len(elements)):
            self.assertEqual(
                elements[i].element_type,
                constants.IMAGE_LAYER)

        # Should have 1 config + 2 layers
        self.assertEqual(len(elements), 3)

    def test_fetch_callback_skips_layers(self):
        """Layers can be skipped via fetch_callback."""
        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image([b'layer-one'])

        blobs = {}
        blobs[config_hex] = os.path.join(
            self.tmpdir, 'config')
        with open(blobs[config_hex], 'wb') as f:
            f.write(config_bytes)

        for compressed, compressed_hex, _ in \
                layer_blobs:
            blob_path = os.path.join(
                self.tmpdir, compressed_hex)
            with open(blob_path, 'wb') as f:
                f.write(compressed)
            blobs[compressed_hex] = blob_path

        proxy_input = _ProxyInput(
            'test', 'latest', manifest_bytes,
            blobs, temp_dir=self.tmpdir)

        def skip_all(digest):
            return False

        elements = list(
            proxy_input.fetch(
                fetch_callback=skip_all))

        # Config + 1 skipped layer
        self.assertEqual(len(elements), 2)
        # Skipped layer has data=None
        self.assertIsNone(elements[1].data)

    def test_missing_config_raises(self):
        """Missing config blob raises an exception."""
        manifest = json.dumps({
            'schemaVersion': 2,
            'config': {
                'digest': 'sha256:missing',
                'size': 10,
            },
            'layers': [],
        }).encode()

        proxy_input = _ProxyInput(
            'test', 'latest', manifest, {})

        with self.assertRaises(Exception):
            list(proxy_input.fetch())

    def test_properties(self):
        proxy_input = _ProxyInput(
            'myimage', 'v1', b'{}', {})
        self.assertEqual(
            proxy_input.image, 'myimage')
        self.assertEqual(proxy_input.tag, 'v1')


class TestProxyState(unittest.TestCase):
    """Tests for _ProxyState."""

    def test_default_state(self):
        state = _ProxyState()
        self.assertEqual(state.uploads, {})
        self.assertEqual(state.blobs, {})
        self.assertEqual(state.images_processed, 0)
        self.assertEqual(state.images_failed, 0)
        self.assertIsNone(state.downstream_uri)
        self.assertEqual(state.filter_strs, [])

    def test_state_with_config(self):
        state = _ProxyState(
            downstream_uri='ghcr.io',
            filter_strs=['normalize-timestamps'])
        self.assertEqual(
            state.downstream_uri, 'ghcr.io')
        self.assertEqual(
            state.filter_strs,
            ['normalize-timestamps'])

    def test_default_max_concurrent(self):
        state = _ProxyState()
        self.assertEqual(
            DEFAULT_MAX_CONCURRENT, 4)
        # Semaphore allows DEFAULT_MAX_CONCURRENT
        # concurrent acquires
        for _ in range(DEFAULT_MAX_CONCURRENT):
            self.assertTrue(
                state.processing_semaphore
                .acquire(blocking=False))
        # Next acquire should block (non-blocking
        # returns False)
        self.assertFalse(
            state.processing_semaphore
            .acquire(blocking=False))

    def test_custom_max_concurrent(self):
        state = _ProxyState(max_concurrent=2)
        self.assertTrue(
            state.processing_semaphore
            .acquire(blocking=False))
        self.assertTrue(
            state.processing_semaphore
            .acquire(blocking=False))
        self.assertFalse(
            state.processing_semaphore
            .acquire(blocking=False))

    def test_blob_refcounts_initialized(self):
        state = _ProxyState()
        self.assertEqual(state.blob_refcounts, {})

    def test_active_processing_initialized(self):
        state = _ProxyState()
        self.assertEqual(state.active_processing, 0)


class TestConcurrentManifests(unittest.TestCase):
    """Tests for concurrent manifest processing."""

    def setUp(self):
        self.state = _ProxyState(
            downstream_uri='localhost:9999',
            max_concurrent=4)
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()
        for path in self.state.blobs.values():
            try:
                os.unlink(path)
            except OSError:
                pass

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_concurrent_manifests_both_succeed(
            self, mock_builder_cls):
        """Two images pushed simultaneously both
        succeed (201)."""
        # Gate to hold processing until both
        # manifests are in-flight.
        gate = threading.Event()

        def slow_build_output(*args, **kwargs):
            gate.wait(timeout=10)
            out = mock.MagicMock()
            out.requires_ordered_layers = True
            out.fetch_callback = mock.MagicMock(
                return_value=True)
            return out

        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_builder.build_output.side_effect = \
            slow_build_output

        # Build two distinct images
        img1 = _make_test_image([b'image-1-layer'])
        img2 = _make_test_image([b'image-2-layer'])

        # Upload blobs for image 1
        _upload_blob(
            self.sess, self.base, 'img1',
            img1[1])
        for c, _, _ in img1[3]:
            _upload_blob(
                self.sess, self.base, 'img1', c)

        # Upload blobs for image 2
        _upload_blob(
            self.sess, self.base, 'img2',
            img2[1])
        for c, _, _ in img2[3]:
            _upload_blob(
                self.sess, self.base, 'img2', c)

        results = [None, None]

        def push_manifest(idx, repo, manifest):
            s = _no_proxy_session()
            results[idx] = s.put(
                '%s/v2/%s/manifests/latest'
                % (self.base, repo),
                data=manifest,
                headers={
                    'Content-Type':
                        constants
                        .MEDIA_TYPE_DOCKER_MANIFEST_V2
                })

        t1 = threading.Thread(
            target=push_manifest,
            args=(0, 'img1', img1[0]))
        t2 = threading.Thread(
            target=push_manifest,
            args=(1, 'img2', img2[0]))

        t1.start()
        t2.start()

        # Let both threads enter processing
        import time
        time.sleep(0.3)

        # Release the gate
        gate.set()

        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(results[0].status_code, 201)
        self.assertEqual(results[1].status_code, 201)
        self.assertEqual(
            self.state.images_processed, 2)
        self.assertEqual(
            self.state.images_failed, 0)
        self.assertEqual(
            self.state.active_processing, 0)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_backpressure_serializes_manifests(
            self, mock_builder_cls):
        """With max_concurrent=1, two concurrent
        manifests are serialized."""
        # Recreate state with max_concurrent=1
        self.state = _ProxyState(
            downstream_uri='localhost:9999',
            max_concurrent=1)
        self.server.shutdown()
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

        processing_order = []
        gate1 = threading.Event()

        call_count = [0]
        call_lock = threading.Lock()

        def tracked_build_output(*args, **kwargs):
            with call_lock:
                call_count[0] += 1
                my_idx = call_count[0]
            processing_order.append(
                'start-%d' % my_idx)
            if my_idx == 1:
                # First call blocks until released
                gate1.wait(timeout=10)
            out = mock.MagicMock()
            out.requires_ordered_layers = True
            out.fetch_callback = mock.MagicMock(
                return_value=True)
            processing_order.append(
                'end-%d' % my_idx)
            return out

        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_builder.build_output.side_effect = \
            tracked_build_output

        img1 = _make_test_image([b'ser-layer-1'])
        img2 = _make_test_image([b'ser-layer-2'])

        # Upload blobs for both
        _upload_blob(
            self.sess, self.base, 'a',
            img1[1])
        for c, _, _ in img1[3]:
            _upload_blob(
                self.sess, self.base, 'a', c)

        _upload_blob(
            self.sess, self.base, 'b',
            img2[1])
        for c, _, _ in img2[3]:
            _upload_blob(
                self.sess, self.base, 'b', c)

        results = [None, None]

        def push(idx, repo, manifest):
            s = _no_proxy_session()
            results[idx] = s.put(
                '%s/v2/%s/manifests/latest'
                % (self.base, repo),
                data=manifest,
                headers={
                    'Content-Type':
                        constants
                        .MEDIA_TYPE_DOCKER_MANIFEST_V2
                })

        t1 = threading.Thread(
            target=push,
            args=(0, 'a', img1[0]))
        t2 = threading.Thread(
            target=push,
            args=(1, 'b', img2[0]))

        t1.start()
        import time
        time.sleep(0.3)
        t2.start()
        time.sleep(0.3)

        # Release first manifest
        gate1.set()

        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(results[0].status_code, 201)
        self.assertEqual(results[1].status_code, 201)

        # Second processing must start after first
        # due to max_concurrent=1
        self.assertIn('start-1', processing_order)
        self.assertIn('end-1', processing_order)
        self.assertIn('start-2', processing_order)
        idx_end1 = processing_order.index('end-1')
        idx_start2 = processing_order.index(
            'start-2')
        self.assertLess(idx_end1, idx_start2)


class TestBlobRefcounting(unittest.TestCase):
    """Tests for blob reference counting during
    concurrent processing."""

    def setUp(self):
        self.state = _ProxyState(
            downstream_uri='localhost:9999',
            max_concurrent=4)
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()
        for path in self.state.blobs.values():
            try:
                os.unlink(path)
            except OSError:
                pass

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_shared_blob_survives_first_finish(
            self, mock_builder_cls):
        """A shared blob is not deleted when the first
        manifest finishes but the second is still
        processing."""
        gate1 = threading.Event()
        gate2 = threading.Event()

        call_count = [0]
        call_lock = threading.Lock()

        shared_blob_exists = [None]

        def gated_build_output(*args, **kwargs):
            with call_lock:
                call_count[0] += 1
                my_idx = call_count[0]

            if my_idx == 1:
                # First finishes quickly
                gate1.wait(timeout=10)
            else:
                # Second waits then checks blob
                gate2.wait(timeout=10)
                # Check if shared blob still exists
                with self.state.lock:
                    shared_blob_exists[0] = bool(
                        self.state.blobs)

            out = mock.MagicMock()
            out.requires_ordered_layers = True
            out.fetch_callback = mock.MagicMock(
                return_value=True)
            return out

        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_builder.build_output.side_effect = \
            gated_build_output

        # Both images share the same layer blob
        shared_layer = b'shared-layer-data'
        img1 = _make_test_image([shared_layer])
        img2 = _make_test_image([shared_layer])

        # Upload shared blobs (upload once, both
        # manifests reference same digests)
        _upload_blob(
            self.sess, self.base, 'x',
            img1[1])
        for c, _, _ in img1[3]:
            _upload_blob(
                self.sess, self.base, 'x', c)
        # Upload config for second image (different
        # config since different diff_ids layout)
        _upload_blob(
            self.sess, self.base, 'y',
            img2[1])

        results = [None, None]

        def push(idx, repo, manifest):
            s = _no_proxy_session()
            results[idx] = s.put(
                '%s/v2/%s/manifests/latest'
                % (self.base, repo),
                data=manifest,
                headers={
                    'Content-Type':
                        constants
                        .MEDIA_TYPE_DOCKER_MANIFEST_V2
                })

        t1 = threading.Thread(
            target=push, args=(0, 'x', img1[0]))
        t2 = threading.Thread(
            target=push, args=(1, 'y', img2[0]))

        t1.start()
        t2.start()

        import time
        time.sleep(0.3)

        # Release first manifest (it finishes)
        gate1.set()
        time.sleep(0.5)

        # Now release second -- it checks blob
        gate2.set()

        t1.join(timeout=10)
        t2.join(timeout=10)

        # The shared blob should have survived
        # the first manifest's cleanup because
        # the refcount was > 1
        self.assertTrue(
            shared_blob_exists[0],
            'Shared blob was deleted while '
            'second manifest was still processing')

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_refcounts_zero_after_completion(
            self, mock_builder_cls):
        """After all manifests complete, all blob
        refcounts should be cleared."""
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder

        mock_output = mock.MagicMock()
        mock_output.requires_ordered_layers = True
        mock_output.fetch_callback = \
            mock.MagicMock(return_value=True)
        mock_builder.build_output.return_value = \
            mock_output

        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image()

        _upload_blob(
            self.sess, self.base, 'test',
            config_bytes)
        for c, _, _ in layer_blobs:
            _upload_blob(
                self.sess, self.base, 'test', c)

        r = self.sess.put(
            '%s/v2/test/manifests/latest'
            % self.base,
            data=manifest_bytes,
            headers={
                'Content-Type':
                    constants
                    .MEDIA_TYPE_DOCKER_MANIFEST_V2
            })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(
            self.state.blob_refcounts, {})
        self.assertEqual(
            self.state.active_processing, 0)


class TestPullThrough(unittest.TestCase):
    """Tests for pull-through proxy support."""

    def setUp(self):
        self.state = _ProxyState(
            downstream_uri='downstream.example.com',
            upstream_uri='upstream.example.com')
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()

    def test_pull_disabled_without_upstream(self):
        """GET manifest returns 404 when no upstream
        is configured."""
        state = _ProxyState(
            downstream_uri='downstream.example.com')
        server, port = _start_test_proxy(state)
        base = 'http://127.0.0.1:%d' % port
        sess = _no_proxy_session()
        try:
            r = sess.get(
                '%s/v2/test/manifests/latest' % base)
            self.assertEqual(r.status_code, 404)
        finally:
            server.shutdown()

    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_manifest_get_cache_hit(
            self, mock_image_cls):
        """When downstream has the image, serve it
        directly without upstream fetch."""
        manifest_data = json.dumps({
            'schemaVersion': 2,
            'config': {'digest': 'sha256:abc'},
            'layers': [],
        }).encode()
        manifest_digest = (
            'sha256:%s'
            % hashlib.sha256(
                manifest_data).hexdigest())

        mock_img = mock.MagicMock()
        mock_image_cls.return_value = mock_img

        mock_resp = mock.MagicMock()
        mock_resp.headers = {
            'Content-Type':
                constants
                .MEDIA_TYPE_DOCKER_MANIFEST_V2,
            'Docker-Content-Digest':
                manifest_digest,
            'Content-Length':
                str(len(manifest_data)),
        }
        mock_resp.iter_content.return_value = [
            manifest_data]

        # First call is cache check (GET), succeeds
        mock_img.request_url.return_value = mock_resp

        r = self.sess.get(
            '%s/v2/myimage/manifests/latest'
            % self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, manifest_data)

        self.assertEqual(
            self.state.pull_cache_hits, 1)
        self.assertEqual(
            self.state.images_pulled, 1)
        self.assertEqual(
            self.state.pull_cache_misses, 0)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_manifest_get_cache_miss(
            self, mock_image_cls,
            mock_builder_cls):
        """When downstream misses, fetch from upstream,
        filter, push downstream, then serve."""
        manifest_data = json.dumps({
            'schemaVersion': 2,
            'config': {'digest': 'sha256:abc'},
            'layers': [],
        }).encode()
        manifest_digest = (
            'sha256:%s'
            % hashlib.sha256(
                manifest_data).hexdigest())

        call_count = [0]

        def image_side_effect(*args, **kwargs):
            img = mock.MagicMock()

            def request_url_effect(
                    method, url, headers=None,
                    stream=False):
                call_count[0] += 1
                if call_count[0] <= 2:
                    # First two calls: cache miss
                    from occystrap.util import (
                        APIException)
                    raise APIException(
                        'not found', 'GET', url,
                        404, '', {})

                # Third call: cache hit (after
                # processing)
                resp = mock.MagicMock()
                resp.headers = {
                    'Content-Type':
                        constants
                        .MEDIA_TYPE_DOCKER_MANIFEST_V2,
                    'Docker-Content-Digest':
                        manifest_digest,
                    'Content-Length':
                        str(len(manifest_data)),
                }
                resp.iter_content.return_value = [
                    manifest_data]
                return resp

            img.request_url.side_effect = (
                request_url_effect)
            return img

        mock_image_cls.side_effect = (
            image_side_effect)

        # Mock the pipeline builder
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_output = mock.MagicMock()
        mock_output.requires_ordered_layers = True
        mock_output.fetch_callback = (
            mock.MagicMock(return_value=True))
        mock_builder.build_output.return_value = (
            mock_output)

        r = self.sess.get(
            '%s/v2/myimage/manifests/latest'
            % self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, manifest_data)

        self.assertEqual(
            self.state.pull_cache_misses, 1)
        self.assertEqual(
            self.state.images_pulled, 1)

    def test_blob_get_404_without_upstream(self):
        """GET blob returns 404 when no upstream
        configured."""
        state = _ProxyState(
            downstream_uri='downstream.example.com')
        server, port = _start_test_proxy(state)
        base = 'http://127.0.0.1:%d' % port
        sess = _no_proxy_session()
        try:
            r = sess.get(
                '%s/v2/test/blobs/sha256:%s'
                % (base, 'a' * 64))
            self.assertEqual(r.status_code, 404)
        finally:
            server.shutdown()

    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_blob_get_proxies_from_downstream(
            self, mock_image_cls):
        """GET blob streams from downstream."""
        blob_data = b'layer data content'

        mock_img = mock.MagicMock()
        mock_image_cls.return_value = mock_img

        mock_resp = mock.MagicMock()
        mock_resp.headers = {
            'Content-Type':
                'application/octet-stream',
            'Content-Length':
                str(len(blob_data)),
            'Docker-Content-Digest':
                'sha256:abc123',
        }
        mock_resp.iter_content.return_value = [
            blob_data]
        mock_img.request_url.return_value = mock_resp

        r = self.sess.get(
            '%s/v2/myimage/blobs/sha256:%s'
            % (self.base, 'a' * 64))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, blob_data)

    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_blob_get_returns_404_on_miss(
            self, mock_image_cls):
        """GET blob returns 404 when not in
        downstream."""
        mock_img = mock.MagicMock()
        mock_image_cls.return_value = mock_img

        from occystrap.util import APIException
        mock_img.request_url.side_effect = (
            APIException(
                'not found', 'GET', 'url',
                404, '', {}))

        r = self.sess.get(
            '%s/v2/myimage/blobs/sha256:%s'
            % (self.base, 'a' * 64))
        self.assertEqual(r.status_code, 404)

    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_head_manifest_returns_200(
            self, mock_image_cls):
        """HEAD manifest returns 200 when image
        exists in downstream."""
        mock_img = mock.MagicMock()
        mock_image_cls.return_value = mock_img

        mock_resp = mock.MagicMock()
        mock_resp.headers = {
            'Content-Type':
                constants
                .MEDIA_TYPE_DOCKER_MANIFEST_V2,
            'Docker-Content-Digest':
                'sha256:abc123',
            'Content-Length': '500',
        }
        mock_img.request_url.return_value = mock_resp

        r = self.sess.head(
            '%s/v2/myimage/manifests/latest'
            % self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.headers.get(
                'Docker-Content-Digest'),
            'sha256:abc123')

    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_head_manifest_returns_404(
            self, mock_image_cls):
        """HEAD manifest returns 404 when image not
        in downstream."""
        mock_img = mock.MagicMock()
        mock_image_cls.return_value = mock_img

        from occystrap.util import APIException
        mock_img.request_url.side_effect = (
            APIException(
                'not found', 'HEAD', 'url',
                404, '', {}))

        r = self.sess.head(
            '%s/v2/myimage/manifests/latest'
            % self.base)
        self.assertEqual(r.status_code, 404)

    def test_pull_stats_initialized(self):
        """Pull stats are initialized to zero."""
        state = _ProxyState()
        self.assertEqual(state.images_pulled, 0)
        self.assertEqual(state.pull_cache_hits, 0)
        self.assertEqual(state.pull_cache_misses, 0)

    def test_upstream_fields_stored(self):
        """Upstream URI and credentials are stored."""
        state = _ProxyState(
            upstream_uri='docker.io',
            upstream_username='user',
            upstream_password='pass')
        self.assertEqual(
            state.upstream_uri, 'docker.io')
        self.assertEqual(
            state.upstream_username, 'user')
        self.assertEqual(
            state.upstream_password, 'pass')

    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_downstream_image_cached(
            self, mock_image_cls):
        """Downstream Image instances are cached
        per repo_name."""
        mock_img = mock.MagicMock()
        mock_image_cls.return_value = mock_img

        from occystrap.util import APIException
        mock_img.request_url.side_effect = (
            APIException(
                'not found', 'GET', 'url',
                404, '', {}))

        # Two requests for the same repo
        self.sess.get(
            '%s/v2/myimage/blobs/sha256:%s'
            % (self.base, 'a' * 64))
        self.sess.get(
            '%s/v2/myimage/blobs/sha256:%s'
            % (self.base, 'b' * 64))

        # Image class should be instantiated once
        # for 'myimage'
        self.assertEqual(
            mock_image_cls.call_count, 1)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_concurrent_pull_deduplicates(
            self, mock_image_cls,
            mock_builder_cls):
        """Two concurrent pulls of the same image
        only trigger one upstream fetch."""
        manifest_data = json.dumps({
            'schemaVersion': 2,
            'config': {'digest': 'sha256:abc'},
            'layers': [],
        }).encode()
        manifest_digest = (
            'sha256:%s'
            % hashlib.sha256(
                manifest_data).hexdigest())

        gate = threading.Event()
        upstream_fetch_count = [0]
        fetch_lock = threading.Lock()

        def image_side_effect(*args, **kwargs):
            img = mock.MagicMock()
            # Track per-instance call count
            img._call_count = [0]

            def request_url_effect(
                    method, url, headers=None,
                    stream=False):
                img._call_count[0] += 1
                if img._call_count[0] <= 2:
                    from occystrap.util import (
                        APIException)
                    raise APIException(
                        'not found', 'GET', url,
                        404, '', {})

                resp = mock.MagicMock()
                resp.headers = {
                    'Content-Type':
                        constants
                        .MEDIA_TYPE_DOCKER_MANIFEST_V2,
                    'Docker-Content-Digest':
                        manifest_digest,
                    'Content-Length':
                        str(len(manifest_data)),
                }
                resp.iter_content.return_value = [
                    manifest_data]
                return resp

            img.request_url.side_effect = (
                request_url_effect)
            return img

        mock_image_cls.side_effect = (
            image_side_effect)

        # Mock pipeline builder to track
        # upstream fetches
        def slow_build_output(*args, **kwargs):
            with fetch_lock:
                upstream_fetch_count[0] += 1
            gate.wait(timeout=10)
            out = mock.MagicMock()
            out.requires_ordered_layers = True
            out.fetch_callback = mock.MagicMock(
                return_value=True)
            return out

        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_builder.build_output.side_effect = (
            slow_build_output)

        results = [None, None]

        def pull(idx):
            s = _no_proxy_session()
            results[idx] = s.get(
                '%s/v2/myimage/manifests/latest'
                % self.base)

        t1 = threading.Thread(
            target=pull, args=(0,))
        t2 = threading.Thread(
            target=pull, args=(1,))

        t1.start()
        import time
        time.sleep(0.3)
        t2.start()
        time.sleep(0.3)

        gate.set()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Only one upstream fetch should have
        # happened
        self.assertEqual(
            upstream_fetch_count[0], 1)

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    @mock.patch(
        'occystrap.proxy.input_registry.Image')
    def test_pull_uses_processing_semaphore(
            self, mock_image_cls,
            mock_builder_cls):
        """Pull-through respects the processing
        semaphore for backpressure."""
        # Recreate with max_concurrent=1
        self.server.shutdown()
        self.state = _ProxyState(
            downstream_uri='downstream.example.com',
            upstream_uri='upstream.example.com',
            max_concurrent=1)
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port

        manifest_data = json.dumps({
            'schemaVersion': 2,
            'config': {'digest': 'sha256:abc'},
            'layers': [],
        }).encode()
        manifest_digest = (
            'sha256:%s'
            % hashlib.sha256(
                manifest_data).hexdigest())

        def image_side_effect(*args, **kwargs):
            img = mock.MagicMock()
            img._call_count = [0]

            def request_url_effect(
                    method, url, headers=None,
                    stream=False):
                img._call_count[0] += 1
                if img._call_count[0] <= 2:
                    from occystrap.util import (
                        APIException)
                    raise APIException(
                        'not found', 'GET', url,
                        404, '', {})

                resp = mock.MagicMock()
                resp.headers = {
                    'Content-Type':
                        constants
                        .MEDIA_TYPE_DOCKER_MANIFEST_V2,
                    'Docker-Content-Digest':
                        manifest_digest,
                    'Content-Length':
                        str(len(manifest_data)),
                }
                resp.iter_content.return_value = [
                    manifest_data]
                return resp

            img.request_url.side_effect = (
                request_url_effect)
            return img

        mock_image_cls.side_effect = (
            image_side_effect)

        gate = threading.Event()
        processing_order = []
        call_count = [0]
        call_lock = threading.Lock()

        def tracked_build_output(*args, **kwargs):
            with call_lock:
                call_count[0] += 1
                my_idx = call_count[0]
            processing_order.append(
                'start-%d' % my_idx)
            if my_idx == 1:
                gate.wait(timeout=10)
            out = mock.MagicMock()
            out.requires_ordered_layers = True
            out.fetch_callback = mock.MagicMock(
                return_value=True)
            processing_order.append(
                'end-%d' % my_idx)
            return out

        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder
        mock_builder.build_output.side_effect = (
            tracked_build_output)

        results = [None, None]

        def pull(idx, repo):
            s = _no_proxy_session()
            results[idx] = s.get(
                '%s/v2/%s/manifests/latest'
                % (self.base, repo))

        t1 = threading.Thread(
            target=pull, args=(0, 'img1'))
        t2 = threading.Thread(
            target=pull, args=(1, 'img2'))

        t1.start()
        import time
        time.sleep(0.3)
        t2.start()
        time.sleep(0.3)

        gate.set()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(results[1].status_code, 200)

        # With max_concurrent=1, second pull must
        # start after first finishes
        self.assertIn('start-1', processing_order)
        self.assertIn('end-1', processing_order)
        self.assertIn('start-2', processing_order)
        idx_end1 = processing_order.index('end-1')
        idx_start2 = processing_order.index(
            'start-2')
        self.assertLess(idx_end1, idx_start2)


class TestBuildOutputPipeline(unittest.TestCase):
    """Tests for the _build_output_pipeline
    refactor."""

    def setUp(self):
        self.state = _ProxyState(
            downstream_uri='localhost:9999')
        self.server, self.port = _start_test_proxy(
            self.state)
        self.base = 'http://127.0.0.1:%d' % self.port
        self.sess = _no_proxy_session()

    def tearDown(self):
        self.server.shutdown()
        for path in self.state.blobs.values():
            try:
                os.unlink(path)
            except OSError:
                pass

    @mock.patch(
        'occystrap.proxy.PipelineBuilder')
    def test_push_still_works_after_refactor(
            self, mock_builder_cls):
        """Existing push path still works after
        _process_image refactoring."""
        mock_builder = mock.MagicMock()
        mock_builder_cls.return_value = mock_builder

        mock_output = mock.MagicMock()
        mock_output.requires_ordered_layers = True
        mock_output.fetch_callback = \
            mock.MagicMock(return_value=True)
        mock_builder.build_output.return_value = \
            mock_output

        (manifest_bytes, config_bytes,
         config_hex, layer_blobs) = \
            _make_test_image()

        _upload_blob(
            self.sess, self.base, 'test/img',
            config_bytes)
        for compressed, _, _ in layer_blobs:
            _upload_blob(
                self.sess, self.base,
                'test/img', compressed)

        r = self.sess.put(
            '%s/v2/test/img/manifests/latest'
            % self.base,
            data=manifest_bytes,
            headers={
                'Content-Type':
                    constants
                    .MEDIA_TYPE_DOCKER_MANIFEST_V2
            })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(
            self.state.images_processed, 1)
