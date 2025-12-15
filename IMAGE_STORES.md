# Image Stores

This document describes the image store components and their roles in the
Occystrap codebase.

## Abstract Base Classes

### inputs.base.ImageInput

Location: `occystrap/inputs/base.py`

Abstract base class for all input sources. Defines the interface:
- `image` (property) - Returns the image name
- `tag` (property) - Returns the image tag
- `fetch(fetch_callback)` - Yields image elements (config files and layers)

### outputs.base.ImageOutput

Location: `occystrap/outputs/base.py`

Abstract base class for all output writers. Defines the interface:
- `fetch_callback(digest)` - Determine whether a layer should be fetched
- `process_image_element(element_type, name, data)` - Process a single element
- `finalize()` - Complete the output operation

### filters.base.ImageFilter

Location: `occystrap/filters/base.py`

Abstract base class for filters. Inherits from `ImageOutput` and wraps another
output (decorator pattern). Defines the interface:
- `__init__(wrapped_output)` - Wrap another output or filter
- `fetch_callback(digest)` - Delegates to wrapped output by default
- `process_image_element(element_type, name, data)` - Process/transform elements
- `finalize()` - Delegates to wrapped output by default

## Input Image Stores

All input image stores inherit from `ImageInput`.

### inputs.docker.Image

Location: `occystrap/inputs/docker.py`

Fetches container images from the local Docker or Podman daemon via the
Docker Engine API over a Unix domain socket (default: `/var/run/docker.sock`).
Handles:
- Streaming image export (equivalent to `docker save`)
- Parsing docker-save format on the fly
- Custom socket path configuration

**Podman Compatibility**: This input source also works with Podman, which
provides a Docker-compatible API. Use the `--socket` option to point to the
Podman socket (`/run/podman/podman.sock` for rootful, or
`/run/user/<uid>/podman/podman.sock` for rootless).

**API Limitation**: Unlike the registry API which allows fetching individual
layer blobs via `GET /v2/<name>/blobs/<digest>`, the Docker Engine API only
provides the `/images/{name}/get` endpoint which returns a complete tarball.
There is no way to fetch individual image components (config, layers)
separately. This is a fundamental limitation of the Docker Engine API design.
The tarball streaming approach used here is the official supported method and
matches what `docker save` does internally.

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

## Filters

All filters inherit from `ImageFilter` and implement the decorator pattern,
wrapping another output or filter.

### filters.normalize_timestamps.TimestampNormalizer

Location: `occystrap/filters/normalize_timestamps.py`

Normalizes timestamps in image layers for reproducible builds. Rewrites layer
tarballs to set all file modification times to a consistent value (default: 0,
Unix epoch). Since this changes layer content, SHA256 hashes are recalculated
and layer names are updated.

Options:
- `timestamp` - Unix timestamp to use (default: 0)

### filters.search.SearchFilter

Location: `occystrap/filters/search.py`

Searches layers for files matching a pattern. Can operate in two modes:
- **Search-only**: `wrapped_output=None`, just prints results
- **Passthrough**: Searches AND passes elements to wrapped output

Options:
- `pattern` - Glob pattern or regex to match
- `use_regex` - Treat pattern as regex instead of glob
- `script_friendly` - Output in machine-parseable format
- `image`, `tag` - For output formatting

## Output Image Stores

All output image stores inherit from `ImageOutput`, which defines the interface:
- `fetch_callback(digest)` - Layer filtering
- `process_image_element(type, name, data)` - Element processing
- `finalize()` - Completion

### outputs.tarfile.TarWriter

Location: `occystrap/outputs/tarfile.py`

Creates docker-loadable tarballs in v1.2 format. For timestamp normalization,
use the `TimestampNormalizer` filter.

### outputs.directory.DirWriter / DirReader

Location: `occystrap/outputs/directory.py`

- `DirWriter` - Extracts images to directories with optional deduplication
- `DirReader` - Reads from shared directories to recreate images

### outputs.ocibundle.OCIBundleWriter

Location: `occystrap/outputs/ocibundle.py`

Creates OCI runtime bundles suitable for runc execution.

### outputs.mounts.MountWriter

Location: `occystrap/outputs/mounts.py`

Creates overlay mount-based extraction using extended attributes.

## CLI Commands

Location: `occystrap/main.py`

### New URI-style Commands (Recommended)

- `process` - Unified pipeline command with URI-style arguments
- `search` - Search for files in image layers

### Legacy Commands (Deprecated)

These commands still work but are deprecated in favor of the `process` command:

- `fetch-to-tarfile` - Registry to tarball
- `fetch-to-extracted` - Registry to directory
- `fetch-to-oci` - Registry to OCI bundle
- `fetch-to-mounts` - Registry to overlay mounts
- `recreate-image` - Shared directory to tarball
- `tarfile-to-extracted` - Tarball to directory
- `docker-to-tarfile` - Local Docker daemon to tarball
- `docker-to-extracted` - Local Docker daemon to directory
- `docker-to-oci` - Local Docker daemon to OCI bundle
- `search-layers` - Search registry image layers
- `search-layers-tarfile` - Search tarball image layers
- `search-layers-docker` - Search local Docker image layers

## Pipeline Infrastructure

### uri.py

Location: `occystrap/uri.py`

Parses URI-style specifications for inputs, outputs, and filters.

### pipeline.py

Location: `occystrap/pipeline.py`

`PipelineBuilder` class that constructs input -> filter chain -> output
pipelines from URI specifications.
