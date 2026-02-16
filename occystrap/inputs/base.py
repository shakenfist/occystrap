from abc import ABC, abstractmethod


class ImageInputError(Exception):
    """Raised when an input source encounters an error."""
    pass


def always_fetch(digest):
    """Default fetch callback that fetches all layers."""
    return True


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

    def get_manifest(self):
        """Return the distribution manifest dict.

        Returns the OCI or Docker v2 distribution
        manifest for this image. This is the manifest
        with schemaVersion, config descriptor, and
        layer descriptors (including compressed sizes
        and media types).

        Returns None if the input source does not have
        access to a distribution manifest (e.g. docker
        daemon inputs).

        Does not download layer blobs.
        """
        return None

    def get_config(self):
        """Return the parsed OCI image config dict.

        Returns the image configuration JSON containing
        architecture, os, rootfs.diff_ids, history,
        config (env, cmd, entrypoint, labels), etc.

        Returns None if the input source cannot provide
        the config without downloading all layers.

        Does not download layer blobs.
        """
        return None
