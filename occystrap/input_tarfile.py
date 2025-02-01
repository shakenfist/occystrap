import json
import logging
import tarfile

from occystrap import constants

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


# This code reads v1.2 format image tarballs.
# v1.2 is documented at https://github.com/moby/docker-image-spec/blob/v1.2.0/v1.2.md
# v2 is documented at https://github.com/opencontainers/image-spec/blob/main/


def always_fetch():
    return True


class Image(object):
    def __init__(self, tarfile_path):
        self.tarfile_path = tarfile_path
        self.tf = tarfile.open(self.tarfile_path)

        with self.tf.extractfile('manifest.json') as f:
            self.manifest = json.loads(f.read())
        self.image, self.tag = self.manifest[0]['RepoTags'][0].split(':')

    def fetch(self, fetch_callback=always_fetch):
        with self.tf.extractfile('index.json') as f:
            yield (constants.INDEX_ENTRY, 'index.json', f)

        config_filename = self.manifest[0]['Config']
        with self.tf.extractfile(config_filename) as f:
            yield (constants.CONFIG_FILE, config_filename, f)

        for layer_filename in self.manifest[0]['Layers']:
            with self.tf.extractfile(layer_filename) as f:
                yield (constants.IMAGE_LAYER, layer_filename, f)
