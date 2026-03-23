"""Functional tests for quay:// bulk image discovery.

The info command tests hit the real quay.io API to verify
end-to-end integration. They use the 'projectquay' public org
(maintained by the Quay team) and only fetch metadata — no
layer blobs are downloaded, keeping the tests fast.

The process command tests mock _resolve_quay_images to return
tuples pointing at the local CI registry (localhost:5000),
avoiding real layer downloads from quay.io while still testing
the multi-image pipeline end-to-end.
"""

import json
import logging
import os
import tempfile
import testtools
from unittest import mock

from click.testing import CliRunner

from occystrap.main import cli


logging.basicConfig(
    level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class QuayInfoRealAPITestCase(testtools.TestCase):
    """Test info with quay:// URIs against the real quay.io API.

    These tests require network access to quay.io. They use the
    'projectquay' public organization which is maintained by the
    Quay team and is expected to remain stable.
    """

    def test_info_quay_text(self):
        """info with a real quay:// URI returns image metadata."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            'info',
            'quay://projectquay/quay:latest',
        ])

        self.assertEqual(
            0, result.exit_code,
            'info failed: %s' % result.output)

        # Should contain the image name and basic metadata
        self.assertIn('projectquay/quay', result.output)
        self.assertIn('Layers:', result.output)

    def test_info_quay_json(self):
        """info -O json with a real quay:// URI returns valid JSON."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '-O', 'json',
            'info',
            'quay://projectquay/quay:latest',
        ])

        self.assertEqual(
            0, result.exit_code,
            'info failed: %s' % result.output)

        # Extract JSON array from output. Try stdout first
        # (clean JSON when Click supports mix_stderr), then
        # fall back to finding the JSON array in mixed output
        # where progress lines and ANSI codes may be present.
        output = getattr(result, 'stdout', None) or result.output
        if not output.lstrip().startswith('['):
            json_start = output.index('\n[') + 1
            output = output[json_start:]
        data = json.loads(output)

        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

        first = data[0]
        self.assertIn('image', first)
        self.assertIn('tag', first)
        self.assertEqual('latest', first['tag'])
        self.assertIn('layer_count', first)
        self.assertGreater(first['layer_count'], 0)

    def test_info_quay_no_matches(self):
        """info with a quay:// URI matching no tags prints a message."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            'info',
            'quay://projectquay/quay:this-tag-does-not-exist-999',
        ])

        self.assertEqual(0, result.exit_code)
        self.assertIn('No images found', result.output)

    def test_info_quay_since_filters_old(self):
        """info with since far in the future returns no results."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            'info',
            'quay://projectquay/quay:latest?since=2099-01-01',
        ])

        self.assertEqual(0, result.exit_code)
        self.assertIn('No images found', result.output)


class QuayProcessMockedTestCase(testtools.TestCase):
    """Test process with quay:// URIs using mocked resolution.

    These tests mock _resolve_quay_images to return tuples
    pointing at the local CI registry (localhost:5000), so
    actual image pulls use the fast local registry.
    """

    @mock.patch('occystrap.main._resolve_quay_images')
    def test_process_quay_to_dir(self, mock_resolve):
        """process quay:// to dir:// with unique_names downloads images."""
        mock_resolve.return_value = [
            ('localhost:5000', 'library/busybox', 'latest'),
            ('localhost:5000', 'library/hello-world', 'latest'),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = CliRunner()
            result = runner.invoke(cli, [
                '--insecure',
                'process',
                'quay://fakeorg/*:latest',
                'dir://%s?unique_names=true' % tmpdir,
            ])

            self.assertEqual(
                0, result.exit_code,
                'process failed: %s' % result.output)

            # Verify output directory has files from both images
            files = os.listdir(tmpdir)
            self.assertGreater(len(files), 0,
                               'Output directory is empty')

            # With unique_names, we expect a catalog.json
            self.assertIn('catalog.json', files)

            # Catalog format is {image_name: {tag: manifest_file}}
            with open(os.path.join(tmpdir, 'catalog.json')) as f:
                catalog = json.load(f)
            self.assertIn('library/busybox', catalog)
            self.assertIn('library/hello-world', catalog)

    @mock.patch('occystrap.main._resolve_quay_images')
    def test_process_quay_tar_rejected(self, mock_resolve):
        """process quay:// to tar:// is rejected with an error."""
        mock_resolve.return_value = [
            ('localhost:5000', 'library/busybox', 'latest'),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            'process',
            'quay://fakeorg/*:latest',
            'tar:///tmp/test_quay_output.tar',
        ])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn('tar://', result.output)
