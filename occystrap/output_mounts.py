import json
import logging
import os
import stat
import tarfile

from occystrap import common
from occystrap import constants
from occystrap import util


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class MountWriter(object):
    def __init__(self, image, tag, image_path):
        self.image = image
        self.tag = tag
        self.image_path = image_path

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]

        self.bundle = {}

        if not os.path.exists(self.image_path):
            os.makedirs(self.image_path)

    def _manifest_filename(self):
        return 'manifest'

    def fetch_callback(self, digest):
        layer_file_in_dir = os.path.join(self.image_path, digest, 'layer.tar')
        LOG.info('Layer file is %s' % layer_file_in_dir)
        return not os.path.exists(layer_file_in_dir)

    def process_image_element(self, element_type, name, data):
        if element_type == constants.CONFIG_FILE:
            with open(os.path.join(self.image_path, name), 'wb') as f:
                d = json.loads(data.read())
                f.write(json.dumps(d, indent=4, sort_keys=True).encode('ascii'))
            self.tar_manifest[0]['Config'] = name

        elif element_type == constants.IMAGE_LAYER:
            layer_dir = os.path.join(self.image_path, name)
            if not os.path.exists(layer_dir):
                os.makedirs(layer_dir)

            layer_file = os.path.join(name, 'layer.tar')
            self.tar_manifest[0]['Layers'].append(layer_file)

            layer_file_in_dir = os.path.join(self.image_path, layer_file)
            if os.path.exists(layer_file_in_dir):
                LOG.info('Skipping layer already in output directory')
            else:
                with open(layer_file_in_dir, 'wb') as f:
                    d = data.read(102400)
                    while d:
                        f.write(d)
                        d = data.read(102400)

                layer_dir_in_dir = os.path.join(self.image_path, name, 'layer')
                os.makedirs(layer_dir_in_dir)
                with tarfile.open(layer_file_in_dir) as layer:
                    for mem in layer.getmembers():
                        dirname, filename = os.path.split(mem.name)

                        # Some light reading on how this works...
                        # https://www.madebymikal.com/interpreting-whiteout-files-in-docker-image-layers/
                        # https://github.com/opencontainers/image-spec/blob/main/layer.md#opaque-whiteout
                        if filename == '.wh..wh..opq':
                            # A deleted directory, but only for layers below
                            # this one.
                            os.setxattr(os.path.join(layer_dir_in_dir, dirname),
                                        'trusted.overlay.opaque', b'y')

                        elif filename.startswith('.wh.'):
                            # A single deleted element, which might not be a
                            # file.
                            os.mknod(os.path.join(layer_dir_in_dir,
                                     mem.name[4:]),
                                     mode=stat.S_IFCHR, device=0)

                        else:
                            path = mem.name
                            layer.extract(path, path=layer_dir_in_dir)

    def finalize(self):
        manifest_filename = self._manifest_filename() + '.json'
        manifest_path = os.path.join(self.image_path, manifest_filename)
        with open(manifest_path, 'wb') as f:
            f.write(json.dumps(self.tar_manifest, indent=4,
                               sort_keys=True).encode('ascii'))

        c = {}
        catalog_path = os.path.join(self.image_path, 'catalog.json')
        if os.path.exists(catalog_path):
            with open(catalog_path, 'r') as f:
                c = json.loads(f.read())

        c.setdefault(self.image, {})
        c[self.image][self.tag] = manifest_filename
        with open(catalog_path, 'w') as f:
            f.write(json.dumps(c, indent=4, sort_keys=True))

    def write_bundle(self, container_template=constants.RUNC_SPEC_TEMPLATE,
                     container_values=None):
        if not container_values:
            container_values = {}

        rootfs_path = os.path.join(self.image_path, 'rootfs')
        if not os.path.exists(rootfs_path):
            os.makedirs(rootfs_path)
        LOG.info('Writing image bundle to %s' % rootfs_path)

        working_path = os.path.join(self.image_path, 'working')
        if not os.path.exists(working_path):
            os.makedirs(working_path)

        delta_path = os.path.join(self.image_path, 'delta')
        if not os.path.exists(delta_path):
            os.makedirs(delta_path)

        # The newest layer is listed first in the mount command
        layer_dirs = []
        self.tar_manifest[0]['Layers'].reverse()
        for layer in self.tar_manifest[0]['Layers']:
            layer_dirs.append(os.path.join(
                self.image_path, layer.replace('.tar', '')))

        # Extract the rootfs as overlay mounts
        util.execute('mount -t overlay overlay -o lowerdir=%(layers)s,'
                     'upperdir=%(upper)s,workdir=%(working)s %(rootfs)s'
                     % {
                         'layers': ':'.join(layer_dirs),
                         'upper': delta_path,
                         'working': working_path,
                         'rootfs': rootfs_path
                     })

        # Rename the container configuration to a well known location. This is
        # not part of the OCI specification, but is convenient for now.
        container_config_filename = os.path.join(self.image_path,
                                                 'container-config.json')
        runtime_config_filename = os.path.join(self.image_path, 'config.json')
        os.rename(os.path.join(self.image_path, self.tar_manifest[0]['Config']),
                  container_config_filename)

        common.write_container_config(container_config_filename,
                                      runtime_config_filename,
                                      container_template=container_template,
                                      container_values=container_values)
