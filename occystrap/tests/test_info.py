"""Tests for the info command and get_manifest/get_config."""

import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner

from occystrap import constants
from occystrap.inputs.base import ImageInput
from occystrap.main import (
    _build_info, _format_size, cli)


# --- Helpers ---

def _make_manifest(layer_count=2,
                   compression='gzip'):
    """Build a Docker v2 distribution manifest."""
    if compression == 'gzip':
        media = constants.MEDIA_TYPE_DOCKER_LAYER_GZIP
    elif compression == 'zstd':
        media = constants.MEDIA_TYPE_DOCKER_LAYER_ZSTD
    else:
        media = constants.MEDIA_TYPE_OCI_LAYER_GZIP

    layers = []
    for i in range(layer_count):
        layers.append({
            'mediaType': media,
            'size': (i + 1) * 1000,
            'digest': 'sha256:%s' % ('a%d' % i
                                     * 32)[:64],
        })

    return {
        'schemaVersion': 2,
        'mediaType':
            constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
        'config': {
            'mediaType':
                constants.MEDIA_TYPE_DOCKER_CONFIG,
            'size': 1234,
            'digest': 'sha256:' + 'cf' * 32,
        },
        'layers': layers,
    }


def _make_config(layer_count=2, arch='amd64',
                 os_name='linux'):
    """Build an OCI image config blob."""
    diff_ids = [
        'sha256:%s' % ('d%d' % i * 32)[:64]
        for i in range(layer_count)
    ]

    history = []
    for i in range(layer_count):
        history.append({
            'created': '2025-01-15T10:0%d:00Z' % i,
            'created_by':
                '/bin/sh -c echo layer%d' % i,
        })
    # Add an empty_layer history entry
    history.append({
        'created': '2025-01-15T10:05:00Z',
        'created_by':
            '/bin/sh -c #(nop) ENV FOO=bar',
        'empty_layer': True,
    })

    return {
        'architecture': arch,
        'os': os_name,
        'created': '2025-01-15T10:00:00Z',
        'rootfs': {
            'type': 'layers',
            'diff_ids': diff_ids,
        },
        'history': history,
        'config': {
            'Env': [
                'PATH=/usr/local/sbin:/usr/local/bin'
                ':/usr/sbin:/usr/bin:/sbin:/bin',
                'FOO=bar',
            ],
            'Cmd': ['/bin/sh'],
            'Entrypoint': None,
            'Labels': {
                'maintainer': 'test@example.com',
            },
            'WorkingDir': '/app',
            'ExposedPorts': {'8080/tcp': {}},
            'Volumes': {'/data': {}},
        },
    }


class MockInput(ImageInput):
    """Mock ImageInput for testing _build_info."""

    def __init__(self, image='myimage', tag='v1',
                 manifest=None, config=None):
        self._image = image
        self._tag = tag
        self._manifest = manifest
        self._config = config

    @property
    def image(self):
        return self._image

    @property
    def tag(self):
        return self._tag

    def fetch(self, fetch_callback=None,
              ordered=True):
        pass

    def get_manifest(self):
        return self._manifest

    def get_config(self):
        return self._config


# --- Tests for _build_info ---

