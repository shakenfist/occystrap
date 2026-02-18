"""Tests for the check command and check module."""

import hashlib
import io
import json

import tarfile
import tempfile
import os
import unittest
from unittest import mock

from click.testing import CliRunner

from occystrap import check
from occystrap import constants
from occystrap.inputs.base import ImageInput
from occystrap.main import cli


# --- Helpers ---

def _make_layer_tar(files=None):
    """Create a tar archive containing the given files.

    Args:
        files: Dict of filename -> content bytes.
            Defaults to a single file.

    Returns:
        Bytes of the tar archive.
    """
    if files is None:
        files = {'file.txt': b'hello'}

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tf:
        for name, content in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def _sha256(data):
    """Return sha256:hex digest of data."""
    h = hashlib.sha256()
    h.update(data)
    return 'sha256:%s' % h.hexdigest()


def _make_manifest(layer_digests, config_digest,
                   config_size,
                   compression='gzip'):
    """Build a distribution manifest."""
    if compression == 'gzip':
        media = constants.MEDIA_TYPE_DOCKER_LAYER_GZIP
    elif compression == 'zstd':
        media = constants.MEDIA_TYPE_DOCKER_LAYER_ZSTD
    else:
        media = constants.MEDIA_TYPE_OCI_LAYER_GZIP

    layers = []
    for digest in layer_digests:
        layers.append({
            'mediaType': media,
            'size': 1000,
            'digest': digest,
        })

    return {
        'schemaVersion': 2,
        'mediaType':
            constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
        'config': {
            'mediaType':
                constants.MEDIA_TYPE_DOCKER_CONFIG,
            'size': config_size,
            'digest': config_digest,
        },
        'layers': layers,
    }


def _make_config(diff_ids, history_count=None,
                 empty_layers=0,
                 args_escaped=False):
    """Build an OCI image config."""
    if history_count is None:
        history_count = len(diff_ids)

    history = []
    for i in range(history_count):
        history.append({
            'created': '2025-01-15T10:0%d:00Z'
            % (i % 10),
            'created_by':
                '/bin/sh -c echo layer%d' % i,
        })
    for i in range(empty_layers):
        history.append({
            'created': '2025-01-15T10:05:00Z',
            'created_by':
                '/bin/sh -c #(nop) ENV FOO=bar',
            'empty_layer': True,
        })

    config = {
        'architecture': 'amd64',
        'os': 'linux',
        'created': '2025-01-15T10:00:00Z',
        'rootfs': {
            'type': 'layers',
            'diff_ids': diff_ids,
        },
        'history': history,
        'config': {
            'Env': ['PATH=/usr/bin'],
            'Cmd': ['/bin/sh'],
        },
    }

    if args_escaped:
        config['config']['ArgsEscaped'] = True

    return config


class MockCheckInput(ImageInput):
    """Mock ImageInput for testing check."""

    def __init__(self, image='testimg', tag='v1',
                 manifest=None, config=None,
                 layers=None, config_bytes=None):
        self._image = image
        self._tag = tag
        self._manifest = manifest
        self._config = config
        self._layers = layers or []
        self._config_bytes = config_bytes

    @property
    def image(self):
        return self._image

    @property
    def tag(self):
        return self._tag

    def fetch(self, fetch_callback=None,
              ordered=True):
        # Yield config element
        if self._config_bytes is not None:
            yield constants.ImageElement(
                constants.CONFIG_FILE,
                'config.json',
                io.BytesIO(self._config_bytes))

        # Yield layer elements
        for layer_data in self._layers:
            yield constants.ImageElement(
                constants.IMAGE_LAYER,
                'layer',
                io.BytesIO(layer_data))

    def get_manifest(self):
        return self._manifest

    def get_config(self):
        return self._config


# --- Tests for CheckResults ---

