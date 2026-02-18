"""Functional tests for the info command.

These tests require a local Docker registry running at
localhost:5000 with test images (busybox, ubuntu). The CI
workflow sets this up.
"""

import json
import logging
import os
import tempfile
import testtools

from click.testing import CliRunner

from occystrap.inputs import registry as input_registry
from occystrap.main import cli


logging.basicConfig(
    level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class InfoRegistryTestCase(testtools.TestCase):
    """Test info against images in a local registry."""

    def test_info_busybox_has_expected_fields(self):
        """Info on busybox returns architecture, OS,
        layer count, and diff_ids."""
        src = input_registry.Image(
            'localhost:5000', 'library/busybox',
            'latest', 'linux', 'amd64', '',
            secure=False)

        manifest = src.get_manifest()
        config = src.get_config()

        self.assertIsNotNone(manifest)
        self.assertIsNotNone(config)

        # Manifest structure
        self.assertEqual(2, manifest.get('schemaVersion'))
        self.assertIn('layers', manifest)
        self.assertIn('config', manifest)
        self.assertGreater(
            len(manifest['layers']), 0)

        # Config structure
        self.assertEqual('amd64', config['architecture'])
        self.assertEqual('linux', config['os'])
        self.assertIn('rootfs', config)
        self.assertEqual(
            'layers', config['rootfs']['type'])
        self.assertEqual(
            len(manifest['layers']),
            len(config['rootfs']['diff_ids']))

    def test_info_ubuntu_has_multiple_fields(self):
        """Info on ubuntu shows expected metadata."""
        src = input_registry.Image(
            'localhost:5000', 'library/ubuntu',
            'latest', 'linux', 'amd64', '',
            secure=False)

        manifest = src.get_manifest()
        config = src.get_config()

        self.assertIsNotNone(manifest)
        self.assertIsNotNone(config)
        self.assertEqual('amd64', config['architecture'])
        self.assertEqual('linux', config['os'])

    def test_info_cli_json_registry(self):
        """The info CLI command outputs valid JSON for
        a registry source."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'json',
            'info',
            'registry://localhost:5000/library/busybox'
            ':latest',
        ])

        self.assertEqual(0, result.exit_code,
                         'info failed: %s' % result.output)

        info = json.loads(result.stdout)
        self.assertEqual(
            'library/busybox', info['image'])
        self.assertEqual('latest', info['tag'])
        self.assertIn('layer_count', info)
        self.assertGreater(info['layer_count'], 0)
        self.assertIn('architecture', info)
        self.assertEqual('amd64', info['architecture'])
        self.assertIn('layers', info)
        self.assertEqual(
            info['layer_count'], len(info['layers']))

        # Each layer should have a digest and diff_id
        for layer in info['layers']:
            self.assertIn('digest', layer)
            self.assertIn('diff_id', layer)
            self.assertTrue(
                layer['digest'].startswith('sha256:'))
            self.assertTrue(
                layer['diff_id'].startswith('sha256:'))

    def test_info_cli_text_registry(self):
        """The info CLI command outputs text for a
        registry source."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'text',
            'info',
            'registry://localhost:5000/library/busybox'
            ':latest',
        ])

        self.assertEqual(0, result.exit_code,
                         'info failed: %s' % result.output)
        self.assertIn('Image:', result.output)
        self.assertIn('busybox', result.output)
        self.assertIn('Layers:', result.output)
        self.assertIn('amd64', result.output)


class InfoTarfileTestCase(testtools.TestCase):
    """Test info against tarball sources.

    Creates a tarball via process, then runs info on it.
    """

    def test_info_cli_json_tarball(self):
        """Process a registry image to tar, then run info
        on the tarball."""
        runner = CliRunner()

        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            # Process registry -> tar
            result = runner.invoke(cli, [
                '--insecure',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(
                0, result.exit_code,
                'process failed: %s' % result.output)

            # Run info on the tarball
            result = runner.invoke(cli, [
                '-O', 'json',
                'info',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(
                0, result.exit_code,
                'info failed: %s' % result.output)

            info = json.loads(result.stdout)
            self.assertIn('layer_count', info)
            self.assertGreater(info['layer_count'], 0)
            self.assertEqual(
                'amd64', info['architecture'])
            self.assertEqual('linux', info['os'])

            # Tar sources have diff_ids but no
            # compressed digests
            for layer in info['layers']:
                self.assertIn('diff_id', layer)
                self.assertTrue(
                    layer['diff_id'].startswith(
                        'sha256:'))

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_info_tarball_layer_count_matches_registry(
            self):
        """Tarball info layer count matches registry
        info layer count for the same image."""
        runner = CliRunner()

        # Get registry info
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'json',
            'info',
            'registry://localhost:5000/library/busybox'
            ':latest',
        ])
        self.assertEqual(0, result.exit_code)
        registry_info = json.loads(result.stdout)

        # Create tarball
        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            result = runner.invoke(cli, [
                '--insecure',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(0, result.exit_code)

            # Get tarball info
            result = runner.invoke(cli, [
                '-O', 'json',
                'info',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(0, result.exit_code)
            tar_info = json.loads(result.stdout)

            # Layer counts must match
            self.assertEqual(
                registry_info['layer_count'],
                tar_info['layer_count'])

            # Diff_ids must match (same uncompressed
            # content)
            self.assertEqual(
                registry_info['diff_ids'],
                tar_info['diff_ids'])

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)
