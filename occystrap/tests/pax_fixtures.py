"""PAX extended record fixtures shared by the tar rewriting tests."""

# A v2 security.capability xattr granting cap_net_raw (bit 13) and
# cap_sys_admin (bit 21), effective and permitted. The 0x80 byte is not
# valid UTF-8, which exercises tarfile's binary PAX value handling.
CAPABILITY = bytes([
    0x01, 0x00, 0x00, 0x02,
    0x80, 0x20, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])
CAPABILITY_KEY = 'SCHILY.xattr.security.capability'


def capability_header():
    """Return CAPABILITY as tarfile represents a PAX value."""
    return CAPABILITY.decode('utf-8', 'surrogateescape')


def read_capability(member):
    """Return the raw security.capability bytes of a member, or None."""
    value = member.pax_headers.get(CAPABILITY_KEY)
    if value is None:
        return None
    return value.encode('utf-8', 'surrogateescape')
