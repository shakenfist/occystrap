# Plan: Add zstd Compression Support to Occystrap

**Status: IMPLEMENTED** (2026-02-07)

## Summary

Add zstd (Zstandard) compression support for container image layers:
- **Input**: Detect and decompress zstd layers from registries and OCI tarballs
- **Output**: Optionally compress with zstd when pushing to registries (gzip default)

## Implementation Notes

- Media type constants were centralized in `constants.py` rather than
  `compression.py` for consistency with other code using media types
- Unit tests added in `occystrap/tests/test_compression.py`
- Documentation updated in README.md, ARCHITECTURE.md, AGENTS.md, and
  docs/command-reference.md

## Files to Modify

### 1. pyproject.toml - Add dependency
Add `zstandard>=0.21.0` to dependencies (line 36).

### 2. NEW: occystrap/compression.py - Compression utilities
Create new module with:
- `detect_compression(data)` - detect format from magic bytes
- `detect_compression_from_media_type(media_type)` - detect from OCI media type
- `StreamingDecompressor` class - handles gzip/zstd streaming decompression
- `StreamingCompressor` class - handles gzip/zstd compression for output
- Media type constants for gzip/zstd variants

### 3. inputs/registry.py - Zstd decompression support
Lines 213-240: Replace hardcoded gzip decompression with:
- Get media type from manifest layer entry
- Detect compression from media type (fallback to magic bytes)
- Use `StreamingDecompressor` with detected format
- Handle uncompressed layers (unusual but possible)

### 4. inputs/tarfile.py - OCI tarball zstd support
Lines 105-110: After reading layer data:
- For OCI format (`blobs/` paths), detect compression from magic bytes
- Decompress if compressed before yielding

### 5. outputs/registry.py - Zstd compression option
- Add `compression_type` parameter to `__init__` (default 'gzip')
- Lines 179-203: Use `StreamingCompressor` with configured type
- Set correct media type in layer manifest entry

### 6. pipeline.py - Pass compression option
Lines 154-166: Pass `compression` option from URI to `RegistryWriter`.

### 7. main.py - CLI flag
Add `--compression` option (choices: gzip, zstd) with env var support.

## Implementation Order

1. Add zstandard dependency to pyproject.toml
2. Create compression.py with detection and streaming classes
3. Update inputs/registry.py for zstd input
4. Update inputs/tarfile.py for OCI tarball zstd
5. Update outputs/registry.py for zstd output
6. Update pipeline.py and main.py for CLI support
7. Add unit tests for compression module
8. Update README.md

## Testing

### Unit Tests (new file: occystrap/tests/test_compression.py)
- Test magic byte detection for gzip/zstd/unknown
- Test media type detection
- Test round-trip compress/decompress for both formats
- Test error handling for unsupported formats

### Integration Tests
- Create zstd-compressed test image in occystrap-testdata
- Test registry pull with zstd layers
- Test registry push with `--compression=zstd`
- Test OCI tarball with zstd layers

### Manual Verification
```bash
# Install updated occystrap
pip install -e .

# Test zstd input (need a zstd-compressed image)
occystrap process registry://ghcr.io/zstd-image:latest tarfile://test.tar

# Test zstd output
occystrap --compression=zstd process \
  docker://busybox:latest \
  registry://localhost:5000/test:zstd
```

## Notes

- Gzip remains default for maximum compatibility with older runtimes
- zstd requires Docker 20.10+ or containerd 1.5+ on client side
- zstd offers ~30% better compression ratio and faster compression
