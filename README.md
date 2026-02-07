# Occy Strap

Occy Strap is a simple set of Docker and OCI container tools, which can be used
either for container forensics or for implementing an OCI orchestrator,
depending on your needs. This is a very early implementation, so be braced for
impact.

## Quick Start with URI-Style Commands

The recommended way to use Occy Strap is with the new URI-style `process` and
`search` commands:

```
# Download from registry to tarball
occystrap process registry://docker.io/library/busybox:latest tar://busybox.tar

# Download from registry to directory
occystrap process registry://docker.io/library/centos:7 dir://centos7

# Export from local Docker to tarball with timestamp normalization
occystrap process docker://myimage:v1 tar://output.tar -f normalize-timestamps

# Search for files in an image
occystrap search registry://docker.io/library/busybox:latest "bin/*sh"
```

## The `process` Command

The `process` command takes a source URI, a destination URI, and optional
filters:

```
occystrap process SOURCE DESTINATION [-f FILTER]...
```

### Input URI Schemes

- `registry://HOST/IMAGE:TAG` - Docker/OCI registry
- `docker://IMAGE:TAG` - Local Docker daemon
- `tar:///path/to/file.tar` - Docker-save format tarball

### Output URI Schemes

- `tar:///path/to/output.tar` - Create tarball
- `dir:///path/to/directory` - Extract to directory
- `oci:///path/to/bundle` - Create OCI runtime bundle
- `mounts:///path/to/directory` - Create overlay mounts
- `docker://IMAGE:TAG` - Load into local Docker daemon
- `registry://HOST/IMAGE:TAG` - Push to Docker/OCI registry

### URI Options

Options can be passed as query parameters:

```
# Extract with unique names and expansion
occystrap process registry://docker.io/library/busybox:latest \
    "dir://merged?unique_names=true&expand=true"

# Use custom Docker socket
occystrap process "docker://myimage:v1?socket=/run/podman/podman.sock" \
    tar://output.tar
```

### Filters

Filters transform or inspect image elements as they pass through the pipeline:

```
# Normalize timestamps for reproducible builds
occystrap process registry://docker.io/library/busybox:latest \
    tar://busybox.tar -f normalize-timestamps

# Normalize with custom timestamp
occystrap process registry://docker.io/library/busybox:latest \
    tar://busybox.tar -f "normalize-timestamps:ts=1609459200"

# Search while creating output (prints matches AND creates tarball)
occystrap process registry://docker.io/library/busybox:latest \
    tar://busybox.tar -f "search:pattern=*.conf"

# Chain multiple filters
occystrap process registry://docker.io/library/busybox:latest \
    tar://busybox.tar -f normalize-timestamps -f "search:pattern=bin/*"

# Record layer metadata to a JSONL file (inspect filter)
occystrap process docker://myimage:v1 registry://myregistry/myimage:v1 \
    -f "inspect:file=layers-before.jsonl" \
    -f normalize-timestamps \
    -f "inspect:file=layers-after.jsonl"

# Exclude files matching glob patterns from layers
occystrap process registry://docker.io/library/python:3.11 \
    tar://python.tar -f "exclude:pattern=**/.git/**"

# Exclude multiple patterns (comma-separated)
occystrap process registry://docker.io/library/python:3.11 \
    tar://python.tar -f "exclude:pattern=**/.git/**,**/__pycache__/**,**/*.pyc"

# Load image directly into local Docker daemon
occystrap process registry://docker.io/library/busybox:latest \
    docker://busybox:latest

# Load into Podman
occystrap process registry://docker.io/library/busybox:latest \
    "docker://busybox:latest?socket=/run/podman/podman.sock"

# Push image to a registry
occystrap process docker://myimage:v1 \
    registry://myregistry.example.com/myuser/myimage:v1

# Push to registry with authentication
occystrap --username myuser --password mytoken \
    process tar://image.tar registry://ghcr.io/myorg/myimage:latest

# Push with zstd compression (better ratio, requires Docker 20.10+/containerd 1.5+)
occystrap --compression=zstd \
    process docker://myimage:v1 registry://myregistry.example.com/myimage:v1
```

## The `search` Command

Search for files in container image layers:

```
occystrap search SOURCE PATTERN [--regex] [--script-friendly]
```

Examples:

```
# Search registry image
occystrap search registry://docker.io/library/busybox:latest "bin/*sh"

# Search local Docker image
occystrap search docker://myimage:v1 "*.conf"

# Search tarball with regex
occystrap search --regex tar://image.tar ".*\.py$"

# Machine-parseable output
occystrap search --script-friendly registry://docker.io/library/busybox:latest "*sh"
```

