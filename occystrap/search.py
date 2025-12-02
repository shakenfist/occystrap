import fnmatch
import logging
import os
import re
import tarfile

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class LayerSearcher(object):
    def __init__(self, pattern, use_regex=False, image=None, tag=None,
                 script_friendly=False):
        self.pattern = pattern
        self.use_regex = use_regex
        self.image = image
        self.tag = tag
        self.script_friendly = script_friendly
        self.results = []  # List of (layer_digest, path, file_info_dict)

        if use_regex:
            self._compiled_pattern = re.compile(pattern)

    def fetch_callback(self, digest):
        return True

    def _matches(self, path):
        if self.use_regex:
            return self._compiled_pattern.search(path) is not None
        else:
            # Match against full path or just the filename
            # This allows patterns like "*bash" to match "/bin/bash"
            filename = os.path.basename(path)
            return (fnmatch.fnmatch(path, self.pattern) or
                    fnmatch.fnmatch(filename, self.pattern))

    def _get_file_type(self, member):
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

    def process_image_element(self, element_type, name, data):
        if element_type != constants.IMAGE_LAYER:
            return

        if data is None:
            LOG.warning('Layer %s has no data (skipped by fetch_callback)'
                        % name)
            return

        LOG.info('Searching layer %s' % name)

        # Open the layer tarball and search for matching paths
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

    def finalize(self):
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
