# OCI bundles are a special case of our directory output -- they don't contain
# all of the data that a directory output does, and they place the data they
# do contain into different locations within the directory structure.

import json
import logging
import os
import shutil

from occystrap.constants import RUNC_SPEC_TEMPLATE
from occystrap.output_directory import DirWriter


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class OCIBundleWriter(DirWriter):
    def __init__(self, image, tag, image_path):
        super(OCIBundleWriter, self).__init__(
            image, tag, image_path, expand=True)

    def finalize(self):
        self._log_bundle()

    def write_bundle(self, container_template=RUNC_SPEC_TEMPLATE,
                     container_values=None):
        if not container_values:
            container_values = {}

        rootfs_path = os.path.join(self.image_path, 'rootfs')
        if not os.path.exists(rootfs_path):
            os.makedirs(rootfs_path)
        LOG.info('Writing image bundle to %s' % rootfs_path)
        self._extract_rootfs(rootfs_path)

        # Remove parts of the output directory which are not present in OCI
        for layer_file in self.tar_manifest[0]['Layers']:
            shutil.rmtree(os.path.join(self.image_path,
                                       os.path.split(layer_file)[0]))

        # Rename the container configuration to a well known location. This is
        # not part of the OCI specification, but is convenient for now.
        container_config_filename = os.path.join(self.image_path,
                                                 'container-config.json')
        os.rename(os.path.join(self.image_path, self.tar_manifest[0]['Config']),
                  container_config_filename)

        # Read the container config
        with open(container_config_filename) as f:
            image_conf = json.loads(f.read())

        # Write a runc specification for the container
        container_conf = json.loads(container_template)

        container_conf['process']['terminal'] = True
        cwd = image_conf['config']['WorkingDir']
        if cwd == '':
            cwd = '/'
        container_conf['process']['cwd'] = cwd
        container_conf['process']['args'] = image_conf['config']['Cmd']

        # terminal = false means "pass through existing file descriptors"
        container_conf['process']['terminal'] = False

        container_conf['hostname'] = container_values.get(
            'hostname', 'occystrap')

        with open(os.path.join(self.image_path, 'config.json'), 'w') as f:
            f.write(json.dumps(container_conf, indent=4, sort_keys=True))
