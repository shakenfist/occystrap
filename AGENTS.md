# Agents Guide

This document provides guidance for AI agents working on the occystrap codebase.

## Project Overview

Occystrap is a Docker/OCI container image processing tool that follows an
input -> filter -> output pipeline pattern. It can fetch images from registries,
local Docker daemons, or tarballs, process them through filters, and write to
various output formats.

## Where the documentation lives

Three documents cover most of what an agent needs:

- [ARCHITECTURE.md](ARCHITECTURE.md) — the module inventory and directory
  structure, plus an index into the rest.
- [docs/pipeline.md](docs/pipeline.md) — the pipeline, element types, and
  the input, filter and output interface contracts.
- [docs/internals.md](docs/internals.md) — Quay discovery, the Docker
  integrations, the proxy, parallelism, caching and the HTTP layer.

[docs/index.md](docs/index.md) is the full index; go there for anything
else rather than looking for a second list in this file. New user-visible
documentation belongs in `docs/`; this file and `ARCHITECTURE.md` are a
summary and an index into it.

## Key Patterns

### Adding a New Filter

1. Create a new file in `occystrap/filters/` (e.g., `myfilter.py`)
2. Subclass `ImageFilter` from `occystrap.filters.base`
3. Implement `process_image_element(element)` taking an `ImageElement`
4. Export from `occystrap/filters/__init__.py`
5. Register in `PipelineBuilder.build_filter()` in `occystrap/pipeline.py`

Template for a filter that modifies layers:

```python
from occystrap import constants
from occystrap.filters.base import ImageFilter

class MyFilter(ImageFilter):
    def __init__(self, wrapped_output, option=None,
                 temp_dir=None, diff_id_map=None):
        super().__init__(wrapped_output, temp_dir=temp_dir,
                         diff_id_map=diff_id_map)
        self.option = option

    def process_image_element(self, element):
        if element.element_type == constants.CONFIG_FILE:
            self._buffer_config(element)
        elif (element.element_type == constants.IMAGE_LAYER
                and element.data is not None):
            # Process the layer, return modified data and new name
            new_data, new_name = self._process_layer(element.data)
            self._record_new_diff_id(
                new_name, element.layer_index,
                original_hex=element.name)
            try:
                self._wrapped.process_image_element(
                    constants.ImageElement(
                        element.element_type, new_name,
                        new_data,
                        layer_index=element.layer_index))
            finally:
                # Clean up temporary files
                pass
        else:
            if element.element_type == constants.IMAGE_LAYER:
                self._skip_layer(element.layer_index)
            self._wrapped.process_image_element(element)
```

Content-modifying filters must accept `diff_id_map` and forward it to the
base class. This enables cross-image diff_id tracking in proxy mode (see
`_forward_buffered_config` in `filters/base.py`). The `original_hex`
parameter to `_record_new_diff_id` records the `original -> filtered`
mapping. Register the `diff_id_map` kwarg in `PipelineBuilder.build_filter`.

### Adding a New Input Source

1. Create a new file in `occystrap/inputs/`
2. Subclass `ImageInput` from `occystrap.inputs.base`
3. Implement `image`, `tag` properties and `fetch()` method
4. Register in `PipelineBuilder.build_input()` in `occystrap/pipeline.py`

### Multi-Image Input (quay:// pattern)

The `quay://` scheme is a **multi-image resolver**, not an `ImageInput`
subclass. It resolves a single URI into a list of `(registry, image, tag)`
tuples, then the command (`info` or `process`) loops over them, creating a
standard `registry.Image` input for each. The `PipelineBuilder` has no
knowledge of `quay://` — resolution happens before the pipeline is built.

Key files:
- `occystrap/quay.py` - `QuayClient` (API v1 wrapper) and `resolve_quay_uri()`
- `occystrap/uri.py` - `parse_quay_uri()` and `quay` in `INPUT_SCHEMES`
- `occystrap/main.py` - `_resolve_quay_images()`, `_info_multi()`,
  `_process_multi()`

To add a similar multi-image scheme for another registry (e.g., Docker Hub,
ghcr.io), follow the same pattern: add a client module, a URI parser, a
resolver function, and detect the scheme in `_resolve_quay_images()` (or
rename it to a more generic helper).

