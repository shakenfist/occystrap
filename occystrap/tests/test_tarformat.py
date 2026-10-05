"""Tests for the tarformat module."""

import io
import tarfile
import unittest

from occystrap.tarformat import (
    add_member,
    needs_pax_format,
    prepare_member_for_rewrite,
)
from occystrap.tests.pax_fixtures import (
    CAPABILITY,
    CAPABILITY_KEY,
    capability_header,
    read_capability,
)

# The largest value an 8 byte USTAR octal field (uid, gid) can hold
USTAR_MAX_ID = 0o7777777


class TestNeedsPaxFormat(unittest.TestCase):
    """Tests for the needs_pax_format function."""

    def _make_member(self, name, **kwargs):
        """Create a TarInfo with given attributes."""
        ti = tarfile.TarInfo(name=name)
        ti.size = kwargs.get('size', 0)
        ti.uid = kwargs.get('uid', 0)
        ti.gid = kwargs.get('gid', 0)
        ti.linkname = kwargs.get('linkname', '')
        return ti

    def test_short_path_uses_ustar(self):
        """Short paths should not require PAX."""
        member = self._make_member('short/path/file.txt')
        self.assertFalse(needs_pax_format(member))

    def test_path_at_ustar_limit_uses_ustar(self):
        """Paths exactly at USTAR limit should not require PAX."""
        # 100 char basename + 155 char dirname + '/' = 256
        dirname = 'a' * 155
        basename = 'b' * 96 + '.txt'  # 100 chars (96 + 4 for .txt)
        path = dirname + '/' + basename
        self.assertEqual(len(path), 256)
        member = self._make_member(path)
        self.assertFalse(needs_pax_format(member))

    def test_path_over_limit_requires_pax(self):
        """Paths over 256 chars should require PAX."""
        path = 'a' * 257
        member = self._make_member(path)
        self.assertTrue(needs_pax_format(member))

    def test_long_basename_requires_pax(self):
        """Basenames over 100 chars should require PAX."""
        basename = 'x' * 101
        path = 'dir/' + basename
        member = self._make_member(path)
        self.assertTrue(needs_pax_format(member))

    def test_long_dirname_requires_pax(self):
        """Dirnames over 155 chars should require PAX."""
        dirname = 'a' * 156
        path = dirname + '/file.txt'
        member = self._make_member(path)
        self.assertTrue(needs_pax_format(member))

    def test_long_linkname_requires_pax(self):
        """Link targets over 100 chars should require PAX."""
        member = self._make_member('mylink', linkname='x' * 101)
        self.assertTrue(needs_pax_format(member))

    def test_linkname_at_limit_uses_ustar(self):
        """Link targets at exactly 100 chars should use USTAR."""
        member = self._make_member('mylink', linkname='x' * 100)
        self.assertFalse(needs_pax_format(member))

    def test_large_uid_requires_pax(self):
        """UID over 2097151 should require PAX."""
        member = self._make_member('file.txt', uid=USTAR_MAX_ID + 1)
        self.assertTrue(needs_pax_format(member))

    def test_large_gid_requires_pax(self):
        """GID over 2097151 should require PAX."""
        member = self._make_member('file.txt', gid=USTAR_MAX_ID + 1)
        self.assertTrue(needs_pax_format(member))

    def test_uid_at_limit_uses_ustar(self):
        """UID at exactly 2097151 should use USTAR."""
        member = self._make_member('file.txt', uid=USTAR_MAX_ID)
        self.assertFalse(needs_pax_format(member))

    def test_non_ascii_path_requires_pax(self):
        """Non-ASCII characters in path should require PAX."""
        member = self._make_member('Főtanúsítvány.pem')
        self.assertTrue(needs_pax_format(member))

    def test_non_ascii_linkname_requires_pax(self):
        """Non-ASCII characters in linkname should require PAX."""
        member = self._make_member('mylink', linkname='célpont.txt')
        self.assertTrue(needs_pax_format(member))

    def test_ascii_path_uses_ustar(self):
        """ASCII-only paths should use USTAR."""
        member = self._make_member('normal/ascii/path.txt')
        self.assertFalse(needs_pax_format(member))


