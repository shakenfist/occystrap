"""Tests for the overlay mount output."""

import json
import os
import tempfile
from unittest import mock

from oslo_concurrency import processutils
import testtools

from occystrap import common
from occystrap.outputs import mounts


LAYERS = ['/i/%s/layer' % (c * 64) for c in 'abc']


class TestMountOverlay(testtools.TestCase):
    @mock.patch('occystrap.util.execute')
    def test_uses_one_lowerdir_plus_per_layer(self, mock_execute):
        mounts.mount_overlay(LAYERS, '/i/delta', '/i/working', '/i/rootfs')

        mock_execute.assert_called_once_with(
            'mount -t overlay overlay -o '
            'lowerdir+=%s,lowerdir+=%s,lowerdir+=%s,'
            'upperdir=/i/delta,workdir=/i/working /i/rootfs' % tuple(LAYERS))

    @mock.patch('occystrap.util.execute')
    def test_falls_back_to_single_lowerdir(self, mock_execute):
        mock_execute.side_effect = [
            processutils.ProcessExecutionError(exit_code=32), None]

        mounts.mount_overlay(LAYERS, '/i/delta', '/i/working', '/i/rootfs')

        self.assertEqual(2, mock_execute.call_count)
        self.assertEqual(
            mock.call('mount -t overlay overlay -o lowerdir=%s,'
                      'upperdir=/i/delta,workdir=/i/working /i/rootfs'
                      % ':'.join(LAYERS)),
            mock_execute.call_args)

    @mock.patch('occystrap.util.execute')
    def test_fallback_failure_propagates(self, mock_execute):
        mock_execute.side_effect = processutils.ProcessExecutionError(
            exit_code=32)

        self.assertRaises(
            processutils.ProcessExecutionError, mounts.mount_overlay,
            LAYERS, '/i/delta', '/i/working', '/i/rootfs')
        self.assertEqual(2, mock_execute.call_count)


class TestWriteContainerConfig(testtools.TestCase):
    def _write(self, image_config):
        with tempfile.TemporaryDirectory() as tempdir:
            container_config = os.path.join(tempdir, 'container-config.json')
            runtime_config = os.path.join(tempdir, 'config.json')
            with open(container_config, 'w') as f:
                f.write(json.dumps({'config': image_config}))
            common.write_container_config(container_config, runtime_config)
            with open(runtime_config) as f:
                return json.loads(f.read())

    def test_missing_working_dir_defaults_to_root(self):
        conf = self._write({'Cmd': ['/bin/bash']})
        self.assertEqual('/', conf['process']['cwd'])

    def test_empty_working_dir_defaults_to_root(self):
        conf = self._write({'WorkingDir': '', 'Cmd': ['/bin/bash']})
        self.assertEqual('/', conf['process']['cwd'])

    def test_working_dir_is_used(self):
        conf = self._write({'WorkingDir': '/app', 'Cmd': ['/bin/bash']})
        self.assertEqual('/app', conf['process']['cwd'])
