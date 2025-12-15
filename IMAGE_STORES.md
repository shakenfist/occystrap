# Image Stores

This document describes the image store components and their roles in the
Occystrap codebase.

## Input Image Stores

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
- `docker-to-tarfile` - Local Docker daemon to tarball
- `docker-to-extracted` - Local Docker daemon to directory
- `docker-to-oci` - Local Docker daemon to OCI bundle
- `search-layers` - Search registry image layers
- `search-layers-tarfile` - Search tarball image layers
- `search-layers-docker` - Search local Docker image layers
