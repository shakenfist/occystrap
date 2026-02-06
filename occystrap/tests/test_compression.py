"""Tests for the compression module."""

import gzip
import io
import unittest

import zstandard as zstd

from occystrap import compression
from occystrap import constants


class TestDetectCompression(unittest.TestCase):
    """Tests for compression detection from magic bytes."""

    def test_detect_gzip_from_bytes(self):
        """Test detection of gzip from raw bytes."""
        data = gzip.compress(b'hello world')
        result = compression.detect_compression(data)
        self.assertEqual(result, constants.COMPRESSION_GZIP)

    def test_detect_zstd_from_bytes(self):
        """Test detection of zstd from raw bytes."""
        cctx = zstd.ZstdCompressor()
        data = cctx.compress(b'hello world')
        result = compression.detect_compression(data)
        self.assertEqual(result, constants.COMPRESSION_ZSTD)

    def test_detect_gzip_from_file(self):
        """Test detection of gzip from file-like object."""
        data = gzip.compress(b'hello world')
        buf = io.BytesIO(data)
        result = compression.detect_compression(buf)
        self.assertEqual(result, constants.COMPRESSION_GZIP)
        # Verify position is restored
        self.assertEqual(buf.tell(), 0)

    def test_detect_zstd_from_file(self):
        """Test detection of zstd from file-like object."""
        cctx = zstd.ZstdCompressor()
        data = cctx.compress(b'hello world')
        buf = io.BytesIO(data)
        result = compression.detect_compression(buf)
        self.assertEqual(result, constants.COMPRESSION_ZSTD)
        # Verify position is restored
        self.assertEqual(buf.tell(), 0)

    def test_detect_unknown_from_random_bytes(self):
        """Test that random bytes return unknown."""
        data = b'not compressed data here'
        result = compression.detect_compression(data)
        self.assertEqual(result, constants.COMPRESSION_UNKNOWN)

    def test_detect_empty_data(self):
        """Test handling of empty data."""
        result = compression.detect_compression(b'')
        self.assertEqual(result, constants.COMPRESSION_UNKNOWN)

    def test_detect_short_data(self):
        """Test handling of very short data."""
        result = compression.detect_compression(b'x')
        self.assertEqual(result, constants.COMPRESSION_UNKNOWN)


class TestDetectCompressionFromMediaType(unittest.TestCase):
    """Tests for compression detection from media type."""

    def test_docker_gzip(self):
        """Test Docker gzip layer media type."""
        result = compression.detect_compression_from_media_type(
            constants.MEDIA_TYPE_DOCKER_LAYER_GZIP)
        self.assertEqual(result, constants.COMPRESSION_GZIP)

    def test_docker_zstd(self):
        """Test Docker zstd layer media type."""
        result = compression.detect_compression_from_media_type(
            constants.MEDIA_TYPE_DOCKER_LAYER_ZSTD)
        self.assertEqual(result, constants.COMPRESSION_ZSTD)

    def test_oci_gzip(self):
        """Test OCI gzip layer media type."""
        result = compression.detect_compression_from_media_type(
            constants.MEDIA_TYPE_OCI_LAYER_GZIP)
        self.assertEqual(result, constants.COMPRESSION_GZIP)

    def test_oci_zstd(self):
        """Test OCI zstd layer media type."""
        result = compression.detect_compression_from_media_type(
            constants.MEDIA_TYPE_OCI_LAYER_ZSTD)
        self.assertEqual(result, constants.COMPRESSION_ZSTD)

    def test_oci_uncompressed(self):
        """Test OCI uncompressed layer media type."""
        result = compression.detect_compression_from_media_type(
            constants.MEDIA_TYPE_OCI_LAYER_UNCOMPRESSED)
        self.assertEqual(result, constants.COMPRESSION_NONE)

    def test_none_media_type(self):
        """Test None media type."""
        result = compression.detect_compression_from_media_type(None)
        self.assertEqual(result, constants.COMPRESSION_UNKNOWN)

    def test_unknown_media_type(self):
        """Test unknown media type."""
        result = compression.detect_compression_from_media_type(
            'application/octet-stream')
        self.assertEqual(result, constants.COMPRESSION_UNKNOWN)

    def test_suffix_fallback_gzip(self):
        """Test fallback to suffix matching for gzip."""
        result = compression.detect_compression_from_media_type(
            'application/x-tar+gzip')
        self.assertEqual(result, constants.COMPRESSION_GZIP)

    def test_suffix_fallback_zstd(self):
        """Test fallback to suffix matching for zstd."""
        result = compression.detect_compression_from_media_type(
            'application/x-tar+zstd')
        self.assertEqual(result, constants.COMPRESSION_ZSTD)


class TestStreamingDecompressor(unittest.TestCase):
    """Tests for StreamingDecompressor class."""

    def test_gzip_decompression(self):
        """Test streaming gzip decompression."""
        original = b'hello world' * 100
        compressed = gzip.compress(original)

        decompressor = compression.StreamingDecompressor(
            constants.COMPRESSION_GZIP)
        result = decompressor.decompress(compressed)
        result += decompressor.flush()

        self.assertEqual(result, original)

    def test_zstd_decompression(self):
        """Test streaming zstd decompression."""
        original = b'hello world' * 100
        cctx = zstd.ZstdCompressor()
        compressed = cctx.compress(original)

        decompressor = compression.StreamingDecompressor(
            constants.COMPRESSION_ZSTD)
        result = decompressor.decompress(compressed)
        result += decompressor.flush()

        self.assertEqual(result, original)

    def test_none_passthrough(self):
        """Test that COMPRESSION_NONE passes data through."""
        original = b'uncompressed data'

        decompressor = compression.StreamingDecompressor(
            constants.COMPRESSION_NONE)
        result = decompressor.decompress(original)
        result += decompressor.flush()

        self.assertEqual(result, original)

    def test_unsupported_compression_raises(self):
        """Test that unsupported compression raises ValueError."""
        with self.assertRaises(ValueError):
            compression.StreamingDecompressor('invalid')


