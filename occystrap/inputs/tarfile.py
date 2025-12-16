import io
import json
import logging
import os
import tarfile

from occystrap import constants
from occystrap.inputs.base import ImageInput


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


def always_fetch(digest):
    return True


class Image(ImageInput):
    def __init__(self, tarfile_path):
        self.tarfile_path = tarfile_path
        self._manifest = None
        self._image = None
        self._tag = None
        self._load_manifest()

    def _load_manifest(self):
        with tarfile.open(self.tarfile_path, 'r') as tf:
            manifest_member = tf.getmember('manifest.json')
            manifest_file = tf.extractfile(manifest_member)
            self._manifest = json.loads(manifest_file.read().decode('utf-8'))

        # Parse image and tag from RepoTags
        # Format is typically ["image:tag"] or ["registry/image:tag"]
        repo_tags = self._manifest[0].get('RepoTags', [])
        if repo_tags:
            repo_tag = repo_tags[0]
            if ':' in repo_tag:
                self._image, self._tag = repo_tag.rsplit(':', 1)
            else:
                self._image = repo_tag
                self._tag = 'latest'
        else:
            # Fallback if no RepoTags
            self._image = 'unknown'
            self._tag = 'unknown'

    @property
    def image(self):
        return self._image

    @property
    def tag(self):
        return self._tag

    def fetch(self, fetch_callback=always_fetch):
        LOG.info('Reading image from tarball %s' % self.tarfile_path)

        with tarfile.open(self.tarfile_path, 'r') as tf:
            # Yield config file
            config_filename = self._manifest[0]['Config']
            LOG.info('Reading config file %s' % config_filename)
            config_member = tf.getmember(config_filename)
            config_file = tf.extractfile(config_member)
            config_data = config_file.read()
            yield (constants.CONFIG_FILE, config_filename,
                   io.BytesIO(config_data))

            # Yield each layer
            layers = self._manifest[0]['Layers']
            LOG.info('There are %d image layers' % len(layers))

            for layer_path in layers:
                # Layer path is like "abc123/layer.tar"
                layer_digest = os.path.dirname(layer_path)
                if not fetch_callback(layer_digest):
                    LOG.info('Fetch callback says skip layer %s' % layer_digest)
                    yield (constants.IMAGE_LAYER, layer_digest, None)
                    continue

                LOG.info('Reading layer %s' % layer_path)
                layer_member = tf.getmember(layer_path)
                layer_file = tf.extractfile(layer_member)
                layer_data = layer_file.read()
                yield (constants.IMAGE_LAYER, layer_digest,
                       io.BytesIO(layer_data))

        LOG.info('Done')
