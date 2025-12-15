from abc import ABC, abstractmethod


class ImageInput(ABC):
    """Abstract base class for image input sources.

    Input sources are responsible for fetching container images from various
    sources (registries, local Docker daemon, tarfiles) and yielding image
    elements (config files and layers) in a standard format.
    """

    @property
    @abstractmethod
    def image(self):
        """Return the image name."""
        pass

    @property
    @abstractmethod
    def tag(self):
        """Return the image tag."""
        pass

    @abstractmethod
    def fetch(self, fetch_callback=None):
        """Fetch image elements (config files and layers).

        Args:
            fetch_callback: Optional callable that takes a layer digest and
                returns True if the layer should be fetched, False to skip.
                If None, all layers are fetched.

        Yields:
            Tuples of (element_type, name, data) where:
            - element_type is constants.CONFIG_FILE or constants.IMAGE_LAYER
            - name is the element identifier (config filename or layer digest)
            - data is a file-like object containing the element data,
              or None if the layer was skipped by fetch_callback
        """
        pass
