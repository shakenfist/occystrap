import fnmatch
import hashlib
import logging
import os
import tarfile
import tempfile

from occystrap import constants
from occystrap.filters.base import ImageFilter


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class ExcludeFilter(ImageFilter):
    """Excludes files matching glob patterns from image layers.

    This filter rewrites layer tarballs to remove files and directories
    that match any of the specified glob patterns. Since this changes the
    layer content, the SHA256 hash is recalculated and the layer name is
    updated to match.

    This is useful for stripping unwanted content like .git directories,
    __pycache__ folders, or other files before writing output.
    """

    def __init__(self, wrapped_output, patterns):
        """Initialize the exclude filter.

        Args:
            wrapped_output: The ImageOutput to pass filtered elements to.
            patterns: List of glob patterns to exclude. Each pattern is
                matched against the full path using fnmatch.
        """
        super().__init__(wrapped_output)
        self.patterns = patterns

    def _matches_exclusion(self, path):
        """Check if a path matches any exclusion pattern.

        Args:
            path: The file path to check.

        Returns:
            True if the path should be excluded, False otherwise.
        """
        for pattern in self.patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def _filter_layer(self, layer_data):
        """Filter a layer tarball, excluding matching entries.

        Creates a new tarball with entries that don't match exclusion
        patterns, calculates the new SHA256 hash, and returns both.

        Args:
            layer_data: File-like object containing the original layer.

        Returns:
            Tuple of (filtered_file_handle, new_sha256_hex)
        """
        excluded_count = 0

        with tempfile.NamedTemporaryFile(delete=False) as filtered_tf:
            try:
                with tarfile.open(fileobj=filtered_tf, mode='w') as filtered_tar:
                    layer_data.seek(0)
                    with tarfile.open(fileobj=layer_data, mode='r') as layer_tar:
                        for member in layer_tar:
                            if self._matches_exclusion(member.name):
                                excluded_count += 1
                                continue

                            if member.isfile():
                                fileobj = layer_tar.extractfile(member)
                                filtered_tar.addfile(member, fileobj)
                            else:
                                filtered_tar.addfile(member)

                if excluded_count > 0:
                    LOG.info('Excluded %d entries from layer' % excluded_count)

                filtered_tf.flush()
                filtered_tf.seek(0)
                h = hashlib.sha256()
                while True:
                    chunk = filtered_tf.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)

                new_sha = h.hexdigest()

                filtered_tf.seek(0)
                return open(filtered_tf.name, 'rb'), new_sha

            except Exception:
                os.unlink(filtered_tf.name)
                raise

    def process_image_element(self, element_type, name, data):
        """Process an image element, filtering layer contents.

        Config files are passed through unchanged. Layers have matching
        entries excluded and their names updated to reflect the new
        SHA256 hash.
        """
        if element_type == constants.IMAGE_LAYER and data is not None:
            LOG.info('Filtering layer %s' % name)
            filtered_data, new_name = self._filter_layer(data)

            try:
                self._wrapped.process_image_element(
                    element_type, new_name, filtered_data)
            finally:
                try:
                    filtered_data.close()
                    os.unlink(filtered_data.name)
                except Exception:
                    pass
        else:
            self._wrapped.process_image_element(element_type, name, data)
