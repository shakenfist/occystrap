"""Functional tests for the --verify flag.

These tests require a local Docker registry running at
localhost:5000 with test images (busybox, ubuntu). The CI
workflow sets this up.
"""

import logging
import os
import shutil
import tempfile
import testtools

from click.testing import CliRunner

from occystrap.main import cli


logging.basicConfig(
    level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class VerifyDirTestCase(testtools.TestCase):
    """Test --verify with dir:// output."""

    def test_verify_dir_passes(self):
        """Process busybox to dir:// with --verify,
        verify passes (exit code 0)."""
        runner = CliRunner(mix_stderr=False)
        outdir = tempfile.mkdtemp()

        try:
            result = runner.invoke(cli, [
                '--insecure',
                '--verify',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'dir://%s' % outdir,
            ])
            self.assertEqual(
                0, result.exit_code,
                'process --verify failed: %s'
                % (result.stderr or result.output))
            self.assertIn(
                'verified OK', result.stderr,
                'Summary should include verified OK')
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_verify_full_dir_passes(self):
        """Process busybox to dir:// with --verify-full,
        full verification passes."""
        runner = CliRunner(mix_stderr=False)
        outdir = tempfile.mkdtemp()

        try:
            result = runner.invoke(cli, [
                '--insecure',
                '--verify-full',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'dir://%s' % outdir,
            ])
            self.assertEqual(
                0, result.exit_code,
                'process --verify-full failed: %s'
                % (result.stderr or result.output))
            self.assertIn(
                'verified OK', result.stderr,
                'Summary should include verified OK')
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_no_verify_skips_verification(self):
        """Process with --no-verify does not run
        verification (no 'verified' in summary)."""
        runner = CliRunner(mix_stderr=False)
        outdir = tempfile.mkdtemp()

        try:
            result = runner.invoke(cli, [
                '--insecure',
                '--no-verify',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'dir://%s' % outdir,
            ])
            self.assertEqual(
                0, result.exit_code,
                'process --no-verify failed: %s'
                % (result.stderr or result.output))
            self.assertNotIn(
                'verified', result.stderr,
                'Summary should not mention verification'
                ' when --no-verify is used')
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_verify_dir_with_filter_passes(self):
        """Process busybox to dir:// with a filter and
        --verify, verify passes."""
        runner = CliRunner(mix_stderr=False)
        outdir = tempfile.mkdtemp()

        try:
            result = runner.invoke(cli, [
                '--insecure',
                '--verify',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'dir://%s' % outdir,
                '-f', 'normalize-timestamps',
            ])
            self.assertEqual(
                0, result.exit_code,
                'process --verify with filter failed: %s'
                % (result.stderr or result.output))
            self.assertIn(
                'verified OK', result.stderr,
                'Summary should include verified OK')
        finally:
            shutil.rmtree(outdir, ignore_errors=True)


class VerifyTarTestCase(testtools.TestCase):
    """Test --verify with tar:// output."""

    def test_verify_tar_passes(self):
        """Process busybox to tar:// with --verify,
        verify passes."""
        runner = CliRunner(mix_stderr=False)
        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            result = runner.invoke(cli, [
                '--insecure',
                '--verify',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'tar://%s' % tar_path,
            ])
            self.assertEqual(
                0, result.exit_code,
                'process --verify tar failed: %s'
                % (result.stderr or result.output))
            self.assertIn(
                'verified OK', result.stderr,
                'Summary should include verified OK')
        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)

    def test_verify_tar_with_filter_passes(self):
        """Process busybox to tar:// with a filter and
        --verify, verify passes."""
        runner = CliRunner(mix_stderr=False)
        with tempfile.NamedTemporaryFile(
                delete=False, suffix='.tar') as tf:
            tar_path = tf.name

        try:
            result = runner.invoke(cli, [
                '--insecure',
                '--verify',
                'process',
                'registry://localhost:5000/'
                'library/busybox:latest',
                'tar://%s' % tar_path,
                '-f', 'normalize-timestamps',
            ])
            self.assertEqual(
                0, result.exit_code,
                'process --verify tar+filter failed: %s'
                % (result.stderr or result.output))
            self.assertIn(
                'verified OK', result.stderr,
                'Summary should include verified OK')
        finally:
            if os.path.exists(tar_path):
                os.unlink(tar_path)


class VerifyRegistryTestCase(testtools.TestCase):
    """Test --verify with registry:// output."""

    def test_verify_registry_passes(self):
        """Process busybox to registry:// with --verify,
        verify passes."""
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(cli, [
            '--insecure',
            '--verify',
            'process',
            'registry://localhost:5000/'
            'library/busybox:latest',
            'registry://localhost:5000/'
            'occystrap_verify_test:latest',
        ])
        self.assertEqual(
            0, result.exit_code,
            'process --verify registry failed: %s'
            % (result.stderr or result.output))
        self.assertIn(
            'verified OK', result.stderr,
            'Summary should include verified OK')
