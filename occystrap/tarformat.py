# Smart tar format selection for occystrap.
#
# Each member of a rewritten layer is written as USTAR when it fits (smaller
# output), and as PAX only when it needs to be. This can save ~1KB per file
# with long names (>100 chars) which adds up to tens of megabytes on large
# container layers, without losing the PAX records (such as xattrs carrying
# file capabilities) that some members depend on.
#
# See docs/tar-format-selection.md for detailed explanation.

import tarfile


# USTAR format limits (POSIX.1-1988)
#
# USTAR stores paths in two fields, a 100 byte name and a 155 byte prefix,
# and numbers (size, uid, gid, mtime, device numbers) in fixed width octal
# fields. Rather than restate those limits here, needs_pax_format() asks
# tarfile to encode a USTAR header and falls back to PAX if it cannot, so
# the check is exactly the one the writer applies. That includes details
# such as the trailing '/' tarfile adds to directory names when writing.
#
# Some things do not fit USTAR but tarfile writes anyway, silently losing
# information. Those are checked explicitly below. The only limit of that
# kind needing a constant is the 32 byte uname and gname fields, which
# tarfile truncates.
#
# PAX format (POSIX.1-2001) adds extended header blocks for metadata that
# doesn't fit in the USTAR header. Each extended header adds ~1KB overhead.
USTAR_MAX_OWNER_NAME = 32

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

# The version of the layer rewriting rules in this module. Layer caches
# include it in their key, so that layers rewritten by older rules (which,
# before issue #151 was fixed, stripped file capabilities) are not reused.
# Increment it whenever a change here alters the bytes a filter writes.
LAYER_REWRITE_VERSION = 2


def prepare_member_for_rewrite(member):
    """
    Remove PAX records which must not be copied into a rewritten layer.

    Everything left in member.pax_headers afterwards is metadata which only
    PAX can represent, such as SCHILY.xattr.* records (file capabilities,
    SELinux labels and user xattrs), ACLs and atime / ctime.

    Old-style GNU sparse members (type 'S') become regular files, for the
    same reason the GNU.sparse.* records are dropped: the data written is
    the expanded data, and an 'S' header without its sparse map would be
    read back as an empty file.

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
    if member.type == tarfile.GNUTYPE_SPARSE:
        member.type = tarfile.REGTYPE
    member.sparse = None
    return member


def needs_pax_format(member, encoding=tarfile.ENCODING,
                     errors='surrogateescape'):
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
        encoding: The encoding the member will be written with. Pass the
                  writing TarFile's encoding, as add_member() does.
        errors: The encoding error handler, likewise.

    Returns:
        bool: True if PAX format is required, False if USTAR suffices.
    """
    # Extended records (xattrs, ACLs, atime and so on) only exist in PAX
    if member.pax_headers:
        return True

    # Check owner names (USTAR uses 32-byte fields, which tarfile truncates)
    if (len(member.uname) > USTAR_MAX_OWNER_NAME or
            len(member.gname) > USTAR_MAX_OWNER_NAME):
        return True

    # Check for sub-second modification times (USTAR stores whole seconds,
    # and tarfile truncates)
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

    # Everything else (path and link lengths, size, ids, mtime range and
    # device numbers) is a hard limit which tarfile enforces by raising
    # ValueError. Ask it rather than restating its rules.
    try:
        member.tobuf(tarfile.USTAR_FORMAT, encoding, errors)
    except ValueError:
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
    if needs_pax_format(member, tar.encoding, tar.errors):
        tar.format = tarfile.PAX_FORMAT
    else:
        tar.format = tarfile.USTAR_FORMAT
    tar.addfile(member, fileobj)
