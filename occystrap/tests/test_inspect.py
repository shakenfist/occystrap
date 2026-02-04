"""Tests for the inspect filter."""

import io
import json
import os
import tarfile
import tempfile
import unittest

from occystrap import constants
from occystrap.filters.inspect import InspectFilter


class TestInspectFilter(unittest.TestCase):
    """Tests for the InspectFilter class."""

    def setUp(self):
        self.output_fd, self.output_file = tempfile.mkstemp(
            suffix='.jsonl')
        os.close(self.output_fd)
        # Start with an empty file
        with open(self.output_file, 'w') as f:
            f.truncate(0)

    def tearDown(self):
        if os.path.exists(self.output_file):
            os.unlink(self.output_file)

    def _make_config(self, history_entries):
        """Create a config JSON file-like object.

        Args:
            history_entries: List of dicts, each with optional
                keys: created, created_by, comment, empty_layer.
        """
        config = {
            'history': history_entries,
            'rootfs': {
                'type': 'layers',
                'diff_ids': [],
            },
        }
        data = json.dumps(config).encode('utf-8')
        return io.BytesIO(data)

    def _make_layer(self, files=None):
        """Create a layer tarball file-like object.

        Args:
            files: List of (name, content) tuples. Defaults to
                a single file.
        """
        if files is None:
            files = [('file.txt', b'hello')]

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            for name, content in files:
                ti = tarfile.TarInfo(name=name)
                ti.size = len(content)
                tar.addfile(ti, io.BytesIO(content))
        buf.seek(0)
        return buf

    def test_basic_output(self):
        """Test that inspect produces valid JSONL output."""
        f = InspectFilter(
            None, self.output_file,
            image='myimage', tag='v1')

        config = self._make_config([
            {
                'created': '2025-01-15T10:00:00Z',
                'created_by': '/bin/sh -c echo hello',
                'comment': '',
            },
        ])
        layer = self._make_layer()

        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'abc123', layer)
        f.finalize()

        with open(self.output_file) as fh:
            lines = fh.readlines()

        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record['name'], 'myimage:v1')
        self.assertEqual(len(record['layers']), 1)

        layer_entry = record['layers'][0]
        self.assertEqual(
            layer_entry['Id'], 'sha256:abc123')
        self.assertGreater(layer_entry['Size'], 0)
        self.assertEqual(
            layer_entry['CreatedBy'],
            '/bin/sh -c echo hello')
        self.assertEqual(layer_entry['Tags'], ['myimage:v1'])

    def test_empty_layer_history_skipped(self):
        """Test that empty_layer history entries are skipped."""
        f = InspectFilter(
            None, self.output_file,
            image='img', tag='latest')

        config = self._make_config([
            {
                'created': '2025-01-15T10:00:00Z',
                'created_by': '/bin/sh -c apt-get install',
            },
            {
                'created': '2025-01-15T10:01:00Z',
                'created_by': '/bin/sh -c #(nop) ENV FOO=bar',
                'empty_layer': True,
            },
            {
                'created': '2025-01-15T10:02:00Z',
                'created_by': '/bin/sh -c echo done',
            },
        ])
        layer1 = self._make_layer(
            [('pkg.deb', b'x' * 100)])
        layer2 = self._make_layer(
            [('done.txt', b'done')])

        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'layer1hash', layer1)
        f.process_image_element(
            constants.IMAGE_LAYER, 'layer2hash', layer2)
        f.finalize()

        with open(self.output_file) as fh:
            record = json.loads(fh.readline())

        # Should have 2 layers, not 3
        self.assertEqual(len(record['layers']), 2)

        # Layers are reversed (newest first)
        self.assertEqual(
            record['layers'][0]['CreatedBy'],
            '/bin/sh -c echo done')
        self.assertEqual(
            record['layers'][1]['CreatedBy'],
            '/bin/sh -c apt-get install')

    def test_digest_normalization(self):
        """Test that digests get sha256: prefix."""
        f = InspectFilter(
            None, self.output_file,
            image='img', tag='v1')

        config = self._make_config([
            {'created_by': 'step1'},
            {'created_by': 'step2'},
        ])
        layer1 = self._make_layer()
        layer2 = self._make_layer()

        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'abc123', layer1)
        f.process_image_element(
            constants.IMAGE_LAYER,
            'sha256:def456', layer2)
        f.finalize()

        with open(self.output_file) as fh:
            record = json.loads(fh.readline())

        # Reversed order
        self.assertEqual(
            record['layers'][0]['Id'], 'sha256:def456')
        self.assertEqual(
            record['layers'][1]['Id'], 'sha256:abc123')

    def test_append_mode(self):
        """Test that multiple invocations append to the file."""
        for i in range(3):
            f = InspectFilter(
                None, self.output_file,
                image='img%d' % i, tag='v1')
            config = self._make_config(
                [{'created_by': 'step'}])
            layer = self._make_layer()
            f.process_image_element(
                constants.CONFIG_FILE, 'cfg', config)
            f.process_image_element(
                constants.IMAGE_LAYER, 'hash%d' % i, layer)
            f.finalize()

        with open(self.output_file) as fh:
            lines = fh.readlines()

        self.assertEqual(len(lines), 3)
        for i, line in enumerate(lines):
            record = json.loads(line)
            self.assertEqual(
                record['name'], 'img%d:v1' % i)

    def test_passthrough_mode(self):
        """Test that elements are passed to wrapped output."""
        received = []

        class MockOutput:
            def fetch_callback(self, digest):
                return True

            def process_image_element(self, et, name, data):
                received.append((et, name))

            def finalize(self):
                pass

        mock = MockOutput()
        f = InspectFilter(
            mock, self.output_file,
            image='img', tag='v1')

        config = self._make_config(
            [{'created_by': 'step'}])
        layer = self._make_layer()

        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'abc123', layer)
        f.finalize()

        self.assertEqual(len(received), 2)
        self.assertEqual(
            received[0], (constants.CONFIG_FILE, 'config.json'))
        self.assertEqual(
            received[1], (constants.IMAGE_LAYER, 'abc123'))

        # Output file should also have been written
        with open(self.output_file) as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record['name'], 'img:v1')

    def test_no_config(self):
        """Test graceful handling when no config is provided."""
        f = InspectFilter(
            None, self.output_file,
            image='img', tag='v1')

        layer = self._make_layer()
        f.process_image_element(
            constants.IMAGE_LAYER, 'abc123', layer)
        f.finalize()

        with open(self.output_file) as fh:
            record = json.loads(fh.readline())

        self.assertEqual(len(record['layers']), 1)
        # No history, so CreatedBy should be empty
        self.assertEqual(
            record['layers'][0]['CreatedBy'], '')

    def test_skipped_layer(self):
        """Test handling of layers with data=None."""
        f = InspectFilter(
            None, self.output_file,
            image='img', tag='v1')

        config = self._make_config(
            [{'created_by': 'step'}])
        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'abc123', None)
        f.finalize()

        with open(self.output_file) as fh:
            record = json.loads(fh.readline())

        self.assertEqual(len(record['layers']), 1)
        self.assertEqual(record['layers'][0]['Size'], 0)

    def test_tags_on_topmost_layer_only(self):
        """Test that Tags is set only on the topmost layer."""
        f = InspectFilter(
            None, self.output_file,
            image='myimg', tag='latest')

        config = self._make_config([
            {'created_by': 'base'},
            {'created_by': 'app'},
        ])
        layer1 = self._make_layer()
        layer2 = self._make_layer()

        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'base', layer1)
        f.process_image_element(
            constants.IMAGE_LAYER, 'app', layer2)
        f.finalize()

        with open(self.output_file) as fh:
            record = json.loads(fh.readline())

        # Reversed: app is first (topmost), base is second
        self.assertEqual(
            record['layers'][0]['Tags'],
            ['myimg:latest'])
        self.assertIsNone(record['layers'][1]['Tags'])

    def test_created_timestamp_parsing(self):
        """Test various timestamp formats in config history."""
        f = InspectFilter(
            None, self.output_file,
            image='img', tag='v1')

        config = self._make_config([
            {
                'created': '2025-06-15T12:30:45Z',
                'created_by': 'iso-utc',
            },
            {
                'created': '2025-06-15T12:30:45+00:00',
                'created_by': 'iso-offset',
            },
        ])
        layer1 = self._make_layer()
        layer2 = self._make_layer()

        f.process_image_element(
            constants.CONFIG_FILE, 'config.json', config)
        f.process_image_element(
            constants.IMAGE_LAYER, 'l1', layer1)
        f.process_image_element(
            constants.IMAGE_LAYER, 'l2', layer2)
        f.finalize()

        with open(self.output_file) as fh:
            record = json.loads(fh.readline())

        # Both should parse to the same timestamp
        ts1 = record['layers'][1]['Created']  # reversed
        ts2 = record['layers'][0]['Created']
        self.assertIsInstance(ts1, int)
        self.assertIsInstance(ts2, int)
        self.assertEqual(ts1, ts2)
        self.assertGreater(ts1, 0)


if __name__ == '__main__':
    unittest.main()
