import hashlib
import logging
import os
import tarfile
import tempfile

from occystrap import constants
from occystrap.filters.base import ImageFilter


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class TimestampNormalizer(ImageFilter):
    """Normalizes timestamps in image layers for reproducible builds.

    This filter rewrites layer tarballs to set all file modification times
    to a consistent value (default: 0, Unix epoch). Since this changes the
    layer content, the SHA256 hash is recalculated and the layer name is
    updated to match.

    This is useful for creating reproducible image tarballs where the same
    source content always produces the same output, regardless of when the
    files were originally created or modified.
    """

    def __init__(self, wrapped_output, timestamp=0):
        """Initialize the timestamp normalizer.

        Args:
            wrapped_output: The ImageOutput to pass normalized elements to.
            timestamp: The Unix timestamp to set for all files (default: 0).
        """
        super().__init__(wrapped_output)
        self.timestamp = timestamp

    def _normalize_layer(self, layer_data):
        """Normalize timestamps in a layer tarball.

        Creates a new tarball with all timestamps set to self.timestamp,
        calculates the new SHA256 hash, and returns both.

        Args:
            layer_data: File-like object containing the original layer.

        Returns:
            Tuple of (normalized_file_handle, new_sha256_hex)
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

                # Return a new file handle and the hash
                normalized_tf.seek(0)
                return open(normalized_tf.name, 'rb'), new_sha

            except Exception:
                os.unlink(normalized_tf.name)
                raise

    def process_image_element(self, element_type, name, data):
        """Process an image element, normalizing layer timestamps.

        Config files are passed through unchanged. Layers have their
        timestamps normalized and their names updated to reflect the
        new SHA256 hash.
        """
        if element_type == constants.IMAGE_LAYER and data is not None:
            LOG.info('Normalizing timestamps in layer %s' % name)
            normalized_data, new_name = self._normalize_layer(data)

            try:
                self._wrapped.process_image_element(
                    element_type, new_name, normalized_data)
            finally:
                # Clean up the temporary file
                try:
                    normalized_data.close()
                    os.unlink(normalized_data.name)
                except Exception:
                    pass
        else:
            # Pass through unchanged (config files, skipped layers)
            self._wrapped.process_image_element(element_type, name, data)
