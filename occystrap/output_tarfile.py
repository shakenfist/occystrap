import io
import json
import logging
import os
import tarfile

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


# This code creates v1.2 format image tarballs.
# v1.2 is documented at https://github.com/moby/docker-image-spec/blob/v1.2.0/v1.2.md
# v2 is documented at https://github.com/opencontainers/image-spec/blob/main/

class TarWriter(object):
    def __init__(self, image, tag, image_path):
        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.image_tar = tarfile.open(image_path, 'w')

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]

    def fetch_callback(self, digest):
        return True

    def process_image_element(self, element_type, name, data):
        if element_type == constants.INDEX_ENTRY:
            LOG.info(f'Writing index file to tarball: {name}')

            ti = tarfile.TarInfo(name)
            ti.size = len(data.read())
            data.seek(0)
            self.image_tar.addfile(ti, data)

        elif element_type == constants.CONFIG_FILE:
            LOG.info(f'Writing config file to tarball: {name}')

            ti = tarfile.TarInfo(name)
            ti.size = len(data.read())
            data.seek(0)
            self.image_tar.addfile(ti, data)
            self.tar_manifest[0]['Config'] = name

        elif element_type == constants.IMAGE_LAYER:
            name += '/layer.tar'
            LOG.info(f'Writing layer to tarball: {name}')

            ti = tarfile.TarInfo(name)
            data.seek(0, os.SEEK_END)
            ti.size = data.tell()
            data.seek(0)
            self.image_tar.addfile(ti, data)
            self.tar_manifest[0]['Layers'].append(name)

    def finalize(self):
        LOG.info('Writing manifest file to tarball: manifest.json')
        manifest_json = json.dumps(self.tar_manifest, indent=4, sort_keys=True)
        for line in manifest_json.split('\n'):
            LOG.info(f'    manifest: {line}')
        LOG.info('--- end --- ')

        encoded_manifest = manifest_json.encode('utf-8')
        ti = tarfile.TarInfo('manifest.json')
        ti.size = len(encoded_manifest)
        self.image_tar.addfile(ti, io.BytesIO(encoded_manifest))
