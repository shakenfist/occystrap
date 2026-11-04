"""Regression tests for the config diff_id update reporting in the
filter base class (issue #188).

A filter recomputes the diff_id of every layer it processes. A layer
that is already in the filter's canonical form -- for example an
already-normalized base layer re-pushed via dockerpush -- comes out
byte-identical and keeps its diff_id. The 'N of M layers modified'
log must count the layers whose diff_id actually changed, not every
layer processed, or an idempotent re-push looks like a full rewrite.
"""
import io
import json
import os
import tarfile
import tempfile

import testtools

from occystrap import constants
from occystrap.filters.normalize_timestamps import TimestampNormalizer
from occystrap.outputs.base import ImageOutput


class _CapturingOutput(ImageOutput):
    """Minimal wrapped output that records the config it receives."""

    def __init__(self):
        super().__init__()
        self.config = None

    @property
    def requires_ordered_layers(self):
        return True

    def fetch_callback(self, digest):
        return True

    def process_image_element(self, element):
        if element.element_type == constants.CONFIG_FILE:
            element.data.seek(0)
            self.config = json.loads(element.data.read())

    def finalize(self):
        pass


def _raw_layer(name, data, mtime):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mtime = mtime
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    return buf.read()


class TestConfigDiffIdModifiedCount(testtools.TestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp()

    def _layer_element(self, diff_id_hex, data, index):
        return constants.ImageElement(
            constants.IMAGE_LAYER, diff_id_hex,
            io.BytesIO(data), layer_index=index)

    def test_only_changed_layers_counted(self):
        """An already-normalized layer is not counted as modified."""
        # Layer 0: already normalized. Normalizing it again is a
        # fixed point, so its diff_id does not change.
        pre = TimestampNormalizer(None, temp_dir=self.temp_dir)
        handle, sha0 = pre._normalize_layer(
            io.BytesIO(_raw_layer('etc/hosts', b'hello', 1700000000)))
        try:
            handle.seek(0)
            normalized0 = handle.read()
        finally:
            handle.close()
            os.unlink(handle.name)

        # Layer 1: a raw layer with a non-zero mtime, whose diff_id
        # the filter will change.
        raw1 = _raw_layer('etc/motd', b'world', 1700000500)
        import hashlib
        raw1_hex = hashlib.sha256(raw1).hexdigest()

        config_bytes = json.dumps({
            'architecture': 'amd64',
            'rootfs': {
                'type': 'layers',
                'diff_ids': ['sha256:%s' % sha0,
                             'sha256:%s' % raw1_hex],
            },
        }).encode('utf-8')

        out = _CapturingOutput()
        f = TimestampNormalizer(out, temp_dir=self.temp_dir)

        f.process_image_element(constants.ImageElement(
            constants.CONFIG_FILE, 'config.json',
            io.BytesIO(config_bytes)))
        f.process_image_element(
            self._layer_element(sha0, normalized0, 0))
        f.process_image_element(
            self._layer_element(raw1_hex, raw1, 1))

        with self.assertLogs(
                'occystrap.filters.base', level='INFO') as cm:
            f.finalize()

        joined = '\n'.join(cm.output)
        # Only layer 1 actually changed.
        self.assertIn('1 of 2 layers modified', joined)
        self.assertNotIn('2 of 2 layers modified', joined)

        # And the forwarded config proves it: layer 0 keeps its
        # diff_id, layer 1 gets a new one.
        self.assertEqual(
            'sha256:%s' % sha0,
            out.config['rootfs']['diff_ids'][0])
        self.assertNotEqual(
            'sha256:%s' % raw1_hex,
            out.config['rootfs']['diff_ids'][1])
