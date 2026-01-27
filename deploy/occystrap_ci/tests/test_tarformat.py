"""Tests for the tarformat module."""

import io
import tarfile
import unittest

from occystrap.tarformat import (
    needs_pax_format,
    select_tar_format_for_layer,
    USTAR_MAX_PATH,
    USTAR_MAX_NAME,
    USTAR_MAX_LINKNAME,
    USTAR_MAX_ID,
)


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


class TestSelectTarFormatForLayer(unittest.TestCase):
    """Tests for select_tar_format_for_layer function."""

    def _create_tar(self, members):
        """Create a tar archive with given members.

        Args:
            members: List of (name, content) or (name, content, kwargs) tuples.

        Returns:
            BytesIO containing the tar archive.
        """
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as tar:
            for item in members:
                if len(item) == 2:
                    name, content = item
                    kwargs = {}
                else:
                    name, content, kwargs = item

                data = content.encode() if isinstance(content, str) else content
                ti = tarfile.TarInfo(name=name)
                ti.size = len(data)
                for k, v in kwargs.items():
                    setattr(ti, k, v)
                tar.addfile(ti, io.BytesIO(data))
        buf.seek(0)
        return buf

    def test_normal_files_select_ustar(self):
        """Normal files should select USTAR format."""
        layer = self._create_tar([
            ('file1.txt', 'content1'),
            ('dir/file2.txt', 'content2'),
        ])
        fmt = select_tar_format_for_layer(layer)
        self.assertEqual(fmt, tarfile.USTAR_FORMAT)

    def test_long_path_selects_pax(self):
        """Layer with long path should select PAX format."""
        long_path = 'a' * 200 + '/' + 'b' * 57  # 258 chars
        layer = self._create_tar([
            ('short.txt', 'content'),
            (long_path, 'content'),
        ])
        fmt = select_tar_format_for_layer(layer)
        self.assertEqual(fmt, tarfile.PAX_FORMAT)

    def test_non_ascii_selects_pax(self):
        """Layer with non-ASCII filename should select PAX format."""
        layer = self._create_tar([
            ('normal.txt', 'content'),
            ('Főtanúsítvány.pem', 'certificate'),
        ])
        fmt = select_tar_format_for_layer(layer)
        self.assertEqual(fmt, tarfile.PAX_FORMAT)

    def test_transform_fn_applied(self):
        """Transform function should be applied before format check."""
        layer = self._create_tar([
            ('file.txt', 'content'),
        ])

        def make_long_name(member):
            member.name = 'x' * 257
            return member

        fmt = select_tar_format_for_layer(layer, transform_fn=make_long_name)
        self.assertEqual(fmt, tarfile.PAX_FORMAT)

    def test_skip_fn_excludes_members(self):
        """Skip function should exclude members from format check."""
        long_path = 'a' * 257
        layer = self._create_tar([
            ('keep.txt', 'content'),
            (long_path, 'content'),
        ])

        # Skip the long path, should select USTAR
        fmt = select_tar_format_for_layer(
            layer,
            skip_fn=lambda m: len(m.name) > 256
        )
        self.assertEqual(fmt, tarfile.USTAR_FORMAT)

    def test_fileobj_reset_after_scan(self):
        """File object should be reset to beginning after scan."""
        layer = self._create_tar([
            ('file.txt', 'content'),
        ])
        initial_pos = layer.tell()
        select_tar_format_for_layer(layer)
        self.assertEqual(layer.tell(), initial_pos)


if __name__ == '__main__':
    unittest.main()