class TestBuildInfo(unittest.TestCase):
    """Tests for the _build_info helper."""

    def test_full_info_with_manifest_and_config(self):
        """Test info with both manifest and config."""
        manifest = _make_manifest(layer_count=2)
        config = _make_config(layer_count=2)
        inp = MockInput(
            manifest=manifest, config=config)

        info = _build_info(inp)

        self.assertEqual(info['image'], 'myimage')
        self.assertEqual(info['tag'], 'v1')
        self.assertEqual(info['schema_version'], 2)
        self.assertEqual(
            info['media_type'],
            constants.MEDIA_TYPE_DOCKER_MANIFEST_V2)
        self.assertEqual(info['architecture'], 'amd64')
        self.assertEqual(info['os'], 'linux')
        self.assertEqual(info['layer_count'], 2)
        self.assertEqual(
            info['total_compressed_size'], 3000)
        self.assertEqual(len(info['layers']), 2)

        # Check that diff_ids were merged into layers
        for layer in info['layers']:
            self.assertIn('diff_id', layer)
            self.assertIn('digest', layer)
            self.assertIn('size', layer)
            self.assertIn('compression', layer)

        # Check history
        self.assertEqual(info['history_count'], 3)
        self.assertEqual(info['empty_layer_count'], 1)

        # Check container config
        self.assertEqual(len(info['env']), 2)
        self.assertEqual(info['cmd'], ['/bin/sh'])
        self.assertIsNone(info['entrypoint'])
        self.assertEqual(info['working_dir'], '/app')
        self.assertIn('8080/tcp', info['exposed_ports'])
        self.assertIn('/data', info['volumes'])
        self.assertEqual(
            info['labels']['maintainer'],
            'test@example.com')

    def test_config_only_no_manifest(self):
        """Test info with config but no manifest."""
        config = _make_config(layer_count=3)
        inp = MockInput(config=config)

        info = _build_info(inp)

        self.assertEqual(info['image'], 'myimage')
        self.assertNotIn('schema_version', info)
        self.assertNotIn('media_type', info)
        self.assertNotIn('total_compressed_size', info)

        # Layers built from diff_ids
        self.assertEqual(info['layer_count'], 3)
        self.assertEqual(len(info['layers']), 3)
        for layer in info['layers']:
            self.assertIn('diff_id', layer)
            self.assertNotIn('digest', layer)
            self.assertNotIn('size', layer)

    def test_no_manifest_no_config(self):
        """Test info with neither manifest nor config."""
        inp = MockInput()

        info = _build_info(inp)

        self.assertEqual(info['image'], 'myimage')
        self.assertEqual(info['tag'], 'v1')
        self.assertNotIn('layers', info)
        self.assertNotIn('architecture', info)

    def test_created_by_merged_into_layers(self):
        """Test that history created_by is merged."""
        manifest = _make_manifest(layer_count=2)
        config = _make_config(layer_count=2)
        inp = MockInput(
            manifest=manifest, config=config)

        info = _build_info(inp)

        self.assertEqual(
            info['layers'][0]['created_by'],
            '/bin/sh -c echo layer0')
        self.assertEqual(
            info['layers'][1]['created_by'],
            '/bin/sh -c echo layer1')

    def test_zstd_compression_detected(self):
        """Test that zstd compression is detected."""
        manifest = _make_manifest(
            layer_count=1, compression='zstd')
        inp = MockInput(manifest=manifest)

        info = _build_info(inp)

        self.assertEqual(
            info['layers'][0]['compression'],
            constants.COMPRESSION_ZSTD)

    def test_config_with_no_labels(self):
        """Test graceful handling of missing labels."""
        config = _make_config(layer_count=1)
        config['config']['Labels'] = None
        inp = MockInput(config=config)

        info = _build_info(inp)

        self.assertEqual(info['labels'], {})

    def test_config_with_no_env(self):
        """Test graceful handling of missing env."""
        config = _make_config(layer_count=1)
        config['config']['Env'] = None
        inp = MockInput(config=config)

        info = _build_info(inp)

        self.assertEqual(info['env'], [])


# --- Tests for _format_size ---

class TestFormatSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(_format_size(42), '42 B')

    def test_kilobytes(self):
        self.assertEqual(
            _format_size(2048), '2.0 KB')

    def test_megabytes(self):
        self.assertEqual(
            _format_size(5 * 1024 * 1024), '5.0 MB')

    def test_gigabytes(self):
        self.assertEqual(
            _format_size(2 * 1024 * 1024 * 1024),
            '2.0 GB')

    def test_none(self):
        self.assertEqual(_format_size(None), 'N/A')


# --- Tests for tarfile get_config ---