class TestNeedsPaxFormatExtended(unittest.TestCase):
    """Tests for needs_pax_format checks beyond names and sizes."""

    def test_xattr_requires_pax(self):
        """Members with xattr records need PAX, USTAR would drop them."""
        member = tarfile.TarInfo('bin/ping')
        member.pax_headers = {CAPABILITY_KEY: capability_header()}
        self.assertTrue(needs_pax_format(member))

    def test_any_extended_record_requires_pax(self):
        """Any remaining extended record needs PAX."""
        member = tarfile.TarInfo('file')
        member.pax_headers = {'atime': '1700000000.5'}
        self.assertTrue(needs_pax_format(member))

    def test_fractional_mtime_requires_pax(self):
        """Sub-second mtimes cannot be stored in USTAR."""
        member = tarfile.TarInfo('file')
        member.mtime = 1700000000.25
        self.assertTrue(needs_pax_format(member))

    def test_whole_float_mtime_uses_ustar(self):
        """A float mtime with no fractional part fits USTAR."""
        member = tarfile.TarInfo('file')
        member.mtime = 1700000000.0
        self.assertFalse(needs_pax_format(member))

    def test_directory_at_name_limit_requires_pax(self):
        """tarfile adds a '/' to directory names, which can overflow USTAR."""
        for name in ('c' * 99 + '/' + 'd' * 100,
                     'a' * 155 + '/' + 'b' * 100):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            self.assertTrue(needs_pax_format(member), name)

    def test_file_at_name_limit_uses_ustar(self):
        """The same names as regular files still fit USTAR."""
        for name in ('c' * 99 + '/' + 'd' * 100,
                     'a' * 155 + '/' + 'b' * 100):
            self.assertFalse(needs_pax_format(tarfile.TarInfo(name)), name)

    def test_out_of_range_mtime_requires_pax(self):
        """Negative or very large mtimes overflow the USTAR field."""
        for mtime in (-1, 8 ** 11):
            member = tarfile.TarInfo('file')
            member.mtime = mtime
            self.assertTrue(needs_pax_format(member), mtime)

    def test_long_uname_requires_pax(self):
        """Owner names over 32 chars should require PAX."""
        member = tarfile.TarInfo('file')
        member.uname = 'u' * 33
        self.assertTrue(needs_pax_format(member))

    def test_non_ascii_gname_requires_pax(self):
        """Non-ASCII group names should require PAX."""
        member = tarfile.TarInfo('file')
        member.gname = 'grüppe'
        self.assertTrue(needs_pax_format(member))


class TestPrepareMemberForRewrite(unittest.TestCase):
    """Tests for prepare_member_for_rewrite function."""

    def test_keeps_metadata_records(self):
        """xattrs, ACLs and times without a TarInfo field are kept."""
        member = tarfile.TarInfo('file')
        member.pax_headers = {
            CAPABILITY_KEY: capability_header(),
            'SCHILY.xattr.security.selinux': 'system_u:object_r:bin_t:s0',
            'SCHILY.acl.access': 'user::rw-',
            'atime': '1700000000.5',
            'ctime': '1700000000.5',
        }
        prepare_member_for_rewrite(member)
        self.assertEqual(
            sorted(member.pax_headers),
            sorted([CAPABILITY_KEY, 'SCHILY.xattr.security.selinux',
                    'SCHILY.acl.access', 'atime', 'ctime']))

    def test_drops_field_records(self):
        """Records mirroring TarInfo fields are dropped."""
        member = tarfile.TarInfo('file')
        member.pax_headers = {
            'path': 'file', 'linkpath': 'other', 'size': '10',
            'uid': '1', 'gid': '1', 'uname': 'u', 'gname': 'g',
            'mtime': '1700000000.5',
        }
        prepare_member_for_rewrite(member)
        self.assertEqual(member.pax_headers, {})

    def test_drops_encoding_records(self):
        """Records describing the source archive's encoding are dropped."""
        member = tarfile.TarInfo('file')
        member.pax_headers = {
            'hdrcharset': 'BINARY',
            'GNU.sparse.major': '1',
            'GNU.sparse.minor': '0',
            'GNU.sparse.name': 'file',
            'GNU.sparse.realsize': '1048579',
        }
        prepare_member_for_rewrite(member)
        self.assertEqual(member.pax_headers, {})


