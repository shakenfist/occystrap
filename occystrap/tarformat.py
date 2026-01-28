# Smart tar format selection for occystrap.
#
# Uses USTAR format by default (smaller output), falls back to PAX when needed.
# This can save ~1KB per file with long names (>100 chars) which adds up to
# tens of megabytes on large container layers.
#
# See docs/tar-format-selection.md for detailed explanation.

import logging
import os
import tarfile


LOG = logging.getLogger(__name__)

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


def needs_pax_format(member):
    """
    Check if a TarInfo member requires PAX format due to USTAR limitations.

    USTAR format is more compact but has restrictions. This function checks
    if a member exceeds any of those restrictions.

    Args:
        member: A TarInfo object to check.

    Returns:
        bool: True if PAX format is required, False if USTAR suffices.
    """
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

    # Check for non-ASCII characters (USTAR only supports ASCII)
    try:
        member.name.encode('ascii')
        if member.linkname:
            member.linkname.encode('ascii')
    except UnicodeEncodeError:
        return True

    return False


def select_tar_format_for_layer(layer_fileobj, transform_fn=None, skip_fn=None):
    """
    Determine the optimal tar format for a layer after applying transforms.

    This performs a read-only scan of the layer to check if any members
    (after transformation and filtering) would require PAX format. Returns
    as soon as a PAX-requiring member is found.

    Args:
        layer_fileobj: File-like object containing the tar layer.
        transform_fn: Optional function(TarInfo) -> TarInfo that will be
                      applied to members. The format check uses the
                      transformed member attributes.
        skip_fn: Optional function(TarInfo) -> bool that returns True for
                 members that will be skipped/excluded. These members are
                 not considered in the format selection.

    Returns:
        tarfile format constant: tarfile.USTAR_FORMAT or tarfile.PAX_FORMAT
    """
    layer_fileobj.seek(0)

    with tarfile.open(fileobj=layer_fileobj, mode='r') as tar:
        for member in tar:
            if skip_fn and skip_fn(member):
                continue

            if transform_fn:
                member = transform_fn(member)

            if needs_pax_format(member):
                layer_fileobj.seek(0)
                LOG.debug('Layer requires PAX format')
                return tarfile.PAX_FORMAT

    layer_fileobj.seek(0)
    LOG.debug('Layer compatible with USTAR format')
    return tarfile.USTAR_FORMAT