## Legacy Commands (Deprecated)

The following commands are deprecated but still work for backwards
compatibility. They will be removed in a future version.

### Downloading an image from a repository and storing as a tarball

Let's say we want to download an image from a repository and store it as a
local tarball. This is a common thing to want to do in airgapped environments
for example. You could do this with docker with a `docker pull; docker save`.
The Occy Strap equivalent is:

```
occystrap fetch-to-tarfile registry-1.docker.io library/busybox latest busybox.tar
```

**New equivalent:**
```
occystrap process registry://registry-1.docker.io/library/busybox:latest tar://busybox.tar
```

In this example we're pulling from the Docker Hub (registry-1.docker.io), and
are downloading busybox's latest version into a tarball named `busybox.tar`.
This tarball can be loaded with `docker load -i busybox.tar` on an airgapped
Docker environment.

### Repeatable builds with normalized timestamps

To make builds more repeatable, you can normalize file access and modification
times in the image layers. This is useful when you want to ensure that the
same image content produces the same tarball hash, regardless of when the
files were originally created:

```
occystrap fetch-to-tarfile --normalize-timestamps registry-1.docker.io library/busybox latest busybox.tar
```

**New equivalent:**
```
occystrap process registry://registry-1.docker.io/library/busybox:latest tar://busybox.tar -f normalize-timestamps
```

This will set all timestamps in the layer tarballs to 0 (Unix epoch: January
1, 1970). You can also specify a custom timestamp:

```
occystrap fetch-to-tarfile --normalize-timestamps --timestamp 1609459200 registry-1.docker.io library/busybox latest busybox.tar
```

**New equivalent:**
```
occystrap process registry://registry-1.docker.io/library/busybox:latest tar://busybox.tar -f "normalize-timestamps:ts=1609459200"
```

When timestamps are normalized, the layer SHAs are recalculated and the
manifest is updated to reflect the new hashes. This ensures the tarball
structure remains consistent and valid.

### Downloading an image from a repository and storing as an extracted tarball

The format of the tarball in the previous example is two JSON configuration
files and a series of image layers as tarballs inside the main tarball. You
can write these elements to a directory instead of to a tarball if you'd like
to inspect them:

```
occystrap fetch-to-extracted registry-1.docker.io library/centos 7 centos7
```

**New equivalent:**
```
occystrap process registry://registry-1.docker.io/library/centos:7 dir://centos7
```

### Downloading an image to a merged directory

In scenarios where image layers are likely to be reused between images, you
can save disk space by downloading images to a directory which contains more
than one image:

```
occystrap fetch-to-extracted --use-unique-names registry-1.docker.io \
    homeassistant/home-assistant latest merged_images
```

**New equivalent:**
```
occystrap process registry://registry-1.docker.io/homeassistant/home-assistant:latest \
    "dir://merged_images?unique_names=true"
```

### Storing an image tarfile in a merged directory

Sometimes you have image tarfiles instead of images in a registry:

```
occystrap tarfile-to-extracted --use-unique-names file.tar merged_images
```

**New equivalent:**
```
occystrap process tar://file.tar "dir://merged_images?unique_names=true"
```

### Exploring the contents of layers and overwritten files

If you'd like the layers to be expanded from their tarballs to the filesystem:

```
occystrap fetch-to-extracted --expand quay.io \
    ukhomeofficedigital/centos-base latest ukhomeoffice-centos
```

**New equivalent:**
```
occystrap process registry://quay.io/ukhomeofficedigital/centos-base:latest \
    "dir://ukhomeoffice-centos?expand=true"
```

### Generating an OCI runtime bundle

```
occystrap fetch-to-oci registry-1.docker.io library/hello-world latest bar
```

**New equivalent:**
```
occystrap process registry://registry-1.docker.io/library/hello-world:latest oci://bar
```

### Searching image layers for files

```
occystrap search-layers registry-1.docker.io library/busybox latest "bin/*sh"
```

**New equivalent:**
```
occystrap search registry://registry-1.docker.io/library/busybox:latest "bin/*sh"
```

### Working with local Docker or Podman daemon

```
occystrap docker-to-tarfile library/busybox latest busybox.tar
```

**New equivalent:**
```
occystrap process docker://library/busybox:latest tar://busybox.tar
```

For Podman:
```
occystrap process "docker://myimage:latest?socket=/run/podman/podman.sock" tar://output.tar
```

