import io
import json
import os
import tarfile
import tempfile
import unittest

from occystrap import constants
from occystrap.filters import (
    ExcludeFilter, InspectFilter, TimestampNormalizer)
from occystrap.outputs import tarfile as output_tarfile


class InspectFilterTestCase(unittest.TestCase):
    """Test the InspectFilter in functional scenarios."""

    def _create_layer_with_files(self, files):
        """Create a layer tarball containing the specified files.

        Args:
            files: Dict of {path: content} pairs.
                Use None for directories.

        Returns:
            Path to the temporary layer file.
        """
        tf = tempfile.NamedTemporaryFile(delete=False)
        with tarfile.open(fileobj=tf, mode='w') as layer_tar:
            for path, content in files.items():
                ti = tarfile.TarInfo(path)
                if content is None:
                    ti.type = tarfile.DIRTYPE
                    ti.size = 0
                    layer_tar.addfile(ti)
                else:
                    data = (content.encode('utf-8')
                            if isinstance(content, str)
                            else content)
                    ti.size = len(data)
                    ti.mtime = 1700000000
                    layer_tar.addfile(ti, io.BytesIO(data))
        tf.flush()
        tf.close()
        return tf.name

    def _create_config_data(self, history_entries=None):
        """Create image config JSON with optional history.

        Args:
            history_entries: List of dicts with created,
                created_by, and optionally empty_layer keys.

        Returns:
            BytesIO containing the config JSON.
        """
        config = {
            'architecture': 'amd64',
            'os': 'linux',
            'history': history_entries or [],
        }
        return io.BytesIO(
            json.dumps(config).encode('utf-8'))

    def test_inspect_writes_jsonl(self):
        """Test that InspectFilter writes JSONL output."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
            'app/utils.py': 'def util(): pass',
        })

        output_jsonl = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        output_jsonl.close()

        try:
            inspect_filter = InspectFilter(
                None, output_file=output_jsonl.name,
                image='test/image', tag='v1')

            config_data = self._create_config_data([
                {
                    'created': '2024-01-01T00:00:00Z',
                    'created_by': '/bin/sh -c #(nop) ADD file:...',
                },
                {
                    'created': '2024-01-01T00:00:01Z',
                    'created_by': 'RUN /bin/sh -c apt-get update',
                },
            ])
            inspect_filter.process_image_element(
                constants.CONFIG_FILE, 'config.json',
                config_data)

            with open(layer_path, 'rb') as f:
                inspect_filter.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:abc123', f)

            with open(layer_path, 'rb') as f:
                inspect_filter.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:def456', f)

            inspect_filter.finalize()

            with open(output_jsonl.name, 'r') as f:
                lines = f.readlines()

            self.assertEqual(1, len(lines))

            record = json.loads(lines[0])
            self.assertEqual('test/image:v1', record['name'])
            self.assertEqual(2, len(record['layers']))

            # Layers should be in reverse order (newest first)
            top_layer = record['layers'][0]
            self.assertEqual(
                'sha256:def456', top_layer['Id'])
            self.assertEqual(
                ['test/image:v1'], top_layer['Tags'])

            second_layer = record['layers'][1]
            self.assertEqual(
                'sha256:abc123', second_layer['Id'])
            self.assertIsNone(second_layer['Tags'])

        finally:
            os.unlink(layer_path)
            if os.path.exists(output_jsonl.name):
                os.unlink(output_jsonl.name)

    def test_inspect_passthrough(self):
        """Test that InspectFilter passes data to wrapped output."""
        layer_path = self._create_layer_with_files({
            'app/main.py': 'print("hello")',
        })

        output_jsonl = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        output_jsonl.close()

        try:
            with tempfile.NamedTemporaryFile(
                    delete=False, suffix='.tar') as output_tf:
                try:
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    inspect_filter = InspectFilter(
                        tw, output_file=output_jsonl.name,
                        image='test/image', tag='latest')

                    config_data = self._create_config_data()
                    inspect_filter.process_image_element(
                        constants.CONFIG_FILE,
                        'config.json', config_data)

                    with open(layer_path, 'rb') as f:
                        inspect_filter.process_image_element(
                            constants.IMAGE_LAYER,
                            'sha256:abc123', f)

                    inspect_filter.finalize()

                    # Verify the tarwriter received the layer
                    self.assertEqual(
                        1, len(tw.tar_manifest[0]['Layers']))

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)
            if os.path.exists(output_jsonl.name):
                os.unlink(output_jsonl.name)

    def test_inspect_records_layer_sizes(self):
        """Test that layer sizes are accurately recorded."""
        small_layer = self._create_layer_with_files({
            'small.txt': 'hi',
        })
        large_layer = self._create_layer_with_files({
            'large.txt': 'x' * 10000,
        })

        output_jsonl = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        output_jsonl.close()

        try:
            inspect_filter = InspectFilter(
                None, output_file=output_jsonl.name,
                image='test/image', tag='v1')

            with open(small_layer, 'rb') as f:
                small_size = os.fstat(f.fileno()).st_size
                inspect_filter.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:small', f)

            with open(large_layer, 'rb') as f:
                large_size = os.fstat(f.fileno()).st_size
                inspect_filter.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:large', f)

            inspect_filter.finalize()

            with open(output_jsonl.name, 'r') as f:
                record = json.loads(f.readline())

            layers = record['layers']
            # Layers are reversed (newest first)
            self.assertEqual(small_size, layers[1]['Size'])
            self.assertEqual(large_size, layers[0]['Size'])

        finally:
            os.unlink(small_layer)
            os.unlink(large_layer)
            if os.path.exists(output_jsonl.name):
                os.unlink(output_jsonl.name)

    def test_inspect_append_mode(self):
        """Test that multiple images append to the same file."""
        layer_path = self._create_layer_with_files({
            'file.txt': 'content',
        })

        output_jsonl = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        output_jsonl.close()

        try:
            # First image
            filter1 = InspectFilter(
                None, output_file=output_jsonl.name,
                image='image/one', tag='v1')
            with open(layer_path, 'rb') as f:
                filter1.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:aaa', f)
            filter1.finalize()

            # Second image
            filter2 = InspectFilter(
                None, output_file=output_jsonl.name,
                image='image/two', tag='v2')
            with open(layer_path, 'rb') as f:
                filter2.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:bbb', f)
            filter2.finalize()

            with open(output_jsonl.name, 'r') as f:
                lines = f.readlines()

            self.assertEqual(2, len(lines))

            record1 = json.loads(lines[0])
            record2 = json.loads(lines[1])
            self.assertEqual('image/one:v1', record1['name'])
            self.assertEqual('image/two:v2', record2['name'])

        finally:
            os.unlink(layer_path)
            if os.path.exists(output_jsonl.name):
                os.unlink(output_jsonl.name)

    def test_inspect_between_filters(self):
        """Test placing inspect between normalize and exclude."""
        # Files must be large enough that removal changes the
        # tarball size past the 10240-byte tar block minimum
        layer_path = self._create_layer_with_files({
            'app/main.py': 'x' * 20000,
            'app/__pycache__/main.cpython-311.pyc':
                b'\x00' * 20000,
            'config.json': '{"key": "value"}',
        })

        jsonl_before = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_before.close()
        jsonl_after = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_after.close()

        try:
            with tempfile.NamedTemporaryFile(
                    delete=False, suffix='.tar') as output_tf:
                try:
                    # Pipeline: inspect -> exclude -> inspect -> tar
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    inspect_after = InspectFilter(
                        tw, output_file=jsonl_after.name,
                        image='test/image', tag='latest')
                    exclude_filter = ExcludeFilter(
                        inspect_after,
                        patterns=['*__pycache__*'])
                    inspect_before = InspectFilter(
                        exclude_filter,
                        output_file=jsonl_before.name,
                        image='test/image', tag='latest')

                    # Process config
                    config_data = self._create_config_data()
                    inspect_before.process_image_element(
                        constants.CONFIG_FILE,
                        'config.json', config_data)

                    # Process layer
                    with open(layer_path, 'rb') as f:
                        inspect_before.process_image_element(
                            constants.IMAGE_LAYER,
                            'sha256:original', f)

                    inspect_before.finalize()

                    # Both files should have output
                    with open(jsonl_before.name, 'r') as f:
                        before = json.loads(f.readline())
                    with open(jsonl_after.name, 'r') as f:
                        after = json.loads(f.readline())

                    # Both should have the same image name
                    self.assertEqual(
                        'test/image:latest', before['name'])
                    self.assertEqual(
                        'test/image:latest', after['name'])

                    # Before-exclude should have original digest
                    self.assertEqual(
                        'sha256:original',
                        before['layers'][0]['Id'])

                    # After-exclude should have different digest
                    # (exclude recalculates SHA)
                    self.assertNotEqual(
                        'sha256:original',
                        after['layers'][0]['Id'])

                    # Both should record one layer
                    self.assertEqual(
                        1, len(before['layers']))
                    self.assertEqual(
                        1, len(after['layers']))

                    # After-exclude layer should be smaller
                    # (pycache removed)
                    self.assertLess(
                        after['layers'][0]['Size'],
                        before['layers'][0]['Size'])

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)
            if os.path.exists(jsonl_before.name):
                os.unlink(jsonl_before.name)
            if os.path.exists(jsonl_after.name):
                os.unlink(jsonl_after.name)

    def test_inspect_with_normalize_timestamps(self):
        """Test inspect filter captures post-normalize state."""
        layer_path = self._create_layer_with_files({
            'file.txt': 'content',
        })

        jsonl_before = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_before.close()
        jsonl_after = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_after.close()

        try:
            with tempfile.NamedTemporaryFile(
                    delete=False, suffix='.tar') as output_tf:
                try:
                    # Pipeline:
                    # inspect -> normalize -> inspect -> tar
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    inspect_after = InspectFilter(
                        tw, output_file=jsonl_after.name,
                        image='test/image', tag='latest')
                    normalizer = TimestampNormalizer(
                        inspect_after, timestamp=0)
                    inspect_before = InspectFilter(
                        normalizer,
                        output_file=jsonl_before.name,
                        image='test/image', tag='latest')

                    with open(layer_path, 'rb') as f:
                        inspect_before.process_image_element(
                            constants.IMAGE_LAYER,
                            'sha256:original', f)

                    inspect_before.finalize()

                    with open(jsonl_before.name, 'r') as f:
                        before = json.loads(f.readline())
                    with open(jsonl_after.name, 'r') as f:
                        after = json.loads(f.readline())

                    # Before should have original digest
                    self.assertEqual(
                        'sha256:original',
                        before['layers'][0]['Id'])

                    # After should have different digest
                    # (timestamps changed)
                    self.assertNotEqual(
                        'sha256:original',
                        after['layers'][0]['Id'])

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)
            if os.path.exists(jsonl_before.name):
                os.unlink(jsonl_before.name)
            if os.path.exists(jsonl_after.name):
                os.unlink(jsonl_after.name)

    def test_inspect_history_correlation(self):
        """Test that history entries correlate with layers."""
        layer_path = self._create_layer_with_files({
            'file.txt': 'content',
        })

        output_jsonl = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        output_jsonl.close()

        try:
            inspect_filter = InspectFilter(
                None, output_file=output_jsonl.name,
                image='test/image', tag='v1')

            config_data = self._create_config_data([
                {
                    'created': '2024-06-15T12:00:00Z',
                    'created_by': 'RUN /bin/sh -c apt-get install',
                    'comment': 'install deps',
                },
                {
                    'created_by': 'ENV PATH=/usr/local/bin',
                    'empty_layer': True,
                },
                {
                    'created': '2024-06-15T12:01:00Z',
                    'created_by': 'COPY . /app',
                },
            ])
            inspect_filter.process_image_element(
                constants.CONFIG_FILE, 'config.json',
                config_data)

            # Two real layers (the ENV is empty_layer)
            with open(layer_path, 'rb') as f:
                inspect_filter.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:layer1', f)

            with open(layer_path, 'rb') as f:
                inspect_filter.process_image_element(
                    constants.IMAGE_LAYER,
                    'sha256:layer2', f)

            inspect_filter.finalize()

            with open(output_jsonl.name, 'r') as f:
                record = json.loads(f.readline())

            layers = record['layers']
            self.assertEqual(2, len(layers))

            # Newest first (layer2 is topmost)
            self.assertEqual(
                'COPY . /app', layers[0]['CreatedBy'])
            self.assertEqual(
                'RUN /bin/sh -c apt-get install',
                layers[1]['CreatedBy'])
            self.assertEqual(
                'install deps', layers[1]['Comment'])

        finally:
            os.unlink(layer_path)
            if os.path.exists(output_jsonl.name):
                os.unlink(output_jsonl.name)

    def test_inspect_full_pipeline(self):
        """Test inspect at three stages of a full pipeline."""
        # Files must be large enough that removal changes the
        # tarball size past the 10240-byte tar block minimum
        layer_path = self._create_layer_with_files({
            'app/main.py': 'x' * 20000,
            '.git/config': 'y' * 20000,
        })

        jsonl_built = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_built.close()
        jsonl_normalized = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_normalized.close()
        jsonl_excluded = tempfile.NamedTemporaryFile(
            delete=False, suffix='.jsonl')
        jsonl_excluded.close()

        try:
            with tempfile.NamedTemporaryFile(
                    delete=False, suffix='.tar') as output_tf:
                try:
                    # Pipeline mirrors build-containers.sh:
                    # inspect -> normalize -> inspect ->
                    # exclude -> inspect -> tar
                    tw = output_tarfile.TarWriter(
                        'test/image', 'latest', output_tf.name)
                    inspect_excluded = InspectFilter(
                        tw,
                        output_file=jsonl_excluded.name,
                        image='test/image', tag='latest')
                    exclude_filter = ExcludeFilter(
                        inspect_excluded,
                        patterns=['.git/*'])
                    inspect_normalized = InspectFilter(
                        exclude_filter,
                        output_file=jsonl_normalized.name,
                        image='test/image', tag='latest')
                    normalizer = TimestampNormalizer(
                        inspect_normalized, timestamp=0)
                    inspect_built = InspectFilter(
                        normalizer,
                        output_file=jsonl_built.name,
                        image='test/image', tag='latest')

                    with open(layer_path, 'rb') as f:
                        inspect_built.process_image_element(
                            constants.IMAGE_LAYER,
                            'sha256:original', f)

                    inspect_built.finalize()

                    # Read all three stages
                    with open(jsonl_built.name, 'r') as f:
                        built = json.loads(f.readline())
                    with open(
                            jsonl_normalized.name, 'r') as f:
                        normalized = json.loads(f.readline())
                    with open(
                            jsonl_excluded.name, 'r') as f:
                        excluded = json.loads(f.readline())

                    # All should be for the same image
                    self.assertEqual(
                        'test/image:latest', built['name'])
                    self.assertEqual(
                        'test/image:latest',
                        normalized['name'])
                    self.assertEqual(
                        'test/image:latest',
                        excluded['name'])

                    # Each stage should have one layer
                    self.assertEqual(
                        1, len(built['layers']))
                    self.assertEqual(
                        1, len(normalized['layers']))
                    self.assertEqual(
                        1, len(excluded['layers']))

                    # As-built should have original digest
                    self.assertEqual(
                        'sha256:original',
                        built['layers'][0]['Id'])

                    # Post-normalize digest differs from
                    # original (timestamps changed)
                    self.assertNotEqual(
                        built['layers'][0]['Id'],
                        normalized['layers'][0]['Id'])

                    # Post-exclude digest differs from
                    # normalized (.git removed)
                    self.assertNotEqual(
                        normalized['layers'][0]['Id'],
                        excluded['layers'][0]['Id'])

                    # Size should decrease at each stage
                    # (normalize may change size due to tar
                    # rewrite, exclude definitely shrinks)
                    self.assertGreater(
                        normalized['layers'][0]['Size'],
                        excluded['layers'][0]['Size'])

                finally:
                    if os.path.exists(output_tf.name):
                        os.unlink(output_tf.name)

        finally:
            os.unlink(layer_path)
            for path in [jsonl_built.name,
                         jsonl_normalized.name,
                         jsonl_excluded.name]:
                if os.path.exists(path):
                    os.unlink(path)
