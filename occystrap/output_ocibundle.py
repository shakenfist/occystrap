# OCI bundles are a special case of our directory output -- they don't contain
# all of the data that a directory output does, and they place the data they
# do contain into different locations within the directory structure.

import logging
import os
import shutil

from occystrap.output_directory import DirWriter


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class OCIBundleWriter(DirWriter):
    def __init__(self, image, tag, image_path):
        super(OCIBundleWriter, self).__init__(
            image, tag, image_path, expand=True)

    def finalize(self):
        pass

    def write_bundle(self):
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
        os.rename(os.path.join(self.image_path, self.tar_manifest[0]['Config']),
                  os.path.join(self.image_path, 'container-config.json'))
