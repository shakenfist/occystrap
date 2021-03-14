import hashlib
import json
import logging
import os
import sys
import tarfile
import zlib

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class DirWriter(object):
    def __init__(self, image, tag, image_path, unique_names=False, expand=False):
        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.unique_names = unique_names
        self.expand = expand

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]
        if self.unique_names:
            self.tar_manifest[0]['ImageName'] = self.image

        if not os.path.exists(self.image_path):
            os.makedirs(self.image_path)

    def _manifest_filename(self):
        if not self.unique_names:
            return 'manifest.json'
        else:
            return ('manifest-%s-%s.json' % (self.image.replace('/', '_'),
                                             self.tag.replace('/', '_')))

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
            if not os.path.exists(layer_file):
                with open(layer_file_in_dir, 'wb') as f:
                    d = data.read(102400)
                    while d:
                        f.write(d)
                        d = data.read(102400)

            if self.expand:
                with tarfile.open(layer_file_in_dir) as layer:
                    # NOTE: tarfile expects the _top_ level directory for the
                    # extraction, not the directory containing the file
                    expand_path = os.path.join(
                        self.image_path, name, 'extracted')
                    if not os.path.exists(expand_path):
                        os.makedirs(expand_path)

                    for mem in layer.getmembers():
                        if mem.name.startswith('/'):
                            LOG.warn('Ignoring layer file with possibly malicious '
                                     'absolute path' % mem.name)
                            continue
                        if mem.name.startswith('..'):
                            LOG.warn('Ignoring layer file with possibly malicious '
                                     'relative path' % mem.name)
                            continue

                        layer.extract(mem, path=expand_path, set_attrs=False)

                        # NOTE: whereas, we need to create the directory tree
                        # ourselves for the merged layer...
                        merged_filename = os.path.join(
                            self.image_path, self._manifest_filename(), mem.name)
                        merged_path = os.path.dirname(merged_filename)

                        if not os.path.exists(merged_path):
                            os.makedirs(merged_path)
                        if os.path.exists(merged_filename):
                            if os.path.isdir(merged_filename):
                                os.rmdir(merged_filename)
                            else:
                                os.unlink(merged_filename)

                        if mem.isdir():
                            if (os.path.exists(merged_filename) and
                                    not os.path.isdir(merged_filename)):
                                os.rmdir(merged_filename)
                            if not os.path.exists(merged_filename):
                                os.mkdir(merged_filename)
                        else:
                            expanded_filename = os.path.join(
                                expand_path, mem.name)
                            LOG.info('Linking %s -> %s'
                                     % (expanded_filename, merged_filename))
                            os.symlink(expanded_filename, merged_filename)

    def finalize(self):
        manifest_filename = self._manifest_filename()
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


class NoSuchImageException(Exception):
    pass


class DirReader(object):
    def __init__(self, path, image, tag):
        self.path = path
        self.image = image
        self.tag = tag

        c = {}
        catalog_path = os.path.join(self.path, 'catalog.json')
        if os.path.exists(catalog_path):
            with open(catalog_path, 'r') as f:
                c = json.loads(f.read())

        if not self.image in c:
            raise NoSuchImageException(self.image)
        if not self.tag in c[self.image]:
            raise NoSuchImageException(self.image)

        self.manifest_filename = c[self.image][self.tag]

    def fetch(self):
        with open(os.path.join(self.path, self.manifest_filename)) as f:
            manifest = json.loads(f.read())

        config_filename = manifest[0]['Config']
        with open(os.path.join(self.path, config_filename), 'rb') as f:
            yield(constants.CONFIG_FILE, config_filename, f)

        for layer in manifest[0]['Layers']:
            with open(os.path.join(self.path, layer), 'rb') as f:
                yield (constants.IMAGE_LAYER, layer, f)
