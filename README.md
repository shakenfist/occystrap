# Occy Strap

Occy Strap is a Docker/OCI container image manipulation toolkit that
lets you work with container images without requiring Docker to be
installed. It follows a flexible **input -> filter -> output** pipeline:
download images from registries, local Docker/Podman daemons, or
tarballs; transform them with filters (normalize timestamps for
reproducible builds, exclude files, search for content); and export
them to tarballs, directories, OCI runtime bundles, or push them to a
registry. A persistent filtering registry proxy supports batch builds
and pull-through caching.

It will mostly be of interest to people building container images in
CI, mirroring images into airgapped environments, or wanting
reproducible image builds.

## Installation

```bash
pip install occystrap
```

See [docs/installation.md](https://github.com/shakenfist/occystrap/blob/develop/docs/installation.md)
for details and verification steps.

## Usage

```bash
# Download from registry to tarball
occystrap process registry://docker.io/library/busybox:latest tar://busybox.tar

# Download from registry to directory
occystrap process registry://docker.io/library/busybox:latest dir://busybox

# Export from local Docker to tarball with timestamp normalization
occystrap process docker://myimage:v1 tar://output.tar -f normalize-timestamps

# Search for files in an image
occystrap search registry://docker.io/library/busybox:latest "bin/*sh"

# Inspect image metadata
occystrap info registry://docker.io/library/busybox:latest

# Validate an image end to end
occystrap check registry://docker.io/library/busybox:latest
```

See the [command reference](https://github.com/shakenfist/occystrap/blob/develop/docs/command-reference.md)
for all commands, URI schemes, filters, authentication, compression,
verification, the layer cache, the registry proxy, and the legacy
command mappings.

## Documentation

In the [docs/](https://github.com/shakenfist/occystrap/blob/develop/docs/index.md)
directory:

- [Documentation Index](https://github.com/shakenfist/occystrap/blob/develop/docs/index.md) - Overview and key concepts
- [Installation](https://github.com/shakenfist/occystrap/blob/develop/docs/installation.md) - Getting started guide
- [Command Reference](https://github.com/shakenfist/occystrap/blob/develop/docs/command-reference.md) - Complete CLI reference
- [Use Cases](https://github.com/shakenfist/occystrap/blob/develop/docs/use-cases.md) - Common scenarios and examples, including airgapped mirroring and Podman
- [Performance Tuning](https://github.com/shakenfist/occystrap/blob/develop/docs/performance.md) - Parallelism, rate limiting, retries, and benchmarking
- [Pipeline Architecture](https://github.com/shakenfist/occystrap/blob/develop/docs/pipeline.md) - How inputs, filters, and outputs compose
- [Development](https://github.com/shakenfist/occystrap/blob/develop/docs/development.md) - Setup, tests, releasing, and the PR bot commands
- [Docker Tarball Formats](https://github.com/shakenfist/occystrap/blob/develop/docs/docker-tarball-formats.md) - Docker save tarball format reference

Project reference files:

- [ARCHITECTURE.md](https://github.com/shakenfist/occystrap/blob/develop/ARCHITECTURE.md) - Modules, commands, and pipeline components
- [AGENTS.md](https://github.com/shakenfist/occystrap/blob/develop/AGENTS.md) - Guide for AI coding assistants
- [IMAGE_STORES.md](https://github.com/shakenfist/occystrap/blob/develop/IMAGE_STORES.md) - Image storage format notes

This project includes Claude Code skills in `.claude/skills/` covering
documentation updates, testing discipline, and PR preparation.

## License

Apache License 2.0. See
[LICENSE](https://github.com/shakenfist/occystrap/blob/develop/LICENSE).
