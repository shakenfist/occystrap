import hashlib
import io
import json
import logging
import os
import tarfile
import tempfile

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


# This code creates v1.2 format image tarballs.
# v1.2 is documented at https://github.com/moby/docker-image-spec/blob/v1.2.0/v1.2.md
# v2 is documented at https://github.com/opencontainers/image-spec/blob/main/

class TarWriter(object):
    def __init__(self, image, tag, image_path, normalize_timestamps=False,
                 timestamp=0):
        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.image_tar = tarfile.open(image_path, 'w')
        self.normalize_timestamps = normalize_timestamps
        self.timestamp = timestamp

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]

    def fetch_callback(self, digest):
        return True

    def _normalize_layer_timestamps(self, layer_data):
        """Normalize timestamps in a layer tarball and return the modified
        data along with its new SHA256 hash.
        """
        with tempfile.NamedTemporaryFile(delete=False) as normalized_tf:
            try:
                # Create a new tarball with normalized timestamps
                with tarfile.open(fileobj=normalized_tf, mode='w') as \
                        normalized_tar:
                    layer_data.seek(0)
                    with tarfile.open(fileobj=layer_data, mode='r') as \
                            layer_tar:
                        for member in layer_tar:
                            # Normalize all timestamp fields
                            member.mtime = self.timestamp

                            # Extract the file data if it's a regular file
                            if member.isfile():
                                fileobj = layer_tar.extractfile(member)
                                normalized_tar.addfile(member, fileobj)
                            else:
                                normalized_tar.addfile(member)

                # Calculate SHA256 of the normalized tarball
                normalized_tf.flush()
                normalized_tf.seek(0)
                h = hashlib.sha256()
                while True:
                    chunk = normalized_tf.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)

                new_sha = h.hexdigest()

                # Return the file handle and new hash
                normalized_tf.seek(0)
                return open(normalized_tf.name, 'rb'), new_sha

            except Exception:
                os.unlink(normalized_tf.name)
                raise

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

            if self.normalize_timestamps:
                LOG.info('Normalizing timestamps in layer')
                data, new_sha = self._normalize_layer_timestamps(data)
                # Update the layer name to use the new SHA
                name = new_sha

            name += '/layer.tar'
            ti = tarfile.TarInfo(name)
            data.seek(0, os.SEEK_END)
            ti.size = data.tell()
            data.seek(0)
            self.image_tar.addfile(ti, data)
            self.tar_manifest[0]['Layers'].append(name)

            # Clean up the temporary file if we normalized
            if self.normalize_timestamps:
                try:
                    data.close()
                    os.unlink(data.name)
                except Exception:
                    pass

    def finalize(self):
        LOG.info('Writing manifest file to tarball')
        encoded_manifest = json.dumps(self.tar_manifest).encode('utf-8')
        ti = tarfile.TarInfo('manifest.json')
        ti.size = len(encoded_manifest)
        self.image_tar.addfile(ti, io.BytesIO(encoded_manifest))
