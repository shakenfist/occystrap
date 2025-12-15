import fnmatch
import logging
import os
import re
import tarfile

from occystrap import constants
from occystrap.filters.base import ImageFilter


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class SearchFilter(ImageFilter):
    """Searches layers for files matching a pattern.

    This filter can operate in two modes:
    - Search-only: wrapped_output is None, just prints results
    - Passthrough: searches AND passes elements to wrapped output

    In passthrough mode, this allows searching while also writing output,
    enabling pipelines like:
        input -> search -> tarfile (search while creating tarball)
    """

    def __init__(self, wrapped_output, pattern, use_regex=False,
                 image=None, tag=None, script_friendly=False):
        """Initialize the search filter.

        Args:
            wrapped_output: The ImageOutput to pass elements to, or None
                for search-only mode.
            pattern: Glob pattern or regex to match file paths.
            use_regex: If True, treat pattern as a regex instead of glob.
            image: Image name for output formatting.
            tag: Image tag for output formatting.
            script_friendly: If True, output in machine-parseable format.
        """
        super().__init__(wrapped_output)
        self.pattern = pattern
        self.use_regex = use_regex
        self.image = image
        self.tag = tag
        self.script_friendly = script_friendly
        self.results = []  # List of (layer_digest, path, file_info_dict)

        if use_regex:
            self._compiled_pattern = re.compile(pattern)

    def fetch_callback(self, digest):
        """Always fetch all layers for searching."""
        # If we have a wrapped output, also check its callback
        if self._wrapped is not None:
            # We need the layer for searching, but the wrapped output
            # might not need it. We fetch it anyway for searching.
            # The wrapped output's callback is still consulted but
            # we always return True to ensure we get the data.
            pass
        return True

    def _matches(self, path):
        """Check if a path matches the search pattern."""
        if self.use_regex:
            return self._compiled_pattern.search(path) is not None
        else:
            # Match against full path or just the filename
            # This allows patterns like "*bash" to match "/bin/bash"
            filename = os.path.basename(path)
            return (fnmatch.fnmatch(path, self.pattern) or
                    fnmatch.fnmatch(filename, self.pattern))

    def _get_file_type(self, member):
        """Get a human-readable file type string."""
        if member.isfile():
            return 'file'
        elif member.isdir():
            return 'directory'
        elif member.issym():
            return 'symlink'
        elif member.islnk():
            return 'hardlink'
        elif member.isfifo():
            return 'fifo'
        elif member.ischr():
            return 'character device'
        elif member.isblk():
            return 'block device'
        else:
            return 'unknown'

    def _search_layer(self, name, data):
        """Search a layer for matching files."""
        LOG.info('Searching layer %s' % name)

        data.seek(0)
        try:
            with tarfile.open(fileobj=data, mode='r') as layer_tar:
                for member in layer_tar:
                    if self._matches(member.name):
                        file_info = {
                            'type': self._get_file_type(member),
                            'size': member.size,
                            'mode': member.mode,
                            'uid': member.uid,
                            'gid': member.gid,
                            'mtime': member.mtime,
                        }
                        if member.issym() or member.islnk():
                            file_info['linkname'] = member.linkname

                        self.results.append((name, member.name, file_info))
        except tarfile.TarError as e:
            LOG.error('Failed to read layer %s: %s' % (name, e))

    def process_image_element(self, element_type, name, data):
        """Process an image element, searching layers for matches."""
        # Search layers
        if element_type == constants.IMAGE_LAYER and data is not None:
            self._search_layer(name, data)

        # Pass through to wrapped output if present
        if self._wrapped is not None:
            if data is not None:
                data.seek(0)  # Reset for next consumer
            self._wrapped.process_image_element(element_type, name, data)

    def _print_results(self):
        """Print search results to stdout."""
        if not self.results:
            if not self.script_friendly:
                print('No matches found.')
            return

        if self.script_friendly:
            # Output format: image:tag:layer:path
            # One line per match, suitable for piping to other tools
            for layer_digest, path, file_info in self.results:
                print('%s:%s:%s:%s'
                      % (self.image, self.tag, layer_digest, path))
            return

        # Group results by layer
        results_by_layer = {}
        for layer_digest, path, file_info in self.results:
            if layer_digest not in results_by_layer:
                results_by_layer[layer_digest] = []
            results_by_layer[layer_digest].append((path, file_info))

        # Print results
        for layer_digest in results_by_layer:
            print('Layer: %s' % layer_digest)
            for path, file_info in results_by_layer[layer_digest]:
                if file_info['type'] in ('symlink', 'hardlink'):
                    print('  %s -> %s (%s)'
                          % (path, file_info['linkname'], file_info['type']))
                elif file_info['type'] == 'file':
                    print('  %s (%s, %d bytes)'
                          % (path, file_info['type'], file_info['size']))
                elif file_info['type'] == 'directory':
                    print('  %s (%s)' % (path, file_info['type']))
                else:
                    print('  %s (%s)' % (path, file_info['type']))
            print()

        layer_count = len(results_by_layer)
        match_count = len(self.results)
        print('Found %d match%s in %d layer%s.'
              % (match_count, '' if match_count == 1 else 'es',
                 layer_count, '' if layer_count == 1 else 's'))

    def finalize(self):
        """Print search results and finalize wrapped output."""
        self._print_results()

        if self._wrapped is not None:
            self._wrapped.finalize()
