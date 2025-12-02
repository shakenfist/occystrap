# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build and Development Commands

```bash
# Install in development mode
pip install -e .

# Run linting (checks only files changed since HEAD~1)
tox -eflake8 -- -HEAD

# Run linting on all files
flake8 --max-line-length=120 occystrap/

# Run tests (uses stestr)
tox

# Run a single test
stestr run test_name
```

## What Occystrap Does

Occystrap is a Docker/OCI container image tool for:
- Downloading images from registries without Docker installed
- Creating docker-loadable tarballs for airgapped environments
- Extracting and inspecting image layers
- Generating OCI runtime bundles for runc
- Managing shared image directories (deduplicated layers across multiple images)

## Architecture

The codebase follows an input/output pipeline pattern:

**Input sources** (image fetchers):
- `docker_registry.py` - Fetches images from Docker/OCI registries via HTTP API
- `input_tarfile.py` - Reads from existing docker-save tarballs

**Output writers** (all implement the same interface):
- `output_tarfile.py` - Creates docker-loadable tarballs (v1.2 format)
- `output_directory.py` - Extracts to directory with optional layer deduplication
- `output_ocibundle.py` - Creates OCI runtime bundles for runc
- `output_mounts.py` - Creates overlay mount-based extraction

**Shared interface pattern**: All outputs implement:
- `fetch_callback(digest)` - Returns whether a layer should be fetched
- `process_image_element(type, name, data)` - Handles CONFIG_FILE or IMAGE_LAYER
- `finalize()` - Writes manifest and completes output

**Element types** (defined in `constants.py`):
- `CONFIG_FILE` - Image configuration JSON
- `IMAGE_LAYER` - Tarball containing filesystem layer

**Main CLI** (`main.py`): Click-based CLI with commands:
- `fetch-to-tarfile` - Registry → tarball
- `fetch-to-extracted` - Registry → directory
- `fetch-to-oci` - Registry → OCI bundle
- `fetch-to-mounts` - Registry → overlay mounts
- `recreate-image` - Shared directory → tarball
- `tarfile-to-extracted` - Tarball → directory
- `search-layers` - Search file paths in registry image layers
- `search-layers-tarfile` - Search file paths in tarball image layers

**Search module** (`search.py`): Implements `LayerSearcher` which follows the output interface pattern but searches layers for matching paths instead of writing them.

## Key Concepts

**Whiteout files**: OCI layers use `.wh.<filename>` to mark deletions and `.wh..wh..opq` for opaque directories. These are processed in `output_directory.py` when `--expand` is used.

**Unique names mode**: `--use-unique-names` enables storing multiple images in one directory by prefixing manifest files with image/tag names. A `catalog.json` tracks which layers belong to which images.

**Timestamp normalization**: `--normalize-timestamps` rewrites layer tar mtimes for reproducible builds, recalculating layer SHAs.

**Registry authentication**: Use `--username` and `--password` global options (or `OCCYSTRAP_USERNAME`/`OCCYSTRAP_PASSWORD` environment variables) to authenticate with private registries like GitLab Container Registry.

## CI Tests

Integration tests in `deploy/occystrap_ci/tests/` run against a local registry (`localhost:5000`) and test whiteout handling, OCI bundle creation, and timestamp normalization.
