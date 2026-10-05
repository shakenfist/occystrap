"""Tests that layer-rewriting filters preserve PAX extended records."""

import io
import os
import tarfile
import tempfile

import testtools

from occystrap.filters.exclude import ExcludeFilter
from occystrap.filters.normalize_timestamps import TimestampNormalizer
from occystrap.tests.pax_fixtures import (
    CAPABILITY,
    CAPABILITY_KEY,
    capability_header,
    read_capability,
)


def make_layer():
    """Build a PAX layer with a capability-bearing binary and a cache file.

    Returns:
        BytesIO containing the layer.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w',
                      format=tarfile.PAX_FORMAT) as tar:
        content = b'#!/bin/true\n'
        ti = tarfile.TarInfo('usr/bin/nsenter')
        ti.size = len(content)
        ti.mode = 0o755
        ti.mtime = 1700000000.5
        ti.pax_headers = {
            CAPABILITY_KEY: capability_header(),
            'atime': '1700000001.5',
            'ctime': '1700000002.5',
            'LIBARCHIVE.creationtime': '1700000003',
        }
        tar.addfile(ti, io.BytesIO(content))

        content = b'cached'
        ti = tarfile.TarInfo('var/cache/junk')
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))
    buf.seek(0)
    return buf


def read_members(fileobj):
    """Read a rewritten layer back and close it."""
    try:
        with tarfile.open(fileobj=fileobj, mode='r') as tar:
            return {m.name: m for m in tar.getmembers()}
    finally:
        fileobj.close()
        os.unlink(fileobj.name)


class TestTimestampNormalizerPax(testtools.TestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, self.temp_dir)

    def test_capability_preserved(self):
        """normalize-timestamps keeps security.capability xattrs."""
        f = TimestampNormalizer(None, temp_dir=self.temp_dir)
        rewritten, _ = f._normalize_layer(make_layer())
        members = read_members(rewritten)
        self.assertEqual(
            read_capability(members['usr/bin/nsenter']), CAPABILITY)

    def test_all_timestamps_normalized(self):
        """No timestamp record survives normalization."""
        f = TimestampNormalizer(None, timestamp=42, temp_dir=self.temp_dir)
        rewritten, _ = f._normalize_layer(make_layer())
        member = read_members(rewritten)['usr/bin/nsenter']
        self.assertEqual(member.mtime, 42)
        for record in ('mtime', 'atime', 'ctime', 'LIBARCHIVE.creationtime'):
            self.assertNotIn(record, member.pax_headers)

    def test_reproducible(self):
        """The same layer normalizes to the same digest."""
        f = TimestampNormalizer(None, temp_dir=self.temp_dir)
        first, first_sha = f._normalize_layer(make_layer())
        read_members(first)
        second, second_sha = f._normalize_layer(make_layer())
        read_members(second)
        self.assertEqual(first_sha, second_sha)


class TestExcludeFilterPax(testtools.TestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, self.temp_dir)

    def test_capability_preserved(self):
        """exclude keeps security.capability xattrs on kept members."""
        f = ExcludeFilter(None, ['var/cache/*'], temp_dir=self.temp_dir)
        rewritten, _ = f._filter_layer(make_layer())
        members = read_members(rewritten)
        self.assertEqual(sorted(members), ['usr/bin/nsenter'])
        member = members['usr/bin/nsenter']
        self.assertEqual(read_capability(member), CAPABILITY)

        # exclude does not change timestamps, so keeps them all
        self.assertEqual(member.mtime, 1700000000.5)
        self.assertEqual(member.pax_headers['atime'], '1700000001.5')
        self.assertEqual(member.pax_headers['ctime'], '1700000002.5')
