# Smart tar format selection for occystrap.
#
# Each member of a rewritten layer is written as USTAR when it fits (smaller
# output), and as PAX only when it needs to be. This can save ~1KB per file
# with long names (>100 chars) which adds up to tens of megabytes on large
# container layers, without losing the PAX records (such as xattrs carrying
# file capabilities) that some members depend on.
#
# See docs/tar-format-selection.md for detailed explanation.

import os
import tarfile


# USTAR format limits (POSIX.1-1988)
#
# USTAR stores paths using two fields:
#   - name: 100 bytes for the filename
#   - prefix: 155 bytes for the directory path
#
# Combined, this allows paths up to 256 characters (prefix + '/' + name)
# without requiring extended headers.
#
# PAX format (POSIX.1-2001) adds extended header blocks for metadata that
# doesn't fit in the USTAR header. Each extended header adds ~1KB overhead.
USTAR_MAX_PATH = 256
USTAR_MAX_NAME = 100
USTAR_MAX_PREFIX = 155
USTAR_MAX_LINKNAME = 100
USTAR_MAX_SIZE = 8 * 1024 * 1024 * 1024 - 1  # 8 GiB - 1 byte
USTAR_MAX_ID = 0o7777777  # 2097151 (max value in 8-byte octal field)
USTAR_MAX_OWNER_NAME = 32  # uname and gname fields

# PAX records which mirror a TarInfo field. tarfile applies these to the
# TarInfo when reading, but also leaves them in pax_headers where they take
# priority over the field when the member is written again. A filter which
# changes a field (for example the mtime) would therefore have its change
# silently undone. We drop them and let tarfile regenerate them from the
# fields when they are needed.
PAX_FIELD_RECORDS = frozenset(
    ['path', 'linkpath', 'size', 'uid', 'gid', 'uname', 'gname', 'mtime'])

# PAX records which describe how the source archive was encoded rather than
# the file itself. tarfile has already decoded these (for sparse files the
# data we copy is the expanded data), so carrying them forward would describe
# data we are not writing and corrupt the output.
PAX_ENCODING_RECORDS = frozenset(['hdrcharset'])
PAX_ENCODING_RECORD_PREFIXES = ('GNU.sparse.',)


def prepare_member_for_rewrite(member):
    """
    Remove PAX records which must not be copied into a rewritten layer.

    Everything left in member.pax_headers afterwards is metadata which only
    PAX can represent, such as SCHILY.xattr.* records (file capabilities,
    SELinux labels and user xattrs), ACLs and atime / ctime.

    Args:
        member: A TarInfo object read from a source layer. It is modified
                in place.

    Returns:
        The same TarInfo object, for convenience.
    """
    member.pax_headers = {
        k: v for k, v in member.pax_headers.items()
        if (k not in PAX_FIELD_RECORDS and
            k not in PAX_ENCODING_RECORDS and
            not k.startswith(PAX_ENCODING_RECORD_PREFIXES))
    }
    return member


def needs_pax_format(member):
    """
    Check if a TarInfo member requires PAX format.

    USTAR format is more compact but has restrictions, and cannot carry PAX
    extended records at all: Python's USTAR writer silently discards
    member.pax_headers. This function checks if a member exceeds any of the
    USTAR restrictions, or has extended records which must be preserved.

    Callers rewriting a member read from another archive should call
    prepare_member_for_rewrite() first, so that records which merely mirror
    TarInfo fields do not force PAX.

    Args:
        member: A TarInfo object to check.

    Returns:
        bool: True if PAX format is required, False if USTAR suffices.
    """
    # Extended records (xattrs, ACLs, atime and so on) only exist in PAX
    if member.pax_headers:
        return True

    # Check total path length
    if len(member.name) > USTAR_MAX_PATH:
        return True

    # Check if path can be split into prefix + name for USTAR
    # The path must be splittable at a '/' boundary where:
    #   - basename (after last '/') <= 100 chars
    #   - dirname (before last '/') <= 155 chars
    if len(member.name) > USTAR_MAX_NAME:
        basename = os.path.basename(member.name)
        dirname = os.path.dirname(member.name)
        if len(basename) > USTAR_MAX_NAME or len(dirname) > USTAR_MAX_PREFIX:
            return True

    # Check symlink/hardlink target length
    if member.linkname and len(member.linkname) > USTAR_MAX_LINKNAME:
        return True

    # Check file size (USTAR uses 12-byte octal, max ~8 GiB)
    if member.size > USTAR_MAX_SIZE:
        return True

    # Check UID/GID (USTAR uses 8-byte octal fields)
    if member.uid > USTAR_MAX_ID or member.gid > USTAR_MAX_ID:
        return True

    # Check owner names (USTAR uses 32-byte fields)
    if (len(member.uname) > USTAR_MAX_OWNER_NAME or
            len(member.gname) > USTAR_MAX_OWNER_NAME):
        return True

    # Check for sub-second modification times (USTAR stores whole seconds)
    if member.mtime != int(member.mtime):
        return True

    # Check for non-ASCII characters (USTAR only supports ASCII)
    try:
        member.name.encode('ascii')
        if member.linkname:
            member.linkname.encode('ascii')
        member.uname.encode('ascii')
        member.gname.encode('ascii')
    except UnicodeEncodeError:
        return True

    return False


def add_member(tar, member, fileobj=None):
    """
    Add a member read from a source layer to a rewritten layer.

    The member is written as USTAR if it fits, and as PAX otherwise. A tar
    archive may mix the two: a PAX archive is a USTAR archive in which some
    members are preceded by an extended header. Choosing per member means a
    single file with a capability does not cost every long-named file in the
    layer its own extended header.

    Args:
        tar: A TarFile opened for writing.
        member: A TarInfo object read from the source layer. It is modified
                in place, see prepare_member_for_rewrite().
        fileobj: File-like object with the member's data, for regular files.
    """
    prepare_member_for_rewrite(member)
    if needs_pax_format(member):
        tar.format = tarfile.PAX_FORMAT
    else:
        tar.format = tarfile.USTAR_FORMAT
    tar.addfile(member, fileobj)
