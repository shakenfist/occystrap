from abc import ABC, abstractmethod
import logging
import time


LOG = logging.getLogger(__name__)


class ImageOutput(ABC):
    """Abstract base class for image output writers.

    Output writers receive image elements (config files and layers) from input
    sources and write them to various destinations (tarballs, directories,
    OCI bundles, etc.).
    """

    def __init__(self):
        """Initialize tracking for summary statistics."""
        self._start_time = None
        self._total_bytes = 0
        self._layer_count = 0

    def _track_element(self, element_type, size):
        """Track an element for summary statistics.

        Call this from process_image_element() to track bytes and layers.

        Args:
            element_type: The element type (CONFIG_FILE or IMAGE_LAYER)
            size: Size of the element in bytes
        """
        if self._start_time is None:
            self._start_time = time.time()

        self._total_bytes += size
        # Import here to avoid circular import
        from occystrap import constants
        if element_type == constants.IMAGE_LAYER:
            self._layer_count += 1

    def _log_summary(self):
        """Log a summary of the processing.

        Call this at the end of finalize() to print the summary line.
        """
        if self._start_time is None:
            return

        elapsed = time.time() - self._start_time
        LOG.info(f'Processed {self._total_bytes} bytes in '
                 f'{self._layer_count} layers in {elapsed:.1f} seconds')

    @abstractmethod
    def fetch_callback(self, digest):
        """Determine whether a layer should be fetched.

        This is called by input sources before fetching each layer, allowing
        output writers to skip layers that already exist in the destination.

        Args:
            digest: The layer digest/identifier.

        Returns:
            True if the layer should be fetched, False to skip.
        """
        pass

    @abstractmethod
    def process_image_element(self, element_type, name, data):
        """Process a single image element (config or layer).

        Args:
            element_type: constants.CONFIG_FILE or constants.IMAGE_LAYER
            name: The element identifier (config filename or layer digest)
            data: A file-like object containing the element data,
                or None if the layer was skipped by fetch_callback
        """
        pass

    @abstractmethod
    def finalize(self):
        """Complete the output operation.

        This is called after all image elements have been processed. Use this
        to write manifests, close files, or perform any final cleanup.
        """
        pass
