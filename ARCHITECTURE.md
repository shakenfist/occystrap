# Architecture

Occystrap follows an input -> filter -> output pipeline pattern for processing
container images.

## Directory Structure

```
occystrap/
    __init__.py
    main.py              # CLI entry point (Click-based)
    check.py             # Image validation checks for the check command
    constants.py         # Element/compression type constants, media types
    compression.py       # Compression utilities (gzip/zstd detection & streaming)
    common.py            # Shared utilities
    util.py              # Additional utilities
    uri.py               # URI parsing for pipeline specification
    pipeline.py          # Pipeline builder from URIs
    quay.py              # Quay.io API v1 client and multi-image resolver
    proxy.py             # Persistent filtering registry proxy server
    layer_cache.py       # Cross-invocation layer cache for registry push
    progress.py          # tqdm progress bars with non-TTY fallback
    docker_extract.py    # Layer extraction utilities
    inputs/              # Input source modules
        __init__.py
        base.py          # ImageInput abstract base class
        docker.py        # Fetches images from local Docker daemon
        dockerpush.py    # Fetches via embedded registry + Docker push
        registry.py      # Fetches images from Docker/OCI registries
        tarfile.py       # Reads from docker-save tarballs
    filters/             # Filter modules (transform/inspect pipeline)
        __init__.py
        base.py          # ImageFilter abstract base class
        exclude.py       # Exclude files matching glob patterns from layers
        inspect.py       # Record layer metadata to JSONL files
        normalize_timestamps.py  # Timestamp normalization for reproducible builds
        search.py        # Search for files matching patterns
    outputs/             # Output writer modules
        __init__.py
        base.py          # ImageOutput abstract base class
        docker.py        # Loads images into local Docker daemon
        registry.py      # Pushes images to Docker/OCI registries
        tarfile.py       # Creates docker-loadable tarballs
        directory.py     # Extracts to directory with deduplication
        ocibundle.py     # Creates OCI runtime bundles
        mounts.py        # Creates overlay mount-based extraction
    tests/               # Unit tests (run with tox -epy3)
        __init__.py
        test_compression.py
        test_check.py
        test_info.py
        test_inspect.py
        test_registry_output.py
        test_layer_cache.py
        test_dockerpush.py
        test_proxy.py
        test_quay.py
        test_tarformat.py

deploy/
    occystrap_ci/
        tests/           # Functional tests (run in CI)
            test_check.py
            test_dir_deep_images.py
            test_docker_input.py
            test_docker_output.py
            test_exclude_filter.py
            test_filter_chaining.py
            test_info.py
            test_inspect_filter.py
            test_normalize_timestamps.py
            test_oci_hello_world.py
            test_quay_bulk.py
            test_registry_push.py
            test_search_layers.py
            test_whiteout.py

pyproject.toml               # Build config (setuptools + setuptools_scm)
tox.ini                      # Test runner configuration

tools/
    benchmark.sh             # Performance benchmark script
    check-log-levels.sh      # Pre-commit log verbosity checker

.github/
    workflows/
        codeql-analysis.yml    # CodeQL security scanning
        functional-tests.yml   # CI functional tests
        python-unit-tests.yml  # CI unit tests
        release.yml            # Automated PyPI release pipeline
    actionlint.yaml            # actionlint configuration
```

## Pipeline pattern

Every operation is a pipeline: an **input source** yields image
elements, zero or more **filters** transform or drop them, and an
**output writer** consumes what survives. Sources, filters and writers
know nothing about each other, so any source composes with any writer.

The element types, the full source and writer inventory, the filter
capabilities and chaining rules, whiteout handling, layer deduplication,
deterministic compression, out-of-order delivery and the security
sanitisation rules are all documented in
[docs/pipeline.md](docs/pipeline.md).

The `info` and `check` commands are read-only consumers of the same
sources; their output is described in
[docs/command-reference.md](docs/command-reference.md).

## Command surface

`occystrap process SOURCE DESTINATION [-f FILTER]...` takes URI-style
arguments — `registry://`, `quay://`, `docker://`, `dockerpush://`,
`tar://`, `dir://`, `oci://` and friends, each with its own query
parameters. The complete grammar, every flag and worked examples are in
[docs/command-reference.md](docs/command-reference.md); which tar format
to pick is [docs/tar-format-selection.md](docs/tar-format-selection.md).

## Where the detail lives

| Topic | Document |
|-------|----------|
| The pipeline, its elements, sources, filters and writers | [docs/pipeline.md](docs/pipeline.md) |
| Every command, flag and URI | [docs/command-reference.md](docs/command-reference.md) |
| Quay discovery, Docker integrations, the proxy, parallelism, HTTP | [docs/internals.md](docs/internals.md) |
| Tuning parallelism and connection reuse | [docs/performance.md](docs/performance.md) |
| Docker tarball format details | [docs/docker-tarball-formats.md](docs/docker-tarball-formats.md) |
| Choosing a tar format | [docs/tar-format-selection.md](docs/tar-format-selection.md) |
| What people use it for | [docs/use-cases.md](docs/use-cases.md) |
| Installing it | [docs/installation.md](docs/installation.md) |
| Building and testing | [docs/development.md](docs/development.md) |

[docs/index.md](docs/index.md) is the full index.
