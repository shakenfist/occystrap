"""Compression utilities for container image layers.

This module provides detection and streaming compression/decompression
for gzip and zstd formats used in Docker/OCI container images.
"""

import gzip
import io
import zlib

import zstandard as zstd

from occystrap import constants


# Magic bytes for compression format detection
GZIP_MAGIC = b'\x1f\x8b'
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def detect_compression(data):
    """Detect compression format from magic bytes.

    Args:
        data: Bytes or file-like object with at least 4 bytes.

    Returns:
        One of COMPRESSION_GZIP, COMPRESSION_ZSTD, COMPRESSION_NONE,
        or COMPRESSION_UNKNOWN.
    """
    if hasattr(data, 'read'):
        # File-like object - read and seek back
        pos = data.tell() if hasattr(data, 'tell') else 0
        magic = data.read(4)
        if hasattr(data, 'seek'):
            data.seek(pos)
        else:
            # Can't seek back - this is a problem
            raise ValueError('Cannot detect compression on non-seekable stream')
    else:
        magic = data[:4] if len(data) >= 4 else data

    if len(magic) < 2:
        return constants.COMPRESSION_UNKNOWN

    if magic[:2] == GZIP_MAGIC:
        return constants.COMPRESSION_GZIP
    if len(magic) >= 4 and magic[:4] == ZSTD_MAGIC:
        return constants.COMPRESSION_ZSTD

    # Check for tar magic at offset 257 (ustar format)
    # If we can see tar magic, it's uncompressed
    if hasattr(data, 'read') and hasattr(data, 'seek'):
        pos = data.tell()
        data.seek(257)
        tar_magic = data.read(5)
        data.seek(pos)
        if tar_magic == b'ustar':
            return constants.COMPRESSION_NONE

    return constants.COMPRESSION_UNKNOWN


def detect_compression_from_media_type(media_type):
    """Detect compression format from OCI/Docker media type.

    Args:
        media_type: Media type string from manifest.

    Returns:
        One of COMPRESSION_GZIP, COMPRESSION_ZSTD, COMPRESSION_NONE,
        or COMPRESSION_UNKNOWN.
    """
    if media_type is None:
        return constants.COMPRESSION_UNKNOWN

    if media_type in (constants.MEDIA_TYPE_DOCKER_LAYER_GZIP,
                      constants.MEDIA_TYPE_OCI_LAYER_GZIP):
        return constants.COMPRESSION_GZIP
    if media_type in (constants.MEDIA_TYPE_DOCKER_LAYER_ZSTD,
                      constants.MEDIA_TYPE_OCI_LAYER_ZSTD):
        return constants.COMPRESSION_ZSTD
    if media_type == constants.MEDIA_TYPE_OCI_LAYER_UNCOMPRESSED:
        return constants.COMPRESSION_NONE

    # Fallback: check for known suffixes
    if media_type.endswith('+gzip') or media_type.endswith('.gzip'):
        return constants.COMPRESSION_GZIP
    if media_type.endswith('+zstd') or media_type.endswith('.zstd'):
        return constants.COMPRESSION_ZSTD
    if media_type.endswith('.tar') and '+' not in media_type:
        return constants.COMPRESSION_NONE

    return constants.COMPRESSION_UNKNOWN


class StreamingDecompressor:
    """Streaming decompressor for gzip and zstd formats.

    This class provides a unified interface for streaming decompression,
    allowing data to be decompressed chunk by chunk as it arrives.
    """

    def __init__(self, compression_type):
        """Initialize the decompressor.

        Args:
            compression_type: One of COMPRESSION_GZIP, COMPRESSION_ZSTD,
                or COMPRESSION_NONE.

        Raises:
            ValueError: If compression_type is not supported.
        """
        self.compression_type = compression_type

        if compression_type == constants.COMPRESSION_GZIP:
            # Use zlib with gzip header support (16 + MAX_WBITS)
            self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif compression_type == constants.COMPRESSION_ZSTD:
            self._decompressor = zstd.ZstdDecompressor().decompressobj()
        elif compression_type == constants.COMPRESSION_NONE:
            self._decompressor = None
        else:
            raise ValueError(
                'Unsupported compression type: %s' % compression_type)

    def decompress(self, chunk):
        """Decompress a chunk of data.

        Args:
            chunk: Bytes to decompress.

        Returns:
            Decompressed bytes.
        """
        if self._decompressor is None:
            return chunk
        return self._decompressor.decompress(chunk)

    def flush(self):
        """Flush any remaining buffered data.

        Returns:
            Any remaining decompressed bytes.
        """
        if self._decompressor is None:
            return b''
        if self.compression_type == constants.COMPRESSION_GZIP:
            return self._decompressor.flush()
        # zstd doesn't have a flush method on decompressobj
        return b''


