from abc import ABC, abstractmethod

from occystrap.outputs.base import ImageOutput


class ImageFilter(ImageOutput, ABC):
    """Abstract base class for image filters.

    Filters wrap an ImageOutput and can transform or inspect image elements
    as they pass through the pipeline. Filters implement the ImageOutput
    interface so they can be chained together or used as the final output.

    The decorator pattern allows filters to be stacked:
        input -> filter1 -> filter2 -> output

    Each filter can:
    - Transform element data (e.g., normalize timestamps)
    - Transform element names (e.g., recalculate hashes)
    - Inspect elements without modification (e.g., search)
    - Skip elements entirely
    - Accumulate state across elements (e.g., collect search results)
    """

    def __init__(self, wrapped_output):
        """Wrap another output (or filter) to form a chain.

        Args:
            wrapped_output: The ImageOutput to pass processed elements to.
                Can be None for terminal filters that don't produce output
                (e.g., search-only mode).
        """
        self._wrapped = wrapped_output

    def fetch_callback(self, digest):
        """Determine whether a layer should be fetched.

        Default implementation delegates to the wrapped output.
        Override to implement custom filtering logic.
        """
        if self._wrapped is None:
            return True
        return self._wrapped.fetch_callback(digest)

    @abstractmethod
    def process_image_element(self, element_type, name, data):
        """Process and optionally transform an image element.

        Implementations should typically:
        1. Perform any transformation or inspection
        2. Pass the (possibly modified) element to self._wrapped

        Args:
            element_type: constants.CONFIG_FILE or constants.IMAGE_LAYER
            name: The element name/digest
            data: File-like object containing the element data, or None
                if the element was skipped by fetch_callback
        """
        pass

    def finalize(self):
        """Complete the filter operation.

        Default implementation delegates to the wrapped output.
        Override to perform cleanup or output accumulated results.
        """
        if self._wrapped is not None:
            self._wrapped.finalize()