### Adding a New Output Writer

1. Create a new file in `occystrap/outputs/`
2. Subclass `ImageOutput` from `occystrap.outputs.base`
3. Implement `fetch_callback()`, `process_image_element()`, `finalize()`
4. Optionally override `verify(full=False)` for post-write output verification.
   Record expectations during `process_image_element()` and check them in
   `verify()`. See `DirWriter` for a reference implementation.
5. Register in `PipelineBuilder.build_output()` in `occystrap/pipeline.py`
6. Add the scheme to `OUTPUT_SCHEMES` in `occystrap/uri.py`

## Build System

The project uses `pyproject.toml` with `setuptools` and `setuptools_scm`
for building and versioning. Versions are derived from git tags. There is
no `setup.py` or `setup.cfg`. Dependencies are declared in `pyproject.toml`
under `[project.dependencies]` and `[project.optional-dependencies.test]`.

## Testing

- **Unit tests**: Located in `occystrap/tests/`. Run with `tox -epy3`.
- **Functional tests**: Located in `deploy/occystrap_ci/tests/`. Run in CI.

### Pre-commit Hooks

The project uses pre-commit hooks for `actionlint` (GitHub Actions
validation), `shellcheck` (shell script linting), `check-log-levels`
(enforces max LOG.info() calls per file), `tox -eflake8` (linting),
and `tox -epy3` (unit tests). Install with `pre-commit install`.

## Common Tasks

- **Search for files in layers**: Use `SearchFilter` as reference
- **Modify layer contents**: Use `TimestampNormalizer` or `ExcludeFilter` as
  reference (they rewrite tarballs)
- **Record layer metadata**: Use `InspectFilter` as reference (accumulates
  state across elements and writes output in `finalize()`)
- **Passthrough filter**: Check element type, process if needed, always call
  `self._wrapped.process_image_element()` to pass data through
- **Write to Docker daemon**: Use `DockerWriter` as reference (builds tarball
  in memory and posts via API)
- **Push to registry**: Use `RegistryWriter` as reference (uploads blobs and
  manifest via Docker Registry HTTP API V2)
- **Layer caching**: Use `LayerCache` in `layer_cache.py` for cross-invocation
  caching of processed layers. Integrated into `RegistryWriter` via
  `fetch_callback` (skip cached layers) and `_compress_and_upload_layer`
  (record new entries). Cache is filter-aware via `filters_hash`.
- **Handle layer compression**: Use `compression.py` module for detecting and
  handling gzip/zstd compressed layers. Media type constants are in `constants.py`.
- **Make HTTP requests to registries**: Use `util.request_url()` with per-thread
  httpx clients obtained via `self._get_thread_client()`. Classes that use
  `ThreadPoolExecutor` for parallel I/O should inherit `util.ThreadSafeClientMixin`
  and call `self._init_thread_clients()` in `__init__()` after setting
  `self._client`, `self._rate_limiter`, and `self._own_client`. Do NOT share a
  single `httpx.Client` across threads — it is not thread-safe. For streaming
  responses, the caller must call `r.close()` when done and use `r.iter_bytes()`
  (not `r.iter_content()` as in the old requests API). Docker daemon code
  (`inputs/docker.py`, `inputs/dockerpush.py`, `outputs/docker.py`) still uses
  `requests-unixsocket` for Unix socket access — do not convert those to httpx.
- **Add HTTP server endpoints**: Any `BaseHTTPRequestHandler` subclass must
  inherit from `SafeHeaderMixin` (in `util.py`) as the first base class.
  This strips `\r`/`\n` from header values to prevent HTTP response splitting.
  Additionally, wrap user-controlled values in `sanitize_header_value()`
  at each call site so CodeQL sees the sanitization on the data flow path.
- **Construct file paths from user input**: Use `safe_path_join()` from
  `util.py` instead of bare `os.path.join()` when any path component comes
  from external data (image names, tags, digests, layer paths). This
  validates the resolved path stays within the base directory, preventing
  path traversal (CWE-22).
- **Add new compression format**: Extend `compression.py` with detection magic,
  `StreamingDecompressor`/`StreamingCompressor` classes, and media type mapping