class TestStreamingCompressor(unittest.TestCase):
    """Tests for StreamingCompressor class."""

    def test_gzip_compression(self):
        """Test streaming gzip compression."""
        original = b'hello world' * 100

        compressor = compression.StreamingCompressor(
            constants.COMPRESSION_GZIP)
        compressor.compress(original)
        compressed = compressor.flush()

        # Verify by decompressing
        decompressed = gzip.decompress(compressed)
        self.assertEqual(decompressed, original)

    def test_zstd_compression(self):
        """Test streaming zstd compression."""
        original = b'hello world' * 100

        compressor = compression.StreamingCompressor(
            constants.COMPRESSION_ZSTD)
        compressor.compress(original)
        compressed = compressor.flush()

        # Verify by decompressing using our own decompressor
        decompressed = compression.decompress_data(
            compressed, constants.COMPRESSION_ZSTD)
        self.assertEqual(decompressed, original)

    def test_gzip_custom_level(self):
        """Test gzip with custom compression level."""
        original = b'hello world' * 100

        compressor = compression.StreamingCompressor(
            constants.COMPRESSION_GZIP, level=1)
        compressor.compress(original)
        compressed = compressor.flush()

        # Should still decompress correctly
        decompressed = gzip.decompress(compressed)
        self.assertEqual(decompressed, original)

    def test_zstd_custom_level(self):
        """Test zstd with custom compression level."""
        original = b'hello world' * 100

        compressor = compression.StreamingCompressor(
            constants.COMPRESSION_ZSTD, level=10)
        compressor.compress(original)
        compressed = compressor.flush()

        # Should still decompress correctly
        decompressed = compression.decompress_data(
            compressed, constants.COMPRESSION_ZSTD)
        self.assertEqual(decompressed, original)

    def test_unsupported_compression_raises(self):
        """Test that unsupported compression raises ValueError."""
        with self.assertRaises(ValueError):
            compression.StreamingCompressor('invalid')


class TestRoundTrip(unittest.TestCase):
    """Tests for round-trip compression/decompression."""

    def test_gzip_roundtrip(self):
        """Test gzip compress then decompress."""
        original = b'test data for roundtrip' * 50

        compressed = compression.compress_data(
            original, constants.COMPRESSION_GZIP)
        decompressed = compression.decompress_data(
            compressed, constants.COMPRESSION_GZIP)

        self.assertEqual(decompressed, original)

    def test_zstd_roundtrip(self):
        """Test zstd compress then decompress."""
        original = b'test data for roundtrip' * 50

        compressed = compression.compress_data(
            original, constants.COMPRESSION_ZSTD)
        decompressed = compression.decompress_data(
            compressed, constants.COMPRESSION_ZSTD)

        self.assertEqual(decompressed, original)

    def test_large_data_gzip(self):
        """Test gzip with larger data."""
        original = b'x' * (1024 * 1024)  # 1MB

        compressed = compression.compress_data(
            original, constants.COMPRESSION_GZIP)
        decompressed = compression.decompress_data(
            compressed, constants.COMPRESSION_GZIP)

        self.assertEqual(decompressed, original)

    def test_large_data_zstd(self):
        """Test zstd with larger data."""
        original = b'x' * (1024 * 1024)  # 1MB

        compressed = compression.compress_data(
            original, constants.COMPRESSION_ZSTD)
        decompressed = compression.decompress_data(
            compressed, constants.COMPRESSION_ZSTD)

        self.assertEqual(decompressed, original)


class TestGetMediaTypeForCompression(unittest.TestCase):
    """Tests for get_media_type_for_compression."""

    def test_docker_gzip(self):
        """Test Docker gzip media type."""
        result = compression.get_media_type_for_compression(
            constants.COMPRESSION_GZIP, use_oci=False)
        self.assertEqual(result, constants.MEDIA_TYPE_DOCKER_LAYER_GZIP)

    def test_docker_zstd(self):
        """Test Docker zstd media type."""
        result = compression.get_media_type_for_compression(
            constants.COMPRESSION_ZSTD, use_oci=False)
        self.assertEqual(result, constants.MEDIA_TYPE_DOCKER_LAYER_ZSTD)

    def test_oci_gzip(self):
        """Test OCI gzip media type."""
        result = compression.get_media_type_for_compression(
            constants.COMPRESSION_GZIP, use_oci=True)
        self.assertEqual(result, constants.MEDIA_TYPE_OCI_LAYER_GZIP)

    def test_oci_zstd(self):
        """Test OCI zstd media type."""
        result = compression.get_media_type_for_compression(
            constants.COMPRESSION_ZSTD, use_oci=True)
        self.assertEqual(result, constants.MEDIA_TYPE_OCI_LAYER_ZSTD)

    def test_unsupported_raises(self):
        """Test that unsupported compression raises ValueError."""
        with self.assertRaises(ValueError):
            compression.get_media_type_for_compression('invalid')


if __name__ == '__main__':
    unittest.main()