class TestCheckResults(unittest.TestCase):

    def test_empty_results(self):
        r = check.CheckResults()
        self.assertFalse(r.has_errors)
        self.assertEqual(r.error_count, 0)
        self.assertEqual(r.warning_count, 0)

    def test_error_recorded(self):
        r = check.CheckResults()
        r.error('test', 'something broke')
        self.assertTrue(r.has_errors)
        self.assertEqual(r.error_count, 1)
        self.assertEqual(r.warning_count, 0)
        self.assertEqual(
            r.results[0]['severity'],
            check.CHECK_ERROR)

    def test_warning_recorded(self):
        r = check.CheckResults()
        r.warning('test', 'heads up')
        self.assertFalse(r.has_errors)
        self.assertEqual(r.warning_count, 1)

    def test_info_recorded(self):
        r = check.CheckResults()
        r.info('test', 'all good')
        self.assertFalse(r.has_errors)
        self.assertEqual(r.error_count, 0)
        self.assertEqual(r.warning_count, 0)
        self.assertEqual(len(r.results), 1)


# --- Tests for check_metadata ---

class TestCheckMetadata(unittest.TestCase):

    def test_valid_image(self):
        """Test that a valid image passes all checks."""
        layer_tar = _make_layer_tar()
        diff_id = _sha256(layer_tar)

        config = _make_config(
            [diff_id], empty_layers=1)
        config_bytes = json.dumps(
            config).encode('utf-8')
        config_digest = _sha256(config_bytes)

        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            config_digest,
            len(config_bytes))

        results = check.CheckResults()
        check.check_metadata(
            manifest, config, results)

        self.assertFalse(results.has_errors)

    def test_schema_version_wrong(self):
        """Test error on wrong schemaVersion."""
        manifest = {'schemaVersion': 1}
        results = check.CheckResults()
        check.check_metadata(manifest, None, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'schema-version']
        self.assertEqual(len(errors), 1)

    def test_rootfs_type_wrong(self):
        """Test error on wrong rootfs.type."""
        config = {
            'rootfs': {'type': 'wrong', 'diff_ids': []},
            'history': [],
        }
        results = check.CheckResults()
        check.check_metadata(None, config, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'rootfs-type']
        self.assertEqual(len(errors), 1)

    def test_layer_count_mismatch(self):
        """Test error when layer count != diff_id count."""
        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32,
             'sha256:' + 'cd' * 32],
            'sha256:' + 'cf' * 32,
            100)

        config = _make_config(
            ['sha256:' + 'dd' * 32])

        results = check.CheckResults()
        check.check_metadata(
            manifest, config, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'layer-count']
        self.assertEqual(len(errors), 1)
        self.assertIn('2 layers', errors[0]['message'])
        self.assertIn('1 diff_ids',
                      errors[0]['message'])

    def test_history_count_mismatch(self):
        """Test error when history doesn't match."""
        config = _make_config(
            ['sha256:' + 'dd' * 32],
            history_count=3)

        results = check.CheckResults()
        check.check_metadata(None, config, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'history-count']
        self.assertEqual(len(errors), 1)

    def test_no_config_descriptor(self):
        """Test error when manifest has no config."""
        manifest = {
            'schemaVersion': 2,
            'mediaType':
                constants.MEDIA_TYPE_DOCKER_MANIFEST_V2,
            'config': {},
            'layers': [],
        }
        results = check.CheckResults()
        check.check_metadata(manifest, None, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'config-descriptor']
        self.assertEqual(len(errors), 1)

    def test_zstd_warning(self):
        """Test zstd compatibility warning."""
        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            'sha256:' + 'cf' * 32,
            100,
            compression='zstd')

        results = check.CheckResults()
        check.check_metadata(manifest, None, results)

        warnings = [
            r for r in results.results
            if r['check'] == 'zstd-compat']
        self.assertEqual(len(warnings), 1)

    def test_args_escaped_warning(self):
        """Test ArgsEscaped deprecation warning."""
        config = _make_config(
            ['sha256:' + 'dd' * 32],
            args_escaped=True)

        results = check.CheckResults()
        check.check_metadata(None, config, results)

        warnings = [
            r for r in results.results
            if r['check'] == 'args-escaped']
        self.assertEqual(len(warnings), 1)

    def test_no_manifest_no_config(self):
        """Test warning when neither is available."""
        results = check.CheckResults()
        check.check_metadata(None, None, results)

        self.assertFalse(results.has_errors)
        self.assertEqual(results.warning_count, 1)

    def test_large_layer_warning(self):
        """Test large layer warning."""
        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            'sha256:' + 'cf' * 32,
            100)
        # Override size to > 1GB
        manifest['layers'][0]['size'] = (
            2 * 1024 * 1024 * 1024)

        results = check.CheckResults()
        check.check_metadata(manifest, None, results)

        warnings = [
            r for r in results.results
            if r['check'] == 'large-layer']
        self.assertEqual(len(warnings), 1)


