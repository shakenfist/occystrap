from abc import ABC, abstractmethod


class ImageOutput(ABC):
    """Abstract base class for image output writers.

    Output writers receive image elements (config files and layers) from input
    sources and write them to various destinations (tarballs, directories,
    OCI bundles, etc.).
    """

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
