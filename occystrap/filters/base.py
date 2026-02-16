from abc import ABC, abstractmethod
import hashlib
import io
import json
import logging

from occystrap import constants
from occystrap.outputs.base import ImageOutput


LOG = logging.getLogger(__name__)


class ImageFilter(ImageOutput, ABC):
    """Abstract base class for image filters.

    Filters wrap an ImageOutput and can transform or
    inspect image elements as they pass through the
    pipeline. Filters implement the ImageOutput
    interface so they can be chained together or used
    as the final output.

    The decorator pattern allows filters to be stacked:
        input -> filter1 -> filter2 -> output

    Each filter can:
    - Transform element data (e.g., normalize
      timestamps)
    - Transform element names (e.g., recalculate
      hashes)
    - Inspect elements without modification (e.g.,
      search)
    - Skip elements entirely
    - Accumulate state across elements (e.g., collect
      search results)

    Filters that modify layer content (and thus change
    the layer's diff_id) should call _buffer_config()
    for config elements and _record_new_diff_id() for
    each modified layer. The base class finalize() will
    then update the config's rootfs.diff_ids to match
    the actual layer content before forwarding it.
    """

    def __init__(self, wrapped_output, temp_dir=None):
        """Wrap another output (or filter) to form a
        chain.

        Args:
            wrapped_output: The ImageOutput to pass
                processed elements to. Can be None for
                terminal filters that don't produce
                output (e.g., search-only mode).
            temp_dir: Directory for temporary files
                (default: system temp directory).
        """
        self._wrapped = wrapped_output
        self.temp_dir = temp_dir

        # Config buffering for diff_id updates.
        # Content-modifying filters buffer the config
        # element and forward it in finalize() with
        # updated diff_ids.
        self._buffered_config = None
        self._new_diff_ids = {}
        self._layer_counter = 0

    @property
    def requires_ordered_layers(self):
        """Delegate ordering requirement to the wrapped
        output.

        If there is no wrapped output, default to
        requiring ordered delivery.
        """
        if self._wrapped is None:
            return True
        return self._wrapped.requires_ordered_layers

    def fetch_callback(self, digest):
        """Determine whether a layer should be fetched.

        Default implementation delegates to the wrapped
        output. Override to implement custom filtering
        logic.
        """
        if self._wrapped is None:
            return True
        return self._wrapped.fetch_callback(digest)

    @abstractmethod
    def process_image_element(self, element):
        """Process and optionally transform an image
        element.

        Implementations should typically:
        1. Perform any transformation or inspection
        2. Pass the (possibly modified) element to
           self._wrapped

        When creating a modified element, preserve
        layer_index from the original element.

        Args:
            element: An ImageElement instance.
        """
        pass

    def _buffer_config(self, element):
        """Buffer config for later diff_id update.

        Content-modifying filters should call this
        instead of forwarding the config element
        directly. The config will be forwarded in
        finalize() with updated diff_ids.

        Args:
            element: The CONFIG_FILE ImageElement.
        """
        self._buffered_config = element

    def _record_new_diff_id(
            self, sha256_hex, layer_index):
        """Record a new diff_id for a modified layer.

        Args:
            sha256_hex: SHA256 hex digest of the
                modified decompressed layer data
                (without 'sha256:' prefix).
            layer_index: The layer's position index,
                or None for ordered delivery.
        """
        idx = (layer_index
               if layer_index is not None
               else self._layer_counter)
        self._new_diff_ids[idx] = sha256_hex
        self._layer_counter += 1

    def _skip_layer(self, layer_index):
        """Advance the layer counter for an unmodified
        layer (data=None, skipped by fetch_callback).

        Only needed for ordered delivery where
        layer_index is None.

        Args:
            layer_index: The layer's position index,
                or None for ordered delivery.
        """
        if layer_index is None:
            self._layer_counter += 1

    def _forward_buffered_config(self):
        """Forward the buffered config to the wrapped
        output, updating diff_ids if any layers were
        modified.

        Called automatically by finalize(). Does
        nothing if no config was buffered.
        """
        if self._buffered_config is None:
            return

        if not self._new_diff_ids:
            self._wrapped.process_image_element(
                self._buffered_config)
            self._buffered_config = None
            return

        # Parse the original config
        self._buffered_config.data.seek(0)
        config = json.loads(
            self._buffered_config.data.read())

        original_ids = config.get(
            'rootfs', {}).get('diff_ids', [])

        # Replace diff_ids for modified layers,
        # keeping originals for unmodified layers
        updated_ids = list(original_ids)
        for idx, sha in self._new_diff_ids.items():
            if idx < len(updated_ids):
                updated_ids[idx] = (
                    'sha256:%s' % sha)

        if updated_ids == original_ids:
            self._wrapped.process_image_element(
                self._buffered_config)
        else:
            LOG.info(
                'Updating config diff_ids '
                '(%d of %d layers modified)'
                % (len(self._new_diff_ids),
                   len(original_ids)))
            config['rootfs']['diff_ids'] = updated_ids
            updated_bytes = json.dumps(
                config).encode('utf-8')

            h = hashlib.sha256()
            h.update(updated_bytes)
            updated_name = (
                '%s.json' % h.hexdigest())

            self._wrapped.process_image_element(
                constants.ImageElement(
                    constants.CONFIG_FILE,
                    updated_name,
                    io.BytesIO(updated_bytes)))

        self._buffered_config = None

    def finalize(self):
        """Complete the filter operation.

        Forwards any buffered config (with updated
        diff_ids) before delegating to the wrapped
        output.
        """
        self._forward_buffered_config()
        if self._wrapped is not None:
            self._wrapped.finalize()
