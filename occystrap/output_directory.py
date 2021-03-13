import hashlib
import json
import logging
import os
import sys
import zlib

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class DirWriter(object):
    def __init__(self, image, tag, image_path):
        self.image = image
        self.tag = tag
        self.image_path = image_path

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]

        if not os.path.exists(self.image_path):
            os.makedirs(self.image_path)

    def process_image_element(self, element_type, name, data):
        if element_type == constants.CONFIG_FILE:
            with open(os.path.join(self.image_path, name), 'wb') as f:
                d = json.loads(data.read())
                f.write(json.dumps(d, indent=4, sort_keys=True).encode('ascii'))

        elif element_type == constants.IMAGE_LAYER:
            # We can use zlib for streaming decompression, but we need to tell it
            # to ignore the gzip header which it doesn't understand. Unfortunately
            # tarfile doesn't do streaming writes (and we need to know the
            # decompressed size before we can write to the tarfile), so we stream
            # to a temporary file on disk.
            h = hashlib.sha256()
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)

            layer_dir = os.path.join(self.image_path, name)
            if not os.path.exists(layer_dir):
                os.makedirs(layer_dir)

            layer_file = os.path.join(layer_dir, 'layer.tar')
            with open(layer_file, 'wb') as f:
                for chunk in data.iter_content(8192):
                    f.write(d.decompress(chunk))
                    h.update(chunk)

            if h.hexdigest() != name:
                LOG.error('Hash verification failed for layer (%s vs %s)'
                          % (name, h.hexdigest()))
                sys.exit(1)

            self.tar_manifest[0]['Layers'].append(layer_file)

    def finalize(self):
        with open(os.path.join(self.image_path, 'manifest.json'), 'wb') as f:
            f.write(json.dumps(self.tar_manifest, indent=4,
                               sort_keys=True).encode('ascii'))