- **Access image metadata without downloading layers**: Use
  `get_manifest()` and `get_config()` on the input source. Registry inputs
  provide both, docker/tarfile inputs provide config only, dockerpush provides
  neither. See `_build_info()` in `main.py` for an example of handling all
  cases gracefully.
- **Add a new metadata-only command**: Follow the `info` command pattern --
  take a `SOURCE` argument, use `PipelineBuilder.build_input()` to get an
  input, call `get_manifest()`/`get_config()` for metadata, and use the
  `-O`/`--output-format` global option for text/JSON output
- **Add a new validation command**: Follow the `check` command pattern --
  use `check.py` module with `CheckResults` accumulator for errors/warnings/
  info, separate metadata checks (fast mode) from layer checks (full mode),
  and exit non-zero on errors for CI integration
- **Add a new standalone command**: Follow the `proxy` command pattern in
  `main.py` (`proxy_cmd`) -- add a Click command that doesn't use the
  standard `process` SOURCE/DESTINATION arguments. The proxy command builds
  its own pipeline per received image using `PipelineBuilder` directly
- **Extend the proxy**: The proxy (`proxy.py`) processes images concurrently
  (multiple manifest PUTs run in parallel, limited by a semaphore). Blob
  reference counting prevents shared blobs from being deleted while still
  in use. `LayerCache` is internally thread-safe. To add new proxy features,
  ensure shared state mutations are under `state.lock`
- **Pull-through proxy**: When `--upstream` is set, the proxy also handles
  GET requests. `_handle_pull_manifest` checks downstream cache first, then
  fetches from upstream on miss. `_handle_pull_blob` proxies blob GETs from
  downstream. Per-image locks (`pull_locks`) prevent duplicate upstream
  fetches. `_build_output_pipeline()` and `_run_pipeline()` are shared
  between push and pull paths. Cached `input_registry.Image` instances
  in `state.downstream_images` provide authenticated downstream reads
- **Create a synthetic input**: Follow `_ProxyInput` in `proxy.py` as a
  reference for creating an `ImageInput` subclass that yields
  `ImageElement`s from data already in memory or on disk (rather than
  fetching from a remote source)

### CliRunner and JSON Output

Click 8.2+ changed `result.output` to be a mix of stdout and
stderr. Tests that parse structured output (JSON) from CLI
commands must use `result.stdout` (stdout only) instead of
`result.output` (mixed). Use `result.output` only for
human-readable assertions where log messages mixed in are
acceptable.

## Logging Conventions

All modules use `shakenfist_utilities.logs.setup_console(__name__)`
for logger initialization. The returned `ConsoleAdapter` supports
`with_fields()` for structured key-value output.

**Log level policy:**
- **INFO**: Milestones only -- pipeline start/end, summary statistics,
  layer counts. Each file should have at most 10 `LOG.info()` calls
  (enforced by the `check-log-levels` pre-commit hook).
- **DEBUG**: Per-layer, per-request, per-blob operations.

**Structured summaries:** Use `LOG.with_fields({...}).info(...)` for
end-of-pipeline summary lines (see `outputs/base.py:_log_summary()`
and `outputs/registry.py:finalize()` for examples).

**Progress bars:** Use `LayerProgress` from `occystrap/progress.py`
for long-running loops (downloads, uploads). It auto-detects TTY
and falls back to periodic log messages in non-TTY environments.

## CI/CD Automation Tools

The `tools/` directory contains scripts for automated PR workflows:

- **address-comments-with-claude.sh**: Processes review items and creates
  commits for fixes. Called by `pr-address-comments.yml`
- **render-review.py**: Converts review JSON to formatted markdown, and
  validates it against **review-schema.json** in `--validate` mode

Generating the review is not done here. `pr-re-review.yml` and the
automated reviewer in CI both call the shared action
`shakenfist/actions/review-pr-with-claude@main`, and the per-project
copies of that script were deleted once they had no callers left.

The bot-triggered workflows in `.github/workflows/`:

- `pr-retest.yml` - Re-run tests via `@shakenfist-bot please retest`
- `pr-fix-tests.yml` - Fix test failures via `@shakenfist-bot please attempt to fix`
- `pr-re-review.yml` - Re-review PR via `@shakenfist-bot please re-review`
- `pr-address-comments.yml` - Address review comments via
  `@shakenfist-bot please address comments`
