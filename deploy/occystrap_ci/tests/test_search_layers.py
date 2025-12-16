import io
import logging
import sys
import tempfile
import testtools

from occystrap.inputs import registry as input_registry
from occystrap.inputs import tarfile as input_tarfile
from occystrap.outputs import tarfile as output_tarfile
from occystrap.filters import SearchFilter


logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class SearchLayersTestCase(testtools.TestCase):
    def test_search_busybox_for_shells(self):
        """Test searching busybox image for shell binaries."""
        image = 'library/busybox'
        tag = 'latest'

        searcher = SearchFilter(None, '*sh')
        img = input_registry.Image(
            'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
        for image_element in img.fetch(fetch_callback=searcher.fetch_callback):
            searcher.process_image_element(*image_element)
        searcher.finalize()

        # busybox should have ash and sh
        paths = [path for _, path, _ in searcher.results]
        self.assertTrue(any('ash' in p for p in paths),
                        'Expected to find ash in busybox, got: %s' % paths)
        self.assertTrue(any('sh' in p for p in paths),
                        'Expected to find sh in busybox, got: %s' % paths)

    def test_search_no_matches(self):
        """Test searching for a pattern that doesn't exist."""
        image = 'library/busybox'
        tag = 'latest'

        searcher = SearchFilter(None, 'nonexistent_file_pattern_xyz123')
        img = input_registry.Image(
            'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
        for image_element in img.fetch(fetch_callback=searcher.fetch_callback):
            searcher.process_image_element(*image_element)
        searcher.finalize()

        self.assertEqual(0, len(searcher.results))

    def test_search_with_regex(self):
        """Test searching with regex pattern."""
        image = 'library/busybox'
        tag = 'latest'

        # Search for files ending in 'sh' in bin directory
        searcher = SearchFilter(None, r'bin/.*sh$', use_regex=True)
        img = input_registry.Image(
            'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
        for image_element in img.fetch(fetch_callback=searcher.fetch_callback):
            searcher.process_image_element(*image_element)
        searcher.finalize()

        # Should find ash and sh
        paths = [path for _, path, _ in searcher.results]
        self.assertTrue(len(paths) >= 2,
                        'Expected at least 2 matches, got: %s' % paths)


class SearchLayersScriptFriendlyTestCase(testtools.TestCase):
    def test_script_friendly_output_format(self):
        """Test that script-friendly mode produces correct output format."""
        image = 'library/busybox'
        tag = 'latest'

        searcher = SearchFilter(
            None, '*ash', image=image, tag=tag, script_friendly=True)
        img = input_registry.Image(
            'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
        for image_element in img.fetch(fetch_callback=searcher.fetch_callback):
            searcher.process_image_element(*image_element)

        # Capture stdout during finalize
        old_stdout = sys.stdout
        sys.stdout = captured_output = io.StringIO()
        searcher.finalize()
        sys.stdout = old_stdout

        output = captured_output.getvalue()
        lines = [line for line in output.strip().split('\n') if line]

        # Should have at least one match
        self.assertTrue(len(lines) >= 1,
                        'Expected at least one match, got: %s' % output)

        # Each line should be colon-separated with 4 parts
        for line in lines:
            parts = line.split(':')
            self.assertEqual(4, len(parts),
                             'Expected 4 colon-separated parts, got: %s' % line)
            self.assertEqual(image, parts[0])
            self.assertEqual(tag, parts[1])
            # parts[2] is the layer digest
            # parts[3] is the path
            self.assertTrue('ash' in parts[3],
                            'Expected ash in path, got: %s' % parts[3])

    def test_script_friendly_no_output_on_no_matches(self):
        """Test that script-friendly mode produces no output when no matches."""
        image = 'library/busybox'
        tag = 'latest'

        searcher = SearchFilter(
            None, 'nonexistent_xyz123', image=image, tag=tag,
            script_friendly=True)
        img = input_registry.Image(
            'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
        for image_element in img.fetch(fetch_callback=searcher.fetch_callback):
            searcher.process_image_element(*image_element)

        # Capture stdout during finalize
        old_stdout = sys.stdout
        sys.stdout = captured_output = io.StringIO()
        searcher.finalize()
        sys.stdout = old_stdout

        output = captured_output.getvalue()
        self.assertEqual('', output,
                         'Expected no output for no matches, got: %s' % output)


class SearchLayersTarfileTestCase(testtools.TestCase):
    def test_search_tarfile(self):
        """Test searching a local tarball for files."""
        image = 'library/busybox'
        tag = 'latest'

        # First, fetch the image to a tarball
        with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as tf:
            tarball_path = tf.name

        try:
            tar = output_tarfile.TarWriter(image, tag, tarball_path)
            img = input_registry.Image(
                'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
            for image_element in img.fetch(fetch_callback=tar.fetch_callback):
                tar.process_image_element(*image_element)
            tar.finalize()

            # Now search the tarball
            tarball_img = input_tarfile.Image(tarball_path)
            searcher = SearchFilter(None, '*sh')
            for image_element in tarball_img.fetch(
                    fetch_callback=searcher.fetch_callback):
                searcher.process_image_element(*image_element)
            searcher.finalize()

            # Should find shells
            paths = [path for _, path, _ in searcher.results]
            self.assertTrue(any('ash' in p for p in paths),
                            'Expected to find ash, got: %s' % paths)

        finally:
            import os
            if os.path.exists(tarball_path):
                os.unlink(tarball_path)

    def test_search_tarfile_script_friendly(self):
        """Test script-friendly output when searching a tarball."""
        image = 'library/busybox'
        tag = 'latest'

        # First, fetch the image to a tarball
        with tempfile.NamedTemporaryFile(delete=False, suffix='.tar') as tf:
            tarball_path = tf.name

        try:
            tar = output_tarfile.TarWriter(image, tag, tarball_path)
            img = input_registry.Image(
                'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
            for image_element in img.fetch(fetch_callback=tar.fetch_callback):
                tar.process_image_element(*image_element)
            tar.finalize()

            # Now search the tarball with script-friendly output
            tarball_img = input_tarfile.Image(tarball_path)
            searcher = SearchFilter(
                None, '*ash', image=tarball_img.image, tag=tarball_img.tag,
                script_friendly=True)
            for image_element in tarball_img.fetch(
                    fetch_callback=searcher.fetch_callback):
                searcher.process_image_element(*image_element)

            # Capture stdout during finalize
            old_stdout = sys.stdout
            sys.stdout = captured_output = io.StringIO()
            searcher.finalize()
            sys.stdout = old_stdout

            output = captured_output.getvalue()
            lines = [line for line in output.strip().split('\n') if line]

            # Should have matches with correct format
            self.assertTrue(len(lines) >= 1,
                            'Expected at least one match')
            for line in lines:
                parts = line.split(':')
                self.assertEqual(4, len(parts),
                                 'Expected 4 parts, got: %s' % line)

        finally:
            import os
            if os.path.exists(tarball_path):
                os.unlink(tarball_path)
