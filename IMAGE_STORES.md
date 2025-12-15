# Image Stores

This document describes the image store components and their roles in the
Occystrap codebase.

## Input Image Stores

### inputs.registry.Image

Location: `occystrap/inputs/registry.py`

Fetches container images from Docker/OCI registries via HTTP API. Handles:
- Registry authentication (basic auth, token-based)
- Manifest parsing (v1, v2, OCI)
- Multi-architecture image selection
- Layer blob downloading

### inputs.tarfile.Image

Location: `occystrap/inputs/tarfile.py`

Reads container images from docker-save format tarballs. Parses manifest.json
and yields config files and layers.

## Output Image Stores

All output image stores implement the standard interface:
- `fetch_callback(digest)` - Layer filtering
- `process_image_element(type, name, data)` - Element processing
- `finalize()` - Completion

### output_tarfile.TarWriter

Location: `occystrap/output_tarfile.py`

Creates docker-loadable tarballs in v1.2 format. Supports timestamp
normalization for reproducible builds.

### output_directory.DirWriter / DirReader

Location: `occystrap/output_directory.py`

- `DirWriter` - Extracts images to directories with optional deduplication
- `DirReader` - Reads from shared directories to recreate images

### output_ocibundle.OCIBundleWriter

Location: `occystrap/output_ocibundle.py`

Creates OCI runtime bundles suitable for runc execution.

### output_mounts.MountWriter

Location: `occystrap/output_mounts.py`

Creates overlay mount-based extraction using extended attributes.

## Search Component

### search.LayerSearcher

Location: `occystrap/search.py`

Implements the output interface but searches layers for matching file paths
instead of writing them. Supports glob and regex patterns.

## CLI Commands

Location: `occystrap/main.py`

Click-based CLI providing commands:
- `fetch-to-tarfile` - Registry to tarball
- `fetch-to-extracted` - Registry to directory
- `fetch-to-oci` - Registry to OCI bundle
- `fetch-to-mounts` - Registry to overlay mounts
- `recreate-image` - Shared directory to tarball
- `tarfile-to-extracted` - Tarball to directory
- `search-layers` - Search registry image layers
- `search-layers-tarfile` - Search tarball image layers