class TestTarfileGetConfig(unittest.TestCase):
    """Test get_config() for tarfile input."""

    def _make_test_tarball(self, config_data,
                           layer_count=1):
        """Create a docker-save format tarball."""
        tf = tempfile.NamedTemporaryFile(
            delete=False, suffix='.tar')
        self._temp_path = tf.name
        tf.close()

        config_json = json.dumps(
            config_data).encode('utf-8')
        config_filename = 'config.json'

        manifest = [{
            'Config': config_filename,
            'RepoTags': ['testimage:v1'],
            'Layers': [
                'layer%d/layer.tar' % i
                for i in range(layer_count)
            ],
        }]
        manifest_json = json.dumps(
            manifest).encode('utf-8')

        with tarfile.open(self._temp_path, 'w') as tar:
            # Add manifest.json
            ti = tarfile.TarInfo('manifest.json')
            ti.size = len(manifest_json)
            tar.addfile(
                ti, io.BytesIO(manifest_json))

            # Add config
            ti = tarfile.TarInfo(config_filename)
            ti.size = len(config_json)
            tar.addfile(
                ti, io.BytesIO(config_json))

            # Add layer tarballs
            for i in range(layer_count):
                layer_buf = io.BytesIO()
                with tarfile.open(
                        fileobj=layer_buf,
                        mode='w') as lt:
                    content = b'layer%d' % i
                    lti = tarfile.TarInfo(
                        'file%d.txt' % i)
                    lti.size = len(content)
                    lt.addfile(
                        lti, io.BytesIO(content))
                layer_data = layer_buf.getvalue()

                layer_path = 'layer%d/layer.tar' % i
                ti = tarfile.TarInfo(layer_path)
                ti.size = len(layer_data)
                tar.addfile(
                    ti, io.BytesIO(layer_data))

        return self._temp_path

    def tearDown(self):
        if hasattr(self, '_temp_path'):
            if os.path.exists(self._temp_path):
                os.unlink(self._temp_path)

    def test_get_config(self):
        """Test that get_config returns parsed config."""
        config_data = _make_config(layer_count=1)
        path = self._make_test_tarball(config_data)

        from occystrap.inputs import tarfile \
            as input_tarfile
        img = input_tarfile.Image(path)
        result = img.get_config()

        self.assertEqual(
            result['architecture'], 'amd64')
        self.assertEqual(result['os'], 'linux')
        self.assertIn('rootfs', result)
        self.assertIn('history', result)

    def test_get_manifest_returns_none(self):
        """Test that tarfile get_manifest returns None."""
        config_data = _make_config(layer_count=1)
        path = self._make_test_tarball(config_data)

        from occystrap.inputs import tarfile \
            as input_tarfile
        img = input_tarfile.Image(path)
        self.assertIsNone(img.get_manifest())


# --- Tests for docker get_config ---

class TestDockerGetConfig(unittest.TestCase):
    """Test get_config() for docker input."""

    @mock.patch(
        'occystrap.inputs.docker.requests_unixsocket')
    def test_get_config_transforms_inspect(
            self, mock_unix):
        """Test that docker get_config() transforms
        inspect data to OCI config format."""
        inspect_data = {
            'Id': 'sha256:' + 'ab' * 32,
            'Created': '2025-01-15T10:00:00Z',
            'Architecture': 'arm64',
            'Os': 'linux',
            'Config': {
                'Env': ['PATH=/usr/bin'],
                'Cmd': ['/bin/bash'],
                'Entrypoint': ['/start.sh'],
                'Labels': {'version': '1.0'},
                'WorkingDir': '/home',
                'ExposedPorts': {'443/tcp': {}},
                'Volumes': None,
            },
            'RootFS': {
                'Type': 'layers',
                'Layers': [
                    'sha256:' + 'dd' * 32,
                ],
            },
        }

        mock_session = mock.MagicMock()
        mock_unix.Session.return_value = mock_session
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = inspect_data
        mock_session.request.return_value = \
            mock_response

        from occystrap.inputs import docker \
            as input_docker
        img = input_docker.Image(
            'myimage', 'v1',
            socket_path='/var/run/docker.sock')
        result = img.get_config()

        # Verify OCI format
        self.assertEqual(
            result['architecture'], 'arm64')
        self.assertEqual(result['os'], 'linux')
        self.assertEqual(
            result['created'],
            '2025-01-15T10:00:00Z')
        self.assertEqual(
            result['rootfs']['type'], 'layers')
        self.assertEqual(
            len(result['rootfs']['diff_ids']), 1)
        self.assertEqual(
            result['config']['Cmd'], ['/bin/bash'])
        self.assertEqual(
            result['config']['Entrypoint'],
            ['/start.sh'])
        self.assertEqual(
            result['config']['Labels'],
            {'version': '1.0'})
        self.assertEqual(result['history'], [])


