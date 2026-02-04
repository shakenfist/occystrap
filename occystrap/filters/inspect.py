import json
import logging

from occystrap import constants
from occystrap.filters.base import ImageFilter


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class InspectFilter(ImageFilter):
    """Collects layer metadata and appends it to a JSONL file.

    This filter is a passthrough that records layer digests, sizes,
    and history information as elements flow through the pipeline.
    In finalize(), it appends one JSON line per image to the output
    file.

    This can be placed at any point in a filter chain to capture
    the state of layers at that stage of processing. Multiple
    inspect filters with different output files can be used to
    compare before/after effects of other filters.

    Output format (one JSON object per line):
        {"name": "image:tag", "layers": [
            {"Id": "sha256:...", "Size": N,
             "Created": N, "CreatedBy": "...",
             "Comment": "", "Tags": [...] or null},
            ...
        ]}
    """

    def __init__(self, wrapped_output, output_file,
                 image=None, tag=None):
        """Initialize the inspect filter.

        Args:
            wrapped_output: The ImageOutput to pass elements to,
                or None for inspect-only mode.
            output_file: Path to the file to append JSON lines to.
            image: Image name for output formatting.
            tag: Image tag for output formatting.
        """
        super().__init__(wrapped_output)
        self.output_file = output_file
        self.image = image
        self.tag = tag

        # Parsed from CONFIG_FILE
        self._history = []  # Non-empty-layer history entries

        # Collected from IMAGE_LAYER elements
        self._layers = []  # List of (digest, size) tuples

    def _parse_config(self, data):
        """Parse the image config to extract history entries.

        The config's history array has entries for all Dockerfile
        steps, including no-op steps (ENV, LABEL, CMD, etc.)
        marked with empty_layer=True. We extract only the entries
        that correspond to actual filesystem layers.
        """
        data.seek(0)
        try:
            config = json.load(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            LOG.warning('Failed to parse image config: %s' % e)
            return

        history = config.get('history', [])
        for entry in history:
            if not entry.get('empty_layer', False):
                self._history.append(entry)

    def _normalize_digest(self, name):
        """Ensure digest has sha256: prefix."""
        if name and not name.startswith('sha256:'):
            return 'sha256:%s' % name
        return name

    def process_image_element(self, element_type, name, data):
        """Process an image element, recording layer metadata."""
        if element_type == constants.CONFIG_FILE and data is not None:
            self._parse_config(data)

        if element_type == constants.IMAGE_LAYER:
            if data is not None:
                data.seek(0, 2)
                size = data.tell()
                data.seek(0)
            else:
                size = 0
            self._layers.append((name, size))

        # Pass through to wrapped output
        if self._wrapped is not None:
            if data is not None:
                data.seek(0)
            self._wrapped.process_image_element(
                element_type, name, data)

    def _build_layer_entries(self):
        """Build layer entry dicts by correlating layers with
        history.

        Returns layers in reverse order (newest first) to match
        the convention used by docker history.
        """
        entries = []
        image_tag = None
        if self.image and self.tag:
            image_tag = '%s:%s' % (self.image, self.tag)

        for i, (digest, size) in enumerate(self._layers):
            entry = {
                'Id': self._normalize_digest(digest),
                'Size': size,
                'Created': 0,
                'CreatedBy': '',
                'Comment': '',
                'Tags': None,
            }

            # Correlate with history if available
            if i < len(self._history):
                hist = self._history[i]
                created = hist.get('created', '')
                if isinstance(created, str):
                    # Convert ISO format to unix timestamp
                    import datetime
                    try:
                        dt = datetime.datetime.fromisoformat(
                            created.replace('Z', '+00:00'))
                        entry['Created'] = int(dt.timestamp())
                    except (ValueError, OSError):
                        entry['Created'] = 0
                elif isinstance(created, (int, float)):
                    entry['Created'] = int(created)
                entry['CreatedBy'] = hist.get('created_by', '')
                entry['Comment'] = hist.get('comment', '')

            entries.append(entry)

        # Reverse to match docker history convention
        # (newest first) and tag the topmost layer
        entries.reverse()
        if entries and image_tag:
            entries[0]['Tags'] = [image_tag]

        return entries

    def _write_output(self):
        """Append a JSON line to the output file."""
        image_tag = ''
        if self.image and self.tag:
            image_tag = '%s:%s' % (self.image, self.tag)
        elif self.image:
            image_tag = self.image

        record = {
            'name': image_tag,
            'layers': self._build_layer_entries(),
        }

        line = json.dumps(record, sort_keys=True)
        with open(self.output_file, 'a') as f:
            f.write(line + '\n')

        LOG.info(
            'Wrote inspect data for %s (%d layers) to %s'
            % (image_tag, len(self._layers), self.output_file))

    def finalize(self):
        """Write collected metadata and finalize wrapped output."""
        self._write_output()

        if self._wrapped is not None:
            self._wrapped.finalize()