class TestAddMember(unittest.TestCase):
    """Tests for add_member function."""

    def _rewrite(self, members):
        """Write members to a PAX archive, then rewrite it with add_member.

        Args:
            members: List of (TarInfo, content bytes) tuples.

        Returns:
            Tuple of (raw rewritten bytes, list of rewritten TarInfos).
        """
        src = io.BytesIO()
        with tarfile.open(fileobj=src, mode='w',
                          format=tarfile.PAX_FORMAT) as tar:
            for member, content in members:
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
        src.seek(0)

        dst = io.BytesIO()
        with tarfile.open(fileobj=dst, mode='w') as out:
            with tarfile.open(fileobj=src, mode='r') as tar:
                for member in tar:
                    add_member(out, member, tar.extractfile(member))

        dst.seek(0)
        with tarfile.open(fileobj=dst, mode='r') as tar:
            rewritten = tar.getmembers()
        return dst.getvalue(), rewritten

    def test_capability_preserved(self):
        """security.capability xattrs survive a rewrite byte for byte."""
        member = tarfile.TarInfo('usr/bin/nsenter')
        member.mode = 0o755
        member.pax_headers = {CAPABILITY_KEY: capability_header()}
        _, rewritten = self._rewrite([(member, b'binary')])
        self.assertEqual(read_capability(rewritten[0]), CAPABILITY)

    def test_long_names_stay_ustar_beside_xattrs(self):
        """A capability on one member does not force PAX on the others."""
        cap = tarfile.TarInfo('bin/ping')
        cap.pax_headers = {CAPABILITY_KEY: capability_header()}
        members = [(cap, b'ping')]
        for i in range(10):
            name = 'd' * 150 + '/' + 'f%03d' % i + 'x' * 90
            members.append((tarfile.TarInfo(name), b'content'))
        raw, rewritten = self._rewrite(members)

        # Only the capability member gets an extended header
        self.assertEqual(raw.count(b'SCHILY.xattr'), 1)
        self.assertEqual(raw.count(b' path='), 0)
        self.assertEqual(read_capability(rewritten[0]), CAPABILITY)
        self.assertEqual(len(rewritten), 11)
        self.assertEqual(rewritten[5].name, members[5][0].name)

    def test_modified_mtime_not_overridden(self):
        """A stale mtime record must not undo a changed mtime."""
        src = io.BytesIO()
        with tarfile.open(fileobj=src, mode='w',
                          format=tarfile.PAX_FORMAT) as tar:
            member = tarfile.TarInfo('file')
            member.mtime = 1700000000.5
            tar.addfile(member)
        src.seek(0)

        dst = io.BytesIO()
        with tarfile.open(fileobj=dst, mode='w') as out:
            with tarfile.open(fileobj=src, mode='r') as tar:
                for member in tar:
                    member.mtime = 0
                    add_member(out, member)

        dst.seek(0)
        with tarfile.open(fileobj=dst, mode='r') as tar:
            self.assertEqual(tar.getmembers()[0].mtime, 0)

    def test_unusual_members_rewritten(self):
        """Members only PAX can hold are rewritten rather than raising."""
        directory = tarfile.TarInfo('c' * 99 + '/' + 'd' * 100)
        directory.type = tarfile.DIRTYPE
        old = tarfile.TarInfo('old')
        old.mtime = -1
        _, rewritten = self._rewrite([(directory, b''), (old, b'')])
        self.assertEqual(rewritten[0].name, directory.name)
        self.assertTrue(rewritten[0].isdir())
        self.assertEqual(rewritten[1].mtime, -1)

    def test_plain_members_unchanged_size(self):
        """Members without extended needs are written as plain USTAR."""
        _, rewritten = self._rewrite([
            (tarfile.TarInfo('file1.txt'), b'content1'),
            (tarfile.TarInfo('dir/file2.txt'), b'content2'),
        ])
        for member in rewritten:
            self.assertEqual(member.pax_headers, {})


if __name__ == '__main__':
    unittest.main()