# --- Tests for CLI command ---

class TestInfoCommand(unittest.TestCase):
    """Test the info CLI command."""

    def test_info_json_output(self):
        """Test info command with JSON output."""
        manifest = _make_manifest(layer_count=1)
        config = _make_config(layer_count=1)

        with mock.patch(
                'occystrap.main.PipelineBuilder') \
                as mock_builder:
            mock_input = MockInput(
                image='test/img', tag='latest',
                manifest=manifest, config=config)
            mock_builder.return_value.build_input \
                .return_value = mock_input

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['-O', 'json', 'info',
                 'registry://docker.io/test/img:latest'
                 ])

        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data['image'], 'test/img')
        self.assertEqual(data['tag'], 'latest')
        self.assertEqual(data['architecture'], 'amd64')
        self.assertEqual(data['layer_count'], 1)

    def test_info_text_output(self):
        """Test info command with text output."""
        manifest = _make_manifest(layer_count=1)
        config = _make_config(layer_count=1)

        with mock.patch(
                'occystrap.main.PipelineBuilder') \
                as mock_builder:
            mock_input = MockInput(
                image='busybox', tag='latest',
                manifest=manifest, config=config)
            mock_builder.return_value.build_input \
                .return_value = mock_input

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['info',
                 'registry://docker.io/library/busybox:latest'
                 ])

        self.assertEqual(result.exit_code, 0)
        self.assertIn('busybox:latest', result.output)
        self.assertIn('amd64', result.output)
        self.assertIn('Layers: 1', result.output)

    def test_info_invalid_uri(self):
        """Test info with an invalid URI."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ['info', 'invalid://bad'])

        self.assertNotEqual(result.exit_code, 0)

    def test_info_tar_source(self):
        """Test info from a tarball source."""
        config_data = _make_config(layer_count=1)
        config_json = json.dumps(
            config_data).encode('utf-8')

        manifest = [{
            'Config': 'config.json',
            'RepoTags': ['testimg:v1'],
            'Layers': ['layer0/layer.tar'],
        }]
        manifest_json = json.dumps(
            manifest).encode('utf-8')

        tf = tempfile.NamedTemporaryFile(
            delete=False, suffix='.tar')
        tf_path = tf.name
        tf.close()

        try:
            with tarfile.open(tf_path, 'w') as tar:
                ti = tarfile.TarInfo('manifest.json')
                ti.size = len(manifest_json)
                tar.addfile(
                    ti, io.BytesIO(manifest_json))

                ti = tarfile.TarInfo('config.json')
                ti.size = len(config_json)
                tar.addfile(
                    ti, io.BytesIO(config_json))

                # Add a minimal layer
                layer_buf = io.BytesIO()
                with tarfile.open(
                        fileobj=layer_buf,
                        mode='w') as lt:
                    content = b'hello'
                    lti = tarfile.TarInfo('f.txt')
                    lti.size = len(content)
                    lt.addfile(
                        lti, io.BytesIO(content))
                layer_data = layer_buf.getvalue()
                ti = tarfile.TarInfo(
                    'layer0/layer.tar')
                ti.size = len(layer_data)
                tar.addfile(
                    ti, io.BytesIO(layer_data))

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['-O', 'json', 'info',
                 'tar://%s' % tf_path])

            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.output)
            self.assertEqual(
                data['image'], 'testimg')
            self.assertEqual(data['tag'], 'v1')
            self.assertEqual(
                data['architecture'], 'amd64')
        finally:
            os.unlink(tf_path)


if __name__ == '__main__':
    unittest.main()
