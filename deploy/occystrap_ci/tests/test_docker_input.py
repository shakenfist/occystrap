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
        self.assertEqual(
            '/var/run/docker.sock', img.socket_path)

    def test_initialization_default_tag(self):
        """Test Image defaults to 'latest' tag."""
        img = input_docker.Image('myimage')
        self.assertEqual('latest', img.tag)

    def test_initialization_custom_socket(self):
        """Test Image accepts custom socket path."""
        img = input_docker.Image(
            'myimage', 'v1.0',
            socket_path='/run/podman/podman.sock')
        self.assertEqual(
            '/run/podman/podman.sock', img.socket_path)

    def test_image_property(self):
        """Test image property returns correct value."""
        img = input_docker.Image('myorg/myimage', 'v2.0')
        self.assertEqual('myorg/myimage', img.image)

    def test_tag_property(self):
        """Test tag property returns correct value."""
        img = input_docker.Image('myimage', 'v3.0')
        self.assertEqual('v3.0', img.tag)

    def test_socket_url_encoding(self):
        """Test that socket path is URL-encoded."""
        img = input_docker.Image('myimage', 'v1.0')
        url = img._socket_url('/images/myimage:v1.0/json')
        self.assertIn('%2Fvar%2Frun%2Fdocker.sock', url)
        self.assertTrue(
            url.endswith('/images/myimage:v1.0/json'))
        self.assertTrue(url.startswith('http+unix://'))

    def test_get_image_reference(self):
        """Test image reference is correctly formatted."""
        img = input_docker.Image('myimage', 'v1.0')
        self.assertEqual(
            'myimage:v1.0', img._get_image_reference())

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
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

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_inspect_image_not_found(
            self, mock_session_class):
        """Test inspect raises for missing image."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 404
        mock_session.request.return_value = mock_response
        mock_session_class.return_value = mock_session

        img = input_docker.Image('nonexistent', 'v1.0')

        with self.assertRaises(Exception) as ctx:
            img.inspect()

        self.assertIn('Image not found', str(ctx.exception))

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_request_api_error(self, mock_session_class):
        """Test _request raises on API error."""
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal server error'
        mock_session.request.return_value = mock_response
        mock_session_class.return_value = mock_session

        img = input_docker.Image('myimage', 'v1.0')

        with self.assertRaises(Exception) as ctx:
            img._request(
                'GET', '/images/myimage:v1.0/json')

        self.assertIn('500', str(ctx.exception))
        self.assertIn(
            'Internal server error', str(ctx.exception))

    def test_extract_inspect_ids(self):
        """Test _extract_inspect_ids with valid data."""
        img = input_docker.Image('myimage', 'v1.0')
        config_hex, diff_ids = img._extract_inspect_ids({
            'Id': 'sha256:abc123def456',
            'RootFS': {
                'Type': 'layers',
                'Layers': [
                    'sha256:layer1hex',
                    'sha256:layer2hex'
                ]
            }
        })
        self.assertEqual('abc123def456', config_hex)
        self.assertEqual(
            ['layer1hex', 'layer2hex'], diff_ids)

    def test_extract_inspect_ids_no_rootfs(self):
        """Test _extract_inspect_ids without RootFS."""
        img = input_docker.Image('myimage', 'v1.0')
        config_hex, diff_ids = img._extract_inspect_ids({
            'Id': 'sha256:abc123'
        })
        self.assertEqual('abc123', config_hex)
        self.assertIsNone(diff_ids)

    def test_extract_inspect_ids_no_sha256(self):
        """Test _extract_inspect_ids without sha256."""
        img = input_docker.Image('myimage', 'v1.0')
        config_hex, diff_ids = img._extract_inspect_ids({
            'Id': 'not-a-sha256'
        })
        self.assertIsNone(config_hex)
        self.assertIsNone(diff_ids)

    def test_digest_from_path_legacy(self):
        """Test _digest_from_path with legacy format."""
        img = input_docker.Image('myimage', 'v1.0')
        self.assertEqual(
            'abc123',
            img._digest_from_path('abc123/layer.tar'))

    def test_digest_from_path_oci(self):
        """Test _digest_from_path with OCI format."""
        img = input_docker.Image('myimage', 'v1.0')
        self.assertEqual(
            'abc123',
            img._digest_from_path(
                'blobs/sha256/abc123'))

    def _create_docker_save_tarball(
            self, config_data, layers,
            config_filename='abc123.json'):
        """Create a tarball in docker-save format.

        Args:
            config_data: Dict for config.json content.
            layers: List of (layer_id, layer_content).
            config_filename: Config file name.

        Returns:
            Path to the temporary tarball.
        """
        tf = tempfile.NamedTemporaryFile(
            delete=False, suffix='.tar')
        with tarfile.open(fileobj=tf, mode='w') as tar:
            # Write config
            config_json = json.dumps(
                config_data).encode('utf-8')
            ti = tarfile.TarInfo(config_filename)
            ti.size = len(config_json)
            tar.addfile(ti, io.BytesIO(config_json))

            # Write layers
            layer_paths = []
            for layer_id, layer_content in layers:
                layer_tf = io.BytesIO()
                with tarfile.open(
                        fileobj=layer_tf,
                        mode='w') as layer_tar:
                    for path, content in \
                            layer_content.items():
                        data = content.encode('utf-8') \
                            if isinstance(
                                content, str) \
                            else content
                        lti = tarfile.TarInfo(path)
                        lti.size = len(data)
                        layer_tar.addfile(
                            lti, io.BytesIO(data))
                layer_tf.seek(0)

                layer_path = '%s/layer.tar' % layer_id
                layer_paths.append(layer_path)
                layer_data = layer_tf.read()
                ti = tarfile.TarInfo(layer_path)
                ti.size = len(layer_data)
                tar.addfile(ti, io.BytesIO(layer_data))

            # Write manifest (last, like real Docker)
            manifest = [{
                'Config': config_filename,
                'RepoTags': ['myimage:v1.0'],
                'Layers': layer_paths
            }]
            manifest_json = json.dumps(
                manifest).encode('utf-8')
            ti = tarfile.TarInfo('manifest.json')
            ti.size = len(manifest_json)
            tar.addfile(ti, io.BytesIO(manifest_json))

        tf.close()
        return tf.name

    def _create_oci_tarball(
            self, config_data, layers,
            config_hex, diff_ids):
        """Create a tarball in Docker 25+ OCI format.

        Args:
            config_data: Dict for config content.
            layers: List of layer_content dicts, one per
                unique DiffID.
            config_hex: Config SHA256 hex.
            diff_ids: List of layer DiffID hex strings.
                May contain duplicates (e.g., empty
                layers). Duplicate blobs are written
                once but referenced multiple times in
                the manifest.

        Returns:
            Path to the temporary tarball.
        """
        tf = tempfile.NamedTemporaryFile(
            delete=False, suffix='.tar')
        with tarfile.open(fileobj=tf, mode='w') as tar:
            # Write config blob
            config_json = json.dumps(
                config_data).encode('utf-8')
            config_path = 'blobs/sha256/%s' % config_hex
            ti = tarfile.TarInfo(config_path)
            ti.size = len(config_json)
            tar.addfile(ti, io.BytesIO(config_json))

            # Write layer blobs (deduplicate paths)
            layer_paths = []
            written_blobs = set()
            layer_idx = 0
            for diff_id in diff_ids:
                blob_path = 'blobs/sha256/%s' % diff_id
                layer_paths.append(blob_path)

                if blob_path not in written_blobs:
                    written_blobs.add(blob_path)
                    layer_content = layers[layer_idx]
                    layer_idx += 1

                    layer_tf = io.BytesIO()
                    with tarfile.open(
                            fileobj=layer_tf,
                            mode='w') as layer_tar:
                        for path, content in \
                                layer_content.items():
                            data = \
                                content.encode('utf-8') \
                                if isinstance(
                                    content, str) \
                                else content
                            lti = tarfile.TarInfo(path)
                            lti.size = len(data)
                            layer_tar.addfile(
                                lti, io.BytesIO(data))
                    layer_tf.seek(0)

                    layer_data = layer_tf.read()
                    ti = tarfile.TarInfo(blob_path)
                    ti.size = len(layer_data)
                    tar.addfile(
                        ti, io.BytesIO(layer_data))

            # Write manifest.json (last)
            manifest = [{
                'Config': config_path,
                'RepoTags': ['myimage:v1.0'],
                'Layers': layer_paths
            }]
            manifest_json = json.dumps(
                manifest).encode('utf-8')
            ti = tarfile.TarInfo('manifest.json')
            ti.size = len(manifest_json)
            tar.addfile(ti, io.BytesIO(manifest_json))

        tf.close()
        return tf.name

    def _make_mock_session(
            self, inspect_data, tarball_path,
            mock_session_class):
        """Create a mock session with inspect and tarball.

        Returns the mock session.
        """
        mock_session = mock.MagicMock()

        inspect_response = mock.MagicMock()
        inspect_response.status_code = 200
        inspect_response.json.return_value = inspect_data

        with open(tarball_path, 'rb') as f:
            tarball_content = f.read()

        tarball_response = mock.MagicMock()
        tarball_response.status_code = 200
        tarball_response.raw = io.BytesIO(
            tarball_content)

        mock_session.request.side_effect = [
            inspect_response,
            tarball_response
        ]
        mock_session_class.return_value = mock_session
        return mock_session

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_fetch_yields_config_and_layers(
            self, mock_session_class):
        """Test fetch yields config file and layers."""
        tarball_path = self._create_docker_save_tarball(
            config_data={
                'architecture': 'amd64', 'os': 'linux'},
            layers=[
                ('layer1', {'file1.txt': 'content1'}),
                ('layer2', {'file2.txt': 'content2'}),
            ])

        try:
            self._make_mock_session(
                {'Id': 'sha256:abc123'},
                tarball_path,
                mock_session_class)

            img = input_docker.Image('myimage', 'v1.0')
            elements = list(img.fetch())

            # Should yield config + 2 layers
            self.assertEqual(3, len(elements))

            # First element is config
            self.assertEqual(
                constants.CONFIG_FILE, elements[0][0])
            self.assertIn('.json', elements[0][1])

            # Next two are layers
            self.assertEqual(
                constants.IMAGE_LAYER, elements[1][0])
            self.assertEqual('layer1', elements[1][1])
            self.assertIsNotNone(elements[1][2])

            self.assertEqual(
                constants.IMAGE_LAYER, elements[2][0])
            self.assertEqual('layer2', elements[2][1])
            self.assertIsNotNone(elements[2][2])

        finally:
            os.unlink(tarball_path)

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_fetch_early_config_from_inspect(
            self, mock_session_class):
        """Test config is yielded early via inspect data.

        When inspect provides the config hash and the
        config file appears in the stream before
        manifest.json, it should be yielded immediately.
        """
        config_hex = 'abc123'
        tarball_path = self._create_docker_save_tarball(
            config_data={
                'architecture': 'amd64', 'os': 'linux'},
            layers=[
                ('layer1', {'file1.txt': 'content1'}),
            ],
            config_filename='%s.json' % config_hex)

        try:
            self._make_mock_session(
                {
                    'Id': 'sha256:%s' % config_hex,
                    'RootFS': {
                        'Type': 'layers',
                        'Layers': ['sha256:layer1_diff']
                    }
                },
                tarball_path,
                mock_session_class)

            img = input_docker.Image('myimage', 'v1.0')
            elements = list(img.fetch())

            # Should yield config + 1 layer
            self.assertEqual(2, len(elements))

            # Config is first
            self.assertEqual(
                constants.CONFIG_FILE, elements[0][0])
            self.assertEqual(
                '%s.json' % config_hex, elements[0][1])

        finally:
            os.unlink(tarball_path)

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_fetch_oci_format_precomputed(
            self, mock_session_class):
        """Test OCI format uses pre-computed manifest.

        For Docker 25+ OCI tarballs, the manifest is
        pre-computed from inspect data and layers are
        processed immediately without waiting for
        manifest.json.
        """
        config_hex = 'cfgaaa111'
        diff_ids = ['diff111', 'diff222']

        tarball_path = self._create_oci_tarball(
            config_data={
                'architecture': 'amd64', 'os': 'linux'},
            layers=[
                {'file1.txt': 'content1'},
                {'file2.txt': 'content2'},
            ],
            config_hex=config_hex,
            diff_ids=diff_ids)

        try:
            self._make_mock_session(
                {
                    'Id': 'sha256:%s' % config_hex,
                    'RootFS': {
                        'Type': 'layers',
                        'Layers': [
                            'sha256:%s' % d
                            for d in diff_ids
                        ]
                    }
                },
                tarball_path,
                mock_session_class)

            img = input_docker.Image('myimage', 'v1.0')
            elements = list(img.fetch())

            # Should yield config + 2 layers
            self.assertEqual(3, len(elements))

            # Config is first
            self.assertEqual(
                constants.CONFIG_FILE, elements[0][0])
            self.assertEqual(
                'blobs/sha256/%s' % config_hex,
                elements[0][1])

            # Layers in order
            self.assertEqual(
                constants.IMAGE_LAYER, elements[1][0])
            self.assertEqual('diff111', elements[1][1])
            self.assertIsNotNone(elements[1][2])

            self.assertEqual(
                constants.IMAGE_LAYER, elements[2][0])
            self.assertEqual('diff222', elements[2][1])
            self.assertIsNotNone(elements[2][2])

        finally:
            os.unlink(tarball_path)

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_fetch_respects_callback(
            self, mock_session_class):
        """Test fetch respects fetch_callback."""
        tarball_path = self._create_docker_save_tarball(
            config_data={'architecture': 'amd64'},
            layers=[
                ('layer1', {'file1.txt': 'content1'}),
                ('layer2', {'file2.txt': 'content2'}),
            ])

        try:
            self._make_mock_session(
                {'Id': 'sha256:abc123'},
                tarball_path,
                mock_session_class)

            img = input_docker.Image('myimage', 'v1.0')

            def skip_layer1(digest):
                return digest != 'layer1'

            elements = list(
                img.fetch(fetch_callback=skip_layer1))

            # Should yield config + 2 layers (1 skipped)
            self.assertEqual(3, len(elements))

            # Layer1 should have None data (skipped)
            layer1_element = [
                e for e in elements
                if e[1] == 'layer1'][0]
            self.assertIsNone(layer1_element[2])

            # Layer2 should have data
            layer2_element = [
                e for e in elements
                if e[1] == 'layer2'][0]
            self.assertIsNotNone(layer2_element[2])

        finally:
            os.unlink(tarball_path)

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_fetch_oci_respects_callback(
            self, mock_session_class):
        """Test OCI format respects fetch_callback."""
        config_hex = 'cfgbbb222'
        diff_ids = ['diff333', 'diff444']

        tarball_path = self._create_oci_tarball(
            config_data={'architecture': 'amd64'},
            layers=[
                {'file1.txt': 'content1'},
                {'file2.txt': 'content2'},
            ],
            config_hex=config_hex,
            diff_ids=diff_ids)

        try:
            self._make_mock_session(
                {
                    'Id': 'sha256:%s' % config_hex,
                    'RootFS': {
                        'Type': 'layers',
                        'Layers': [
                            'sha256:%s' % d
                            for d in diff_ids
                        ]
                    }
                },
                tarball_path,
                mock_session_class)

            img = input_docker.Image('myimage', 'v1.0')

            def skip_first(digest):
                return digest != 'diff333'

            elements = list(
                img.fetch(fetch_callback=skip_first))

            self.assertEqual(3, len(elements))

            # First layer skipped (None data)
            layer1 = [
                e for e in elements
                if e[1] == 'diff333'][0]
            self.assertIsNone(layer1[2])

            # Second layer has data
            layer2 = [
                e for e in elements
                if e[1] == 'diff444'][0]
            self.assertIsNotNone(layer2[2])

        finally:
            os.unlink(tarball_path)

    @mock.patch(
        'occystrap.inputs.docker'
        '.requests_unixsocket.Session')
    def test_fetch_oci_duplicate_layer_paths(
            self, mock_session_class):
        """Test OCI format handles duplicate layer paths.

        When a manifest references the same blob path
        multiple times (e.g., empty layers from ENV/CMD
        directives), the blob only exists once in the
        tarball but must be yielded for each reference.
        """
        config_hex = 'cfgdup999'
        empty_id = 'emptyaaa'
        diff_ids = ['diff555', empty_id, 'diff666',
                    empty_id]

        tarball_path = self._create_oci_tarball(
            config_data={
                'architecture': 'amd64',
                'os': 'linux'},
            layers=[
                {'file1.txt': 'content1'},
                {},
                {'file3.txt': 'content3'},
            ],
            config_hex=config_hex,
            diff_ids=diff_ids)

        try:
            self._make_mock_session(
                {
                    'Id': 'sha256:%s' % config_hex,
                    'RootFS': {
                        'Type': 'layers',
                        'Layers': [
                            'sha256:%s' % d
                            for d in diff_ids
                        ]
                    }
                },
                tarball_path,
                mock_session_class)

            img = input_docker.Image('myimage', 'v1.0')
            elements = list(img.fetch())

            # Should yield config + 4 layers
            self.assertEqual(5, len(elements))

            # Config is first
            self.assertEqual(
                constants.CONFIG_FILE, elements[0][0])

            # All 4 layers yielded with data
            layer_elements = [
                e for e in elements
                if e[0] == constants.IMAGE_LAYER]
            self.assertEqual(4, len(layer_elements))

            # Verify each layer has data
            for elem in layer_elements:
                self.assertIsNotNone(elem[2])

            # Verify layer order matches DiffIDs
            expected_digests = [
                'diff555', empty_id,
                'diff666', empty_id]
            actual_digests = [
                e[1] for e in layer_elements]
            self.assertEqual(
                expected_digests, actual_digests)

        finally:
            os.unlink(tarball_path)

    def test_always_fetch_returns_true(self):
        """Test the always_fetch helper function."""
        self.assertTrue(
            input_docker.always_fetch('sha256:abc'))
        self.assertTrue(
            input_docker.always_fetch('anything'))
