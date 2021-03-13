import hashlib
import io
import json
import logging
import os
import re
import sys
import tarfile
import tempfile
import zlib

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DELETED_FILE_RE = re.compile('.*/\.wh\.(.*)$')


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

    def process_image_element(self, element_type, name, data):
        if element_type == constants.CONFIG_FILE:
            LOG.info('Writing config file to tarball')

            ti = tarfile.TarInfo(name)
            ti.size = len(data.read())
            data.seek(0)
            self.image_tar.addfile(ti, data)
            self.tar_manifest[0]['Config'] = name

        elif element_type == constants.IMAGE_LAYER:
            LOG.info('Writing layer to tarball')

            # We can use zlib for streaming decompression, but we need to tell it
            # to ignore the gzip header which it doesn't understand. Unfortunately
            # tarfile doesn't do streaming writes (and we need to know the
            # decompressed size before we can write to the tarfile), so we stream
            # to a temporary file on disk.
            try:
                h = hashlib.sha256()
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)

                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    LOG.info('Temporary file for layer is %s' % tf.name)
                    for chunk in data.iter_content(8192):
                        tf.write(d.decompress(chunk))
                        h.update(chunk)

                if h.hexdigest() != name:
                    LOG.error('Hash verification failed for layer (%s vs %s)'
                              % (name, h.hexdigest()))
                    sys.exit(1)

                name += '/layer.tar'
                self.image_tar.add(
                    tf.name, arcname=name)
                self.tar_manifest[0]['Layers'].append(name)

                with tarfile.open(tf.name) as layer:
                    for mem in layer.getmembers():
                        m = DELETED_FILE_RE.match(mem.name)
                        if m:
                            LOG.info('Layer tarball contains deleted file: %s'
                                     % mem.name)

            finally:
                os.unlink(tf.name)

    def finalize(self):
        LOG.info('Writing manifest file to tarball')
        encoded_manifest = json.dumps(self.tar_manifest).encode('utf-8')
        ti = tarfile.TarInfo('manifest.json')
        ti.size = len(encoded_manifest)
        self.image_tar.addfile(ti, io.BytesIO(encoded_manifest))
