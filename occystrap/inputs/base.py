from abc import ABC, abstractmethod


class ImageInput(ABC):
    """Abstract base class for image input sources.

    Input sources are responsible for fetching container
    images from various sources (registries, local Docker
    daemon, tarfiles) and yielding ImageElement objects.
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
    def fetch(self, fetch_callback=None, ordered=True):
        """Fetch image elements (config files and layers).

        Args:
            fetch_callback: Optional callable that takes
                a layer digest and returns True if the
                layer should be fetched, False to skip.
                If None, all layers are fetched.
            ordered: If True, yield layers in manifest
                order (default). If False, yield layers
                as they become available and set
                layer_index on each ImageElement.

        Yields:
            ImageElement instances.
        """
        pass