Note: Podman doesn't run a daemon by default. You need to start the socket
service first:

```
# For rootless Podman
systemctl --user start podman.socket

# For rootful Podman
sudo systemctl start podman.socket
```

## Authenticating with private registries

To fetch images from private registries (such as GitLab Container Registry,
AWS ECR, or private Docker Hub repositories), use the `--username` and
`--password` global options:

```
occystrap --username myuser --password mytoken \
    process registry://registry.gitlab.com/mygroup/myimage:latest tar://output.tar
```

You can also use environment variables to avoid putting credentials on the
command line:

```
export OCCYSTRAP_USERNAME=myuser
export OCCYSTRAP_PASSWORD=mytoken
occystrap process registry://registry.gitlab.com/mygroup/myimage:latest tar://output.tar
```

For GitLab Container Registry, the username is typically your GitLab username
and the password is a personal access token with `read_registry` scope.

## Layer Compression

When pushing images to registries, occystrap supports both gzip (default) and
zstd compression for image layers:

```
# Use gzip (default, maximum compatibility)
occystrap process docker://myimage:v1 registry://myregistry/myimage:v1

# Use zstd for better compression ratio and speed
occystrap --compression=zstd process docker://myimage:v1 registry://myregistry/myimage:v1
```

You can also set the compression via environment variable:

```
export OCCYSTRAP_COMPRESSION=zstd
occystrap process docker://myimage:v1 registry://myregistry/myimage:v1
```

Or via URI query parameter:

```
occystrap process docker://myimage:v1 "registry://myregistry/myimage:v1?compression=zstd"
```

**Compatibility notes:**
- **gzip** (default): Works with all Docker/container runtimes
- **zstd**: Requires Docker 20.10+ or containerd 1.5+ on the pulling client;
  offers ~30% better compression ratio and faster compression

When pulling images, occystrap automatically detects and handles both gzip and
zstd compressed layers from registries or OCI tarballs.

## Supporting non-default architectures

Docker image repositories can store multiple versions of a single image, with
each image corresponding to a different (operating system, cpu architecture,
cpu variant) tuple. Occy Strap supports letting you specify which to use with
global command line flags. Occy Strap defaults to linux amd64 if you don't
specify something different:

```
occystrap --os linux --architecture arm64 --variant v8 \
    process registry://registry-1.docker.io/library/busybox:latest dir://busybox
```

Or via URI query parameters:

```
occystrap process "registry://registry-1.docker.io/library/busybox:latest?os=linux&arch=arm64&variant=v8" \
    dir://busybox
```

## Development

### Install for Development

```
pip install -e ".[test]"
```

### Pre-commit Hooks

This project uses pre-commit hooks to validate code before commits. Install them
with:

```
pip install pre-commit
pre-commit install
```

The hooks run:
- `actionlint` - GitHub Actions workflow validation
- `shellcheck` - Shell script linting
- `tox -eflake8` - Python code style checks
- `tox -epy3` - Unit tests

To run the hooks manually:

```
pre-commit run --all-files
```

### Running Tests

Unit tests are in `occystrap/tests/` and can be run with:

```
tox -epy3
```

Functional tests are in `deploy/occystrap_ci/tests/` and are run in CI.

### Releasing

Releases are automated via GitHub Actions. Push a version tag to trigger the
pipeline:

```
git tag -s v0.5.0 -m "Release v0.5.0"
git push origin v0.5.0
```

The workflow builds the package, signs the tag with Sigstore, publishes to
PyPI, and creates a GitHub Release. See [RELEASE-SETUP.md](RELEASE-SETUP.md)
for one-time configuration steps.

## Developer Automation

This project supports automated CI helpers via PR comments. To use these
commands, comment on a pull request with one of the following:

- `@shakenfist-bot please retest` - Re-run the functional test suite
- `@shakenfist-bot please attempt to fix` - Have Claude Code attempt to fix
  test failures
- `@shakenfist-bot please re-review` - Request another automated code review
- `@shakenfist-bot please address comments` - Have Claude Code address the
  automated review comments

These commands are only available to repository collaborators with write access.

## Documentation

For more detailed documentation, see the [docs/](docs/) directory:

- [Installation](docs/installation.md) - Getting started guide
- [Command Reference](docs/command-reference.md) - Complete CLI reference
- [Pipeline Architecture](docs/pipeline.md) - How the pipeline works
- [Use Cases](docs/use-cases.md) - Common scenarios and examples
