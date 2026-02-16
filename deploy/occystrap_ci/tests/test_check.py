"""Functional tests for the check command.

These tests require a local Docker registry running at
localhost:5000 with test images (busybox, ubuntu). The CI
workflow sets this up.
"""

import json
import logging
import os
import tempfile
import traceback
import testtools

from click.testing import CliRunner

from occystrap.main import cli


logging.basicConfig(
    level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class CheckRegistryTestCase(testtools.TestCase):
    """Test check against images in a local registry."""

    def test_check_fast_busybox_passes(self):
        """Fast check on a known-good busybox image
        should report no errors."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'json',
            'check', '--fast',
            'registry://localhost:5000/library/busybox'
            ':latest',
        ])

        self.assertEqual(
            0, result.exit_code,
            'check --fast failed: %s' % result.output)

        output = json.loads(result.output)
        self.assertEqual('fast', output['mode'])
        self.assertEqual(0, output['errors'])

        # Should have info-level results for passing
        # checks
        check_ids = [
            r['check'] for r in output['results']]
        self.assertIn('schema-version', check_ids)
        self.assertIn('rootfs-type', check_ids)
        self.assertIn('layer-count', check_ids)

    def test_check_full_busybox_passes(self):
        """Full check on a known-good busybox image
        should report no errors."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'json',
            'check',
            'registry://localhost:5000/library/busybox'
            ':latest',
        ])

        self.assertEqual(
            0, result.exit_code,
            'check failed: %s' % result.output)

        output = json.loads(result.output)
        self.assertEqual('full', output['mode'])
        self.assertEqual(0, output['errors'])

        # Full mode should verify config and diff_ids
        check_ids = [
            r['check'] for r in output['results']]
        self.assertIn('config-digest', check_ids)
        self.assertIn('config-size', check_ids)
        self.assertIn('diff-id', check_ids)

    def test_check_full_ubuntu_passes(self):
        """Full check on ubuntu (multi-layer) should
        report no errors."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'json',
            'check',
            'registry://localhost:5000/library/ubuntu'
            ':latest',
        ])

        self.assertEqual(
            0, result.exit_code,
            'check failed: %s' % result.output)

        output = json.loads(result.output)
        self.assertEqual(0, output['errors'])

    def test_check_text_output(self):
        """Check text output includes summary line."""
        runner = CliRunner()
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'text',
            'check', '--fast',
            'registry://localhost:5000/library/busybox'
            ':latest',
        ])

        self.assertEqual(
            0, result.exit_code,
            'check failed: %s' % result.output)
        self.assertIn(
            'Check complete', result.output)
        self.assertIn('0 error(s)', result.output)


class CheckTarfileTestCase(testtools.TestCase):
    """Test check against tarball sources.

    Creates tarballs via process, then runs check.
    """

    def test_check_tarball_passes(self):
        """Process an image to tar, then check the tar
        passes full validation."""
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

            # Full check on the tarball
            result = runner.invoke(cli, [
                '-O', 'json',
                'check',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(
                0, result.exit_code,
                'check failed: %s' % result.output)

            output = json.loads(result.output)
            self.assertEqual(0, output['errors'])

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_check_tarball_fast_passes(self):
        """Fast check on a tarball passes (config-only
        checks)."""
        runner = CliRunner()

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

            result = runner.invoke(cli, [
                '-O', 'json',
                'check', '--fast',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(
                0, result.exit_code,
                'check --fast failed: %s'
                % result.output)

            output = json.loads(result.output)
            self.assertEqual(0, output['errors'])

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)


class CheckAfterProcessTestCase(testtools.TestCase):
    """Test that check validates images produced by
    process with various filters.

    This is the core CI integration use case: run
    process with filters, then check the output.
    """

    def test_check_after_normalize_timestamps(self):
        """Normalize-timestamps changes layer content
        and updates config diff_ids to match. Full
        check should pass with zero errors."""
        runner = CliRunner()

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
                '-f', 'normalize-timestamps',
            ])
            self.assertEqual(
                0, result.exit_code,
                'process failed: %s' % result.output)

            # Full check should pass -- filter updates
            # config diff_ids to match modified layers
            result = runner.invoke(cli, [
                '-O', 'json',
                'check',
                'tar://%s' % tar_path,
            ])

            self.assertEqual(
                0, result.exit_code,
                'check failed: %s' % result.output)

            output = json.loads(result.output)
            self.assertEqual(
                0, output['errors'],
                'Unexpected errors: %s'
                % output['results'])

            # Fast mode should also pass
            result = runner.invoke(cli, [
                '-O', 'json',
                'check', '--fast',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(0, result.exit_code)
            fast_output = json.loads(result.output)
            self.assertEqual(0, fast_output['errors'])

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_check_after_exclude_filter(self):
        """Exclude filter changes layer content and
        updates config diff_ids. Full check should
        pass with zero errors."""
        runner = CliRunner()

        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            result = runner.invoke(cli, [
                '--insecure',
                'process',
                'registry://localhost:5000/'
                'library/ubuntu:latest',
                'tar://%s' % tar_path,
                '-f', 'exclude:pattern=**/__pycache__/**',
            ])
            self.assertEqual(
                0, result.exit_code,
                'process failed: %s' % result.output)

            result = runner.invoke(cli, [
                '-O', 'json',
                'check',
                'tar://%s' % tar_path,
            ])

            self.assertEqual(
                0, result.exit_code,
                'check failed: %s' % result.output)

            output = json.loads(result.output)
            self.assertEqual(
                0, output['errors'],
                'Unexpected errors: %s'
                % output['results'])

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_check_after_combined_filters(self):
        """Combined filters change layer content and
        update config diff_ids. Full check should
        pass with zero errors."""
        runner = CliRunner()

        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            result = runner.invoke(cli, [
                '--insecure',
                'process',
                'registry://localhost:5000/'
                'library/ubuntu:latest',
                'tar://%s' % tar_path,
                '-f', 'normalize-timestamps',
                '-f', 'exclude:pattern=**/__pycache__/**',
            ])
            self.assertEqual(
                0, result.exit_code,
                'process failed: %s' % result.output)

            result = runner.invoke(cli, [
                '-O', 'json',
                'check',
                'tar://%s' % tar_path,
            ])

            if result.exit_code != 0:
                exc_info = ''
                if result.exception:
                    exc_info = (
                        '\nException: %s\n%s'
                        % (result.exception,
                           ''.join(
                               traceback
                               .format_exception(
                                   type(
                                       result
                                       .exception
                                   ),
                                   result.exception,
                                   result.exception
                                   .__traceback__
                               ))))
                self.fail(
                    'check failed '
                    '(exit_code=%d):\n'
                    'output: %s%s'
                    % (result.exit_code,
                       result.output,
                       exc_info))

            output = json.loads(result.output)
            self.assertEqual(
                0, output['errors'],
                'Unexpected errors: %s'
                % output['results'])

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_check_after_registry_roundtrip(self):
        """Push to local registry, then check the pushed
        image passes full validation."""
        runner = CliRunner()

        # Push busybox to a test destination
        result = runner.invoke(cli, [
            '--insecure',
            'process',
            'registry://localhost:5000/'
            'library/busybox:latest',
            'registry://localhost:5000/'
            'occystrap_check_roundtrip:latest',
        ])
        self.assertEqual(
            0, result.exit_code,
            'process failed: %s' % result.output)

        # Check the pushed image
        result = runner.invoke(cli, [
            '--insecure',
            '-O', 'json',
            'check',
            'registry://localhost:5000/'
            'occystrap_check_roundtrip:latest',
        ])
        self.assertEqual(
            0, result.exit_code,
            'check failed: %s' % result.output)

        output = json.loads(result.output)
        self.assertEqual(0, output['errors'])

    def test_check_verifies_diff_ids_match(self):
        """Full check verifies that every layer's
        diff_id matches the config."""
        runner = CliRunner()

        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            result = runner.invoke(cli, [
                '--insecure',
                'process',
                'registry://localhost:5000/'
                'library/ubuntu:latest',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(0, result.exit_code)

            result = runner.invoke(cli, [
                '-O', 'json',
                'check',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(0, result.exit_code)

            output = json.loads(result.output)

            # Count diff-id info results -- should be
            # one per layer
            diff_id_results = [
                r for r in output['results']
                if r['check'] == 'diff-id'
                and r['severity'] == 'info']
            self.assertGreater(
                len(diff_id_results), 0,
                'No diff-id verification results found')

            # All should be info (verified), not error
            diff_id_errors = [
                r for r in output['results']
                if r['check'] == 'diff-id'
                and r['severity'] == 'error']
            self.assertEqual(
                0, len(diff_id_errors),
                'diff-id errors: %s' % diff_id_errors)

        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)