class StreamingCompressor:
    """Streaming compressor for gzip and zstd formats.

    This class provides a unified interface for streaming compression,
    allowing data to be compressed chunk by chunk.
    """

    def __init__(self, compression_type, level=None):
        """Initialize the compressor.

        Args:
            compression_type: One of COMPRESSION_GZIP, COMPRESSION_ZSTD.
            level: Compression level (optional, uses default if not specified).
                For gzip: 0-9 (default 9)
                For zstd: 1-22 (default 3)

        Raises:
            ValueError: If compression_type is not supported.
        """
        self.compression_type = compression_type
        self._buffer = io.BytesIO()

        if compression_type == constants.COMPRESSION_GZIP:
            self._level = level if level is not None else 9
        elif compression_type == constants.COMPRESSION_ZSTD:
            self._level = level if level is not None else 3
        else:
            raise ValueError(
                'Unsupported compression type: %s' % compression_type)

    def compress(self, chunk):
        """Compress a chunk of data.

        Args:
            chunk: Bytes to compress.

        Returns:
            Compressed bytes (may be empty if buffering).
        """
        # Buffer all data for final compression
        self._buffer.write(chunk)
        return b''

    def flush(self):
        """Flush and finalize compression.

        Returns:
            Final compressed bytes.
        """
        self._buffer.seek(0)
        data = self._buffer.read()

        if self.compression_type == constants.COMPRESSION_GZIP:
            compressed = io.BytesIO()
            with gzip.GzipFile(fileobj=compressed, mode='wb',
                               compresslevel=self._level) as gz:
                gz.write(data)
            return compressed.getvalue()
        else:
            # Use zstd compressor with write_content_size for compatibility
            cctx = zstd.ZstdCompressor(level=self._level,
                                       write_content_size=True)
            return cctx.compress(data)


def compress_data(data, compression_type, level=None):
    """Compress data in a single operation.

    Args:
        data: Bytes to compress.
        compression_type: One of COMPRESSION_GZIP, COMPRESSION_ZSTD.
        level: Compression level (optional).

    Returns:
        Compressed bytes.
    """
    compressor = StreamingCompressor(compression_type, level=level)
    compressor.compress(data)
    return compressor.flush()


def decompress_data(data, compression_type):
    """Decompress data in a single operation.

    Args:
        data: Bytes to decompress.
        compression_type: One of COMPRESSION_GZIP, COMPRESSION_ZSTD,
            or COMPRESSION_NONE.

    Returns:
        Decompressed bytes.
    """
    decompressor = StreamingDecompressor(compression_type)
    result = decompressor.decompress(data)
    result += decompressor.flush()
    return result


def get_media_type_for_compression(compression_type, use_oci=False):
    """Get the appropriate media type for a compression format.

    Args:
        compression_type: One of COMPRESSION_GZIP, COMPRESSION_ZSTD.
        use_oci: If True, return OCI media type; otherwise Docker.

    Returns:
        Media type string.
    """
    if compression_type == constants.COMPRESSION_GZIP:
        if use_oci:
            return constants.MEDIA_TYPE_OCI_LAYER_GZIP
        return constants.MEDIA_TYPE_DOCKER_LAYER_GZIP
    elif compression_type == constants.COMPRESSION_ZSTD:
        if use_oci:
            return constants.MEDIA_TYPE_OCI_LAYER_ZSTD
        return constants.MEDIA_TYPE_DOCKER_LAYER_ZSTD
    else:
        raise ValueError('Unsupported compression type: %s' % compression_type)
