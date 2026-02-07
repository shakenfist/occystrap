import io
import json
import logging
import os
import tarfile

from occystrap import compression
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
            names = tf.getnames()

            if 'manifest.json' not in names:
                # Check if this is a legacy format tarball (pre-Docker 1.10)
                if 'repositories' in names:
                    raise ValueError(
                        'This tarball appears to be in legacy Docker format '
                        '(pre-1.10, circa 2016). occystrap only supports '
                        'Docker 1.10+ tarballs which contain manifest.json. '
                        'To convert: docker load < old.tar && '
                        'docker save image:tag > new.tar'
                    )
                raise ValueError(
                    'Invalid tarball: no manifest.json found. '
                    'This does not appear to be a valid docker save tarball.'
                )

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
                # Layer path format varies by Docker version:
                # - Traditional (v1.10-v24): "<digest>/layer.tar"
                # - OCI format (v25+): "blobs/sha256/<digest>"
                if layer_path.startswith('blobs/'):
                    # OCI format: extract digest from end of path
                    layer_digest = os.path.basename(layer_path)
                else:
                    # Traditional format: extract digest from directory name
                    layer_digest = os.path.dirname(layer_path)
                if not fetch_callback(layer_digest):
                    LOG.info('Fetch callback says skip layer %s' % layer_digest)
                    yield (constants.IMAGE_LAYER, layer_digest, None)
                    continue

                LOG.info('Reading layer %s' % layer_path)
                layer_member = tf.getmember(layer_path)
                layer_file = tf.extractfile(layer_member)
                layer_data = layer_file.read()

                # For OCI format (blobs/ paths), layers may be compressed
                if layer_path.startswith('blobs/'):
                    compression_type = compression.detect_compression(
                        layer_data)
                    if compression_type in (constants.COMPRESSION_GZIP,
                                            constants.COMPRESSION_ZSTD):
                        LOG.info('Decompressing %s layer' % compression_type)
                        layer_data = compression.decompress_data(
                            layer_data, compression_type)

                yield (constants.IMAGE_LAYER, layer_digest,
                       io.BytesIO(layer_data))

        LOG.info('Done')