# --- Tests for check_layers ---

class TestCheckLayers(unittest.TestCase):

    def test_valid_layers(self):
        """Test that valid layers pass all checks."""
        layer_tar = _make_layer_tar()
        diff_id = _sha256(layer_tar)

        config = _make_config([diff_id])
        config_bytes = json.dumps(
            config).encode('utf-8')
        config_digest = _sha256(config_bytes)

        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            config_digest,
            len(config_bytes))

        inp = MockCheckInput(
            manifest=manifest,
            config=config,
            layers=[layer_tar],
            config_bytes=config_bytes)

        results = check.CheckResults()
        check.check_layers(
            inp, manifest, config, results)

        self.assertFalse(results.has_errors)
        # Should have config-digest, config-size,
        # diff-id info messages
        infos = [
            r for r in results.results
            if r['severity'] == check.CHECK_INFO]
        self.assertTrue(len(infos) >= 3)

    def test_diff_id_mismatch(self):
        """Test error when diff_id doesn't match."""
        layer_tar = _make_layer_tar()
        wrong_diff_id = 'sha256:' + 'ff' * 32

        config = _make_config([wrong_diff_id])

        inp = MockCheckInput(
            config=config,
            layers=[layer_tar])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'diff-id']
        self.assertEqual(len(errors), 1)
        self.assertIn('mismatch',
                      errors[0]['message'])

    def test_config_digest_mismatch(self):
        """Test error when config digest is wrong."""
        config = _make_config(
            ['sha256:' + 'dd' * 32])
        config_bytes = json.dumps(
            config).encode('utf-8')

        # Manifest claims a different digest
        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            'sha256:' + 'ff' * 32,
            len(config_bytes))

        inp = MockCheckInput(
            manifest=manifest,
            config=config,
            config_bytes=config_bytes)

        results = check.CheckResults()
        check.check_layers(
            inp, manifest, config, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'config-digest']
        self.assertEqual(len(errors), 1)

    def test_config_size_mismatch(self):
        """Test error when config size is wrong."""
        config = _make_config(
            ['sha256:' + 'dd' * 32])
        config_bytes = json.dumps(
            config).encode('utf-8')
        config_digest = _sha256(config_bytes)

        # Manifest claims wrong size
        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            config_digest,
            999999)

        inp = MockCheckInput(
            manifest=manifest,
            config=config,
            config_bytes=config_bytes)

        results = check.CheckResults()
        check.check_layers(
            inp, manifest, config, results)

        self.assertTrue(results.has_errors)
        errors = [
            r for r in results.results
            if r['check'] == 'config-size']
        self.assertEqual(len(errors), 1)

    def test_invalid_tar_layer(self):
        """Test error when layer isn't valid tar."""
        config = _make_config(
            ['sha256:' + 'dd' * 32])

        inp = MockCheckInput(
            config=config,
            layers=[b'not a tar file'])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        errors = [
            r for r in results.results
            if r['check'] == 'tar-format']
        self.assertEqual(len(errors), 1)

    def test_whiteout_empty_target(self):
        """Test error for empty whiteout target."""
        layer_tar = _make_layer_tar(
            {'.wh.': b''})

        config = _make_config(
            [_sha256(layer_tar)])

        inp = MockCheckInput(
            config=config,
            layers=[layer_tar])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        errors = [
            r for r in results.results
            if r['check'] == 'whiteout']
        self.assertEqual(len(errors), 1)

    def test_valid_whiteout(self):
        """Test that valid whiteouts pass."""
        layer_tar = _make_layer_tar(
            {'.wh.oldfile': b''})

        config = _make_config(
            [_sha256(layer_tar)])

        inp = MockCheckInput(
            config=config,
            layers=[layer_tar])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        # No whiteout errors
        errors = [
            r for r in results.results
            if r['check'] == 'whiteout'
            and r['severity'] == check.CHECK_ERROR]
        self.assertEqual(len(errors), 0)

    def test_whiteout_nonzero_size(self):
        """Test warning for non-empty whiteout file."""
        layer_tar = _make_layer_tar(
            {'.wh.oldfile': b'unexpected'})

        config = _make_config(
            [_sha256(layer_tar)])

        inp = MockCheckInput(
            config=config,
            layers=[layer_tar])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        warnings = [
            r for r in results.results
            if r['check'] == 'whiteout-size']
        self.assertEqual(len(warnings), 1)
        self.assertIn('non-zero',
                      warnings[0]['message'])

    def test_duplicate_files_across_layers(self):
        """Test duplicate file info report."""
        layer1 = _make_layer_tar(
            {'shared.txt': b'v1'})
        layer2 = _make_layer_tar(
            {'shared.txt': b'v2'})

        config = _make_config(
            [_sha256(layer1), _sha256(layer2)])

        inp = MockCheckInput(
            config=config,
            layers=[layer1, layer2])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        dupes = [
            r for r in results.results
            if r['check'] == 'duplicate-file']
        self.assertEqual(len(dupes), 1)
        self.assertEqual(
            dupes[0]['severity'], check.CHECK_INFO)
        self.assertIn('shared.txt',
                      dupes[0]['message'])

    def test_negative_timestamp(self):
        """Test error for negative mtime."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tf:
            ti = tarfile.TarInfo('badtime.txt')
            ti.size = 5
            ti.mtime = -1
            tf.addfile(ti, io.BytesIO(b'hello'))
        layer_tar = buf.getvalue()

        config = _make_config(
            [_sha256(layer_tar)])

        inp = MockCheckInput(
            config=config,
            layers=[layer_tar])

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        errors = [
            r for r in results.results
            if r['check'] == 'tar-timestamp']
        self.assertEqual(len(errors), 1)

    def test_multi_layer_verification(self):
        """Test diff_id verification for 3 layers."""
        layers = []
        diff_ids = []
        for i in range(3):
            layer = _make_layer_tar(
                {'file%d.txt' % i:
                 ('content%d' % i).encode()})
            layers.append(layer)
            diff_ids.append(_sha256(layer))

        config = _make_config(diff_ids)

        inp = MockCheckInput(
            config=config,
            layers=layers)

        results = check.CheckResults()
        check.check_layers(
            inp, None, config, results)

        self.assertFalse(results.has_errors)
        infos = [
            r for r in results.results
            if r['check'] == 'diff-id']
        self.assertEqual(len(infos), 3)


# --- Tests for CLI command ---

class TestCheckCommand(unittest.TestCase):

    def test_check_fast_json_output(self):
        """Test check --fast with JSON output."""
        layer_tar = _make_layer_tar()
        diff_id = _sha256(layer_tar)

        config = _make_config(
            [diff_id], empty_layers=1)
        config_bytes = json.dumps(
            config).encode('utf-8')
        config_digest = _sha256(config_bytes)

        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            config_digest,
            len(config_bytes))

        with mock.patch(
                'occystrap.main.PipelineBuilder') \
                as mock_builder:
            mock_input = MockCheckInput(
                manifest=manifest,
                config=config)
            mock_builder.return_value.build_input \
                .return_value = mock_input

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['-O', 'json', 'check', '--fast',
                 'registry://docker.io/test/img:v1'])

        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.stdout)
        self.assertEqual(data['mode'], 'fast')
        self.assertEqual(data['errors'], 0)
        self.assertIn('results', data)

    def test_check_fast_text_output(self):
        """Test check --fast with text output."""
        config = _make_config(
            ['sha256:' + 'dd' * 32],
            empty_layers=1)
        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            'sha256:' + 'cf' * 32,
            100)

        with mock.patch(
                'occystrap.main.PipelineBuilder') \
                as mock_builder:
            mock_input = MockCheckInput(
                manifest=manifest,
                config=config)
            mock_builder.return_value.build_input \
                .return_value = mock_input

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['check', '--fast',
                 'registry://docker.io/test/img:v1'])

        self.assertEqual(result.exit_code, 0)
        self.assertIn('Check complete', result.output)
        self.assertIn('fast', result.output)

    def test_check_error_exit_code(self):
        """Test that errors cause non-zero exit."""
        # Wrong schemaVersion triggers an error
        manifest = {'schemaVersion': 1, 'config': {}}

        with mock.patch(
                'occystrap.main.PipelineBuilder') \
                as mock_builder:
            mock_input = MockCheckInput(
                manifest=manifest)
            mock_builder.return_value.build_input \
                .return_value = mock_input

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['check', '--fast',
                 'registry://docker.io/test/img:v1'])

        self.assertNotEqual(result.exit_code, 0)

    def test_check_invalid_uri(self):
        """Test check with invalid URI."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ['check', 'invalid://bad'])

        self.assertNotEqual(result.exit_code, 0)

    def test_check_full_json_output(self):
        """Test full check (no --fast) with JSON."""
        layer_tar = _make_layer_tar()
        diff_id = _sha256(layer_tar)

        config = _make_config([diff_id])
        config_bytes = json.dumps(
            config).encode('utf-8')
        config_digest = _sha256(config_bytes)

        manifest = _make_manifest(
            ['sha256:' + 'ab' * 32],
            config_digest,
            len(config_bytes))

        with mock.patch(
                'occystrap.main.PipelineBuilder') \
                as mock_builder:
            mock_input = MockCheckInput(
                manifest=manifest,
                config=config,
                layers=[layer_tar],
                config_bytes=config_bytes)
            mock_builder.return_value.build_input \
                .return_value = mock_input

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['-O', 'json', 'check',
                 'registry://docker.io/test/img:v1'])

        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.stdout)
        self.assertEqual(data['mode'], 'full')
        self.assertEqual(data['errors'], 0)

    def test_check_tar_source(self):
        """Test check from a tarball source."""
        layer_tar = _make_layer_tar()
        diff_id = _sha256(layer_tar)

        config_data = _make_config([diff_id])
        config_json = json.dumps(
            config_data).encode('utf-8')

        tar_manifest = [{
            'Config': 'config.json',
            'RepoTags': ['testimg:v1'],
            'Layers': ['layer0/layer.tar'],
        }]
        manifest_json = json.dumps(
            tar_manifest).encode('utf-8')

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

                ti = tarfile.TarInfo(
                    'layer0/layer.tar')
                ti.size = len(layer_tar)
                tar.addfile(
                    ti, io.BytesIO(layer_tar))

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ['-O', 'json', 'check',
                 'tar://%s' % tf_path])

            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data['errors'], 0)
            self.assertEqual(data['mode'], 'full')
        finally:
            os.unlink(tf_path)


if __name__ == '__main__':
    unittest.main()
