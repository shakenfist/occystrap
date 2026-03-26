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

## Pipeline Pattern

The pipeline follows a decorator pattern where filters wrap outputs:

```
Input Source -> Filter Chain -> Output Writer -> Files
     |              |                |
   fetch()    process_image_element()  finalize()
```

### Input Sources

All input sources inherit from the `ImageInput` abstract base class defined in
`inputs/base.py`. This ABC defines the interface:

- `image` (property) - Returns the image name
- `tag` (property) - Returns the image tag
- `fetch(fetch_callback, ordered)` - Yields `ImageElement` objects (config
  files and layers). When `ordered=False`, layers may arrive out of manifest
  order with `layer_index` set for reordering at finalize time.
- `get_manifest()` - Returns the distribution manifest dict (OCI/Docker v2)
  without downloading layer blobs. Returns `None` if the input source does
  not have a distribution manifest (e.g. docker, tarfile inputs).
- `get_config()` - Returns the parsed OCI image config dict (architecture,
  OS, rootfs.diff_ids, history, labels, env, etc.) without downloading layer
  blobs. Returns `None` if unavailable (e.g. dockerpush input).

Input source implementations:
- `inputs/docker.py` - Fetches images from local Docker daemon via Unix socket,
  using hybrid streaming to minimize disk usage
- `inputs/dockerpush.py` - Starts an embedded V2 registry on localhost and uses
  Docker's push mechanism to receive layers in parallel; faster than docker.py
  for multi-layer images
- `inputs/registry.py` - Fetches images from Docker/OCI registries via HTTP API,
  with parallel layer downloads using ThreadPoolExecutor
- `inputs/tarfile.py` - Reads from existing docker-save tarballs

### Filters

Filters inherit from `ImageFilter` (in `filters/base.py`) which itself inherits
from `ImageOutput`. This allows filters to be chained together using the
decorator pattern. Each filter wraps another output (or filter) and can:

- Transform element data (e.g., normalize timestamps)
- Transform element names (e.g., recalculate hashes)
- Inspect elements without modification (e.g., search)
- Skip elements entirely
- Accumulate state across elements

**Config diff_id updates:** Filters that modify layer content change the layer's
SHA256 hash (diff_id). The `ImageFilter` base class provides helper methods for
keeping the config's `rootfs.diff_ids` in sync:

- `_buffer_config(element)` - Buffers the config element instead of forwarding
  it immediately. The config is forwarded in `finalize()` with updated diff_ids.
- `_record_new_diff_id(sha256_hex, layer_index, original_hex=None)` - Records
  the new SHA256 for a modified layer. When `original_hex` is provided and
  differs from `sha256_hex`, the mapping is also recorded in the cross-image
  `_diff_id_map` for use by subsequent images in proxy mode.
- `_skip_layer(layer_index)` - Advances the layer counter for unmodified
  layers (data=None, skipped by fetch_callback).
- `_forward_buffered_config()` - Called by `finalize()` to update the config's
  `rootfs.diff_ids` and forward it to the wrapped output. For skipped layers
  (not in `_new_diff_ids`), consults `_diff_id_map` to translate original
  diff_ids to their filtered equivalents. If no diff_ids changed, the original
  config is forwarded unchanged.

**Cross-image diff_id mapping:** When the proxy processes child images that
share base layers with a previously pushed parent, Docker only uploads new
layers. The base layers already exist in the downstream registry in their
filtered form. The `_diff_id_map` (a dict passed via `diff_id_map=` to filter
constructors) maps `original_hex -> filtered_hex` across images. In proxy
mode, `_ProxyState.diff_id_map` provides this shared dict. Non-proxy
pipelines use an empty dict (no cross-image state needed).

Content-modifying filters (ExcludeFilter, TimestampNormalizer) use these methods.
Non-modifying filters (InspectFilter, SearchFilter) pass config through unchanged.

Filter implementations:
- `filters/exclude.py` - Excludes files matching glob patterns from layers,
  recalculating layer SHAs. Supports multiple comma-separated patterns.
- `filters/inspect.py` - Records layer metadata (digest, size, created_by,
  tags) to a JSONL file. Passthrough filter that can be placed between
  other filters to measure their effect on layers.
- `filters/normalize_timestamps.py` - Normalizes layer timestamps for
  reproducible builds, recalculating layer SHAs
- `filters/search.py` - Searches layers for files matching glob or regex
  patterns, can operate standalone or as passthrough

### Output Writers

All output writers inherit from the `ImageOutput` abstract base class defined in
`outputs/base.py`. This ABC defines the interface:

- `requires_ordered_layers` (property) - Whether layers must arrive in
  manifest order (default `True`)
- `fetch_callback(digest)` - Returns whether a layer should be fetched
- `process_image_element(element)` - Handles an `ImageElement` (config or
  layer)
- `finalize()` - Writes manifest and completes output
- `verify(full=False)` - Post-write verification. Returns `CheckResults`.
  Default is a no-op. Implementations: `DirWriter` checks manifest, config,
  and layer existence/sizes (full mode validates tar integrity); `TarWriter`
  re-opens the tarball and checks all entries; `DockerWriter` queries the
  Docker API to confirm the image was loaded with the correct digest

The base class also provides summary statistics tracking via
`_log_summary()`, which uses `LOG.with_fields()` for structured
output. All output writers log a summary at the end of processing:

```
Processing complete
    bytes: 12345678
    layers: 5
    elapsed_s: 3.2
```

The registry output provides granular timing with compression and
upload breakdowns via its own `with_fields()` summary in `finalize()`:

```
Push complete
    layers: 40
    elapsed_s: 34.7
    compress_s: 15.8
    upload_s: 4.5
    upload_skipped: 22
    cache_hits: 0
    input_mb: 980.0
    output_mb: 326.3
    ratio_pct: 33
```

Output writer implementations:
- `outputs/docker.py` - Loads images into local Docker/Podman daemon via API
- `outputs/registry.py` - Pushes images to Docker/OCI registries via HTTP API,
  with parallel layer uploads using ThreadPoolExecutor
- `outputs/tarfile.py` - Creates docker-loadable tarballs (v1.2 format)
- `outputs/directory.py` - Extracts to directory with optional layer deduplication;
  uses `safe_path_join()` to prevent path traversal (CWE-22); uses `os.rename()`
  to move temp files to their final location when `temp_path` is available,
  avoiding the read/write copy loop
- `outputs/ocibundle.py` - Creates OCI runtime bundles for runc (inherits from
  DirWriter); uses `safe_path_join()` for user-controlled paths
- `outputs/mounts.py` - Creates overlay mount-based extraction; uses
  `safe_path_join()` for user-controlled paths

### Element Types

Pipeline elements are represented as `ImageElement` dataclass instances
(defined in `constants.py`):

```python
@dataclasses.dataclass
class ImageElement:
    element_type: str   # CONFIG_FILE or IMAGE_LAYER
    name: str           # Filename or digest hash
    data: object        # File-like object or None (skipped)
    layer_index: int | None = None  # Manifest position
    temp_path: str | None = None    # Backing temp file path
```

Element types:
- `CONFIG_FILE` - Image configuration JSON
- `IMAGE_LAYER` - Tarball containing filesystem layer

The `layer_index` field is set when layers are delivered out of order
(i.e., when the output's `requires_ordered_layers` is `False`). Outputs
use this index to reconstruct the correct manifest layer order at
`finalize()` time.

The `temp_path` field carries the path to the backing temp file when the
element's data is backed by a temp file on disk. Outputs that write to a
directory (e.g., `DirWriter`) can use `os.rename()` to move the temp file
to its final location instead of copying data through a read/write loop.
This is O(1) on the same filesystem. When an output moves the temp file,
the input's `finally` block skips the `os.unlink()` (the file no longer
exists at the original path).

### The `info` Command

The `info` command displays image metadata without downloading layer blobs.
It uses `get_manifest()` and `get_config()` on the input source to collect
metadata, then formats it as either human-readable text (with `prettytable`
for per-layer details) or JSON.

The output format is controlled by the global `-O`/`--output-format` option
stored in `ctx.obj['OUTPUT_FORMAT']`.

Key implementation details in `main.py`:
- `_build_info(input_source)` - Collects metadata from manifest and config
  into a dict, handling both full (manifest+config) and partial (config-only)
  cases gracefully
- `_format_size(size_bytes)` - Formats bytes as human-readable string
- `_print_info_text(info)` - Renders info dict as human-readable text

### The `check` Command

The `check` command validates a container image's structural integrity,
history consistency, compression compatibility, and filesystem correctness.
It supports two modes:

- **Fast mode** (`--fast`): Uses only `get_manifest()` and `get_config()` to
  check metadata consistency without downloading layer blobs. Validates
  schema version, rootfs type, layer count vs diff_id count, history
  entry count, compression compatibility, and more.

- **Full mode** (default): Downloads all layers via `fetch()` and additionally
  verifies config digest/size, diff_ids (SHA256 of decompressed layers),
  tar archive validity, whiteout file correctness, and tar header
  integrity.

The check logic lives in a separate `occystrap/check.py` module:
- `CheckResults` - Accumulator for errors, warnings, and informational messages
- `check_metadata(manifest, config, results)` - Fast mode checks
- `check_layers(input_source, manifest, config, results)` - Full mode checks

The command in `main.py` wires these together and handles output formatting
(text or JSON via `-O`/`--output-format`). Exit code is non-zero if any
errors are found, enabling CI integration.

## URI-Style Command Line

The new `process` command uses URI-style arguments:

```
occystrap process SOURCE DESTINATION [-f FILTER]...
```

### Input URIs

```
registry://[user:pass@]host/image:tag[?arch=X&os=Y&variant=Z]
quay://org/glob:tag[?token=TOKEN]
docker://image:tag[?socket=/path/to/socket]
dockerpush://image:tag[?socket=/path/to/socket]
tar:///path/to/file.tar
```

### Output URIs

```
tar:///path/to/output.tar
dir:///path/to/directory[?unique_names=true&expand=true]
oci:///path/to/bundle
mounts:///path/to/directory
docker://image:tag[?socket=/path/to/socket]
registry://host/image:tag[?insecure=true]
```

### Filter Specifications

```
filter-name
filter-name:option=value
filter-name:opt1=val1,opt2=val2
```

Available filters:
- `exclude` - Exclude files from layers (option: `pattern=GLOB` or
  `pattern=GLOB1,GLOB2,...` for multiple patterns)
- `inspect` - Record layer metadata to JSONL file (option: `file=PATH`)
- `normalize-timestamps` - Normalize layer timestamps (option: `ts=TIMESTAMP`)
- `search` - Search for files (options: `pattern=GLOB`, `regex=true`,
  `script_friendly=true`)

## Quay.io Bulk Image Discovery

The `quay://` URI scheme enables discovering and fetching multiple images
from a quay.io organization by tag. Unlike all other input sources (which
produce exactly one image), `quay://` is a **multi-image** input that
resolves to zero or many images.

### Architecture

The `quay://` scheme is not implemented as an `ImageInput` subclass.
Instead, it is a **resolver** that expands a single URI into a list of
standard `(registry, image, tag)` tuples:

```
quay://kolla/*:latest
    │
    ▼
resolve_quay_uri('kolla', '*', 'latest')
    │
    ├── QuayClient.list_repositories('kolla')     ← quay.io API v1
    ├── fnmatch filter by glob pattern
    ├── QuayClient.has_tag('kolla', repo, 'latest') for each match (parallel)
    │
    ▼
[('quay.io', 'kolla/nova-api', 'latest'),
 ('quay.io', 'kolla/keystone', 'latest'),
 ...]
    │
    ▼
For each tuple (concurrently, up to -J workers):
build a standard registry.Image input and run
through the existing pipeline
```

### Module: `occystrap/quay.py`

- `QuayClient` - Wraps the quay.io proprietary REST API v1
  (`/api/v1/`), providing `list_repositories()` (paginated, opaque
  cursor tokens) and `has_tag()` (uses `specificTag` filter). Accepts
  an optional bearer token for private organizations.
- `resolve_quay_uri()` - Orchestrates the discovery flow: lists repos,
  filters by glob, checks tags in parallel via `ThreadPoolExecutor`
  (concurrency controlled by `-j` flag), returns tuples.
- `_check_one_repo()` - Per-repo tag check helper, designed to run in
  the thread pool. Handles tag existence and since-date filtering.

### Command Integration

The `info` and `process` commands in `main.py` detect the `quay` scheme
before calling `PipelineBuilder`. A helper `_resolve_quay_images()`
parses the URI and runs the resolver. The commands then process images
concurrently using `ThreadPoolExecutor` (controlled by the `-J` /
`--image-parallel` flag, default 3), creating a fresh `registry.Image`
input and pipeline for each image. Each image gets its own independent
output writer instance.  The `PipelineBuilder` itself has no knowledge
of `quay://`.

Thread safety for concurrent multi-image processing:
- `DirWriter` with `unique_names=true`: each image writes to its own
  manifest file. `catalog.json` updates are serialized via a module-level
  `threading.Lock`.
- `RegistryWriter`: each image has its own instance with independent
  `httpx.Client` and thread pool.
- `LayerCache`: already thread-safe via `threading.Lock`.

## Key Concepts

### Whiteout Files

OCI layers use special files to mark deletions:
- `.wh.<filename>` - Marks a file as deleted
- `.wh..wh..opq` - Marks directory as opaque (contents replaced)

Processed in `outputs/directory.py` when `expand` option is used.

### Unique Names Mode

`unique_names=true` enables storing multiple images in one directory by
prefixing manifest files with image/tag names. A `catalog.json` tracks which
layers belong to which images.

### Timestamp Normalization

The `normalize-timestamps` filter rewrites layer tar mtimes for reproducible
builds, recalculating layer SHAs.

### Docker Daemon Hybrid Streaming

The Docker daemon input (`inputs/docker.py`) uses a hybrid streaming approach
to minimize disk usage when exporting images. It also uses the Docker Engine
inspect API to pre-compute manifest information, avoiding the need to wait
for `manifest.json` (which arrives near the end of the tarball stream).

#### Pre-Computed Manifest

Before streaming begins, `fetch()` calls the inspect API
(`GET /images/{name}/json`) to extract:

- **Config hash** from `Id` field (e.g., `sha256:abc123...`)
- **DiffIDs** from `RootFS.Layers` (SHA256 hashes of uncompressed layers)

For Docker 25+ OCI format (detected when tarball entries start with
`blobs/`), DiffIDs directly correspond to blob paths
(`blobs/sha256/<diffid>`), so the full manifest can be pre-computed before
any data arrives. This eliminates buffering entirely.

For Docker 1.10-24.x content-addressable format, the config file can be
identified early (`<config-hex>.json`) and yielded immediately, but layer
directory names (v1-compat IDs) cannot be predicted from inspect data.
Layers are still buffered until `manifest.json` arrives for ordering.

When inspect data is unavailable or the pre-computed manifest differs from
the actual `manifest.json`, occystrap falls back to buffered processing.

See [docs/docker-tarball-formats.md](docs/docker-tarball-formats.md) for
detailed documentation on tarball formats, entry ordering, and the inspect
API.

#### Streaming Pipeline

```
fetch() generator
    └── Call inspect API to get config hash and DiffIDs
    └── Stream tarball sequentially (mode='r|')
    └── Detect format from first entry (blobs/ prefix = OCI)
    └── If OCI: pre-compute manifest from DiffIDs
    └── If legacy: identify config early from inspect hash
    └── For each file in stream:
        ├── If ordered=True:
        │   ├── If next expected layer: yield directly (no disk I/O)
        │   └── If out-of-order: buffer to temp file for later
        └── If ordered=False:
            └── Any known layer: yield immediately with layer_index
    └── After stream ends: yield remaining buffered layers
```

Key design considerations:
- Uses tarfile streaming mode (`r|`) - files read sequentially as they appear
- Pre-computed manifest enables zero-buffering for Docker 25+ OCI format
- Early config identification reduces blocking for Docker 1.10-24.x format
- Optimistic case: layers in order are streamed directly with no temp files
- Pessimistic case: out-of-order layers buffered to individual temp files
- Temp files are cleaned up immediately after yielding each layer
- Temp file location is configurable via `--temp-dir` CLI option
- Graceful fallback when inspect data is unavailable or incorrect

### Docker Push via Embedded Registry

The `dockerpush` input (`inputs/dockerpush.py`) takes a fundamentally different
approach to fetching images from a local Docker daemon. Instead of using the
Docker Engine API's `/images/{name}/get` endpoint (which returns a single
sequential tarball), it starts an embedded HTTPS server implementing the Docker
Registry V2 push-path API, then uses Docker's own push mechanism to transfer
layers.

```
fetch() generator
    └── Generate ephemeral self-signed TLS certificate (via openssl)
    └── Start ThreadingHTTPServer on 127.0.0.1:0 (ephemeral port, TLS-wrapped)
    └── Tag image for localhost push
    └── POST /images/{name}/push (Docker pushes in parallel)
    └── Wait for manifest event from registry handler
    └── Parse manifest and config
    └── For each layer:
        ├── If fetch_callback returns False: yield with data=None
        └── If True: read blob, decompress, yield ImageElement
    └── Cleanup: untag, stop server, delete temp files
```

The embedded registry implements six V2 push-path endpoints:
- `GET /v2/` - Version check
- `HEAD /v2/{name}/blobs/{digest}` - Blob existence check
- `POST /v2/{name}/blobs/uploads/` - Start upload
- `PATCH /v2/{name}/blobs/uploads/{uuid}` - Receive chunks
- `PUT /v2/{name}/blobs/uploads/{uuid}?digest=...` - Complete upload
- `PUT /v2/{name}/manifests/{tag}` - Receive manifest

Key design considerations:
- Docker push uploads layers in parallel using the V2 protocol, providing
  significantly better throughput than the sequential tarball export
- Docker treats `127.0.0.0/8` as insecure (skips cert verification), but
  some Docker versions don't fall back from HTTPS to HTTP, so we serve
  HTTPS with an ephemeral self-signed certificate generated via `openssl`
- Blobs are stored as temp files during the push, then read and decompressed
  when yielding elements
- Thread-safe shared state (`_RegistryState`) coordinates between the HTTP
  handler threads and the main fetch() thread
- Both `EmbeddedRegistryHandler` and `ProxyRegistryHandler` inherit from
  `SafeHeaderMixin` (`util.py`), which overrides `send_header()` to strip
  `\r` and `\n` from values, preventing HTTP response splitting (CWE-113).
  User-controlled values are also wrapped in `sanitize_header_value()` at
  each call site so CodeQL sees the sanitization on the data flow path
- Cleanup is robust: untag, stop server, and delete temp files all happen
  in nested try/finally blocks
- When `--layer-cache` is used with a `registry://` output, the embedded
  registry returns `200` for HEAD checks on cached layers, causing Docker
  to skip their upload entirely. A persistent digest mapping file
  (`{cache_path}.digests`) translates between Docker's compressed digests
  and the DiffIDs used as cache keys

### Filtering Registry Proxy

The `proxy` command (`proxy.py`) runs a persistent Docker Registry V2 server
on localhost that receives pushes, applies filters, and forwards images to a
downstream registry. Unlike `dockerpush://` (which is a single-image-per-session
embedded registry), the proxy handles multiple images concurrently across its
lifetime.

```
occystrap proxy --listen 127.0.0.1:5050 \
    --downstream ghcr.io/myorg \
    -f normalize-timestamps
```

**Architecture:**

The proxy consists of four main components:

- `_ProxyState` - Shared state between HTTP handler threads and the main
  process. Holds temp_dir, downstream_uri, filter_strs, layer_cache, in-progress
  uploads, completed blobs, blob reference counts, diff_id_map (cross-image
  diff_id tracking for filtered layers), and statistics. All mutations
  protected by a lock. A processing semaphore limits concurrent manifest
  processing (configurable via `--concurrency`, default 4).

- `_ProxyInput(ImageInput)` - Synthetic input that yields `ImageElement`s from
  already-received blobs. Decompresses layers via `StreamingDecompressor` to
  feed uncompressed tar data into the pipeline, matching the interface that
  filters and outputs expect.

- `ProxyRegistryHandler` - HTTP request handler implementing the V2 push-path
  endpoints. The manifest PUT blocks the HTTP response while the image is
  processed through the pipeline and pushed downstream. Multiple manifests
  can be processed concurrently. Returns 201 on success, 500 on failure,
  giving Docker direct error feedback.

- `_KeepAliveHTTPServer` - `ThreadingHTTPServer` subclass that enables
  `SO_KEEPALIVE` on accepted connections, preventing TCP drops during long
  manifest processing.

**Manifest processing flow:**

```
1. Client pushes blobs (layers + config) via standard V2 upload flow
2. Client PUTs manifest
3. Handler parses manifest, extracts referenced blob set
4. Under lock: increment blob refcounts, snapshot blob paths,
   increment active_processing counter
5. Acquire processing semaphore (backpressure)
6. Handler creates _ProxyInput from received blobs
7. PipelineBuilder builds output + filters (fresh per image)
8. Pipeline runs: _ProxyInput.fetch() -> filters -> RegistryWriter
9. Release semaphore, decrement refcounts, delete blobs at refcount 0,
   decrement active_processing
10. On success: 201 to client
11. On failure: 500 to client
```

**Concurrent processing and blob reference counting:**

`ThreadingHTTPServer` creates a new thread for each HTTP request, so
multiple manifest PUTs can arrive simultaneously. A configurable
semaphore (`--concurrency`, default 4) limits how many manifests are
processed concurrently, providing backpressure when many images are
pushed at once.

When two concurrent pushes share blob digests (common for base layers),
reference counting prevents premature deletion. Blob refcounts are
incremented *before* acquiring the semaphore, so blobs are protected
even while a manifest is waiting for a processing slot. Blobs are only
deleted when their refcount reaches zero (i.e., no in-flight manifest
references them).

**Key design considerations:**

- Blocking manifest processing provides natural backpressure and error
  propagation. Docker sees success or failure directly.
- Each image gets a fresh pipeline (PipelineBuilder creates new instances),
  so there is no cross-image state leakage in filters or outputs -- except
  for `diff_id_map`, which intentionally persists across images to track
  how filters transform layer identities.
- A shared `LayerCache` across images enables cross-image layer dedup:
  the first image pays full cost, subsequent images with shared base
  layers skip those layers entirely. `LayerCache` is internally
  thread-safe via its own lock.
- Blob namespace is flat and content-addressed. Two images sharing a
  base layer push the same digest, giving cross-image dedup for free.
- Blob reference counting ensures shared blobs survive until all
  referencing manifests complete processing.
- `run_proxy()` handles SIGINT/SIGTERM gracefully, waiting up to 5
  minutes for in-flight image processing to complete before saving
  the layer cache and exiting.

**Pull-through / full proxy:**

When `--upstream` is specified, the proxy also serves as a pull-through
cache. Clients can pull images via standard Docker V2 GET endpoints.
The proxy checks the downstream registry first (acting as a persistent
cache), and on a cache miss, fetches from the upstream registry, applies
filters, pushes the filtered image to downstream, then serves it.

```
Client GET manifest → proxy → downstream HEAD → 200 → serve from downstream
                                              → 404 → acquire per-image lock
                                                     → upstream fetch → filter
                                                     → push downstream → serve
```

Pull-through reuses the existing pipeline infrastructure:
- `_build_output_pipeline()` constructs the filter chain + registry
  output (shared with the push path)
- `_run_pipeline()` executes the fetch/filter/output loop
- `input_registry.Image` provides authenticated upstream reads
- Cached `input_registry.Image` instances per repo provide
  authenticated downstream reads (token caching, streaming)

Per-image locks (`pull_locks`) prevent duplicate upstream fetches
when multiple clients request the same image concurrently. A
double-check pattern re-verifies the downstream cache after
acquiring the lock. The processing semaphore is shared between
push and pull paths for unified backpressure.

Pull statistics (`images_pulled`, `pull_cache_hits`,
`pull_cache_misses`) are logged at shutdown alongside push stats.

### Parallel Downloads

The registry input (`inputs/registry.py`) uses `ThreadPoolExecutor` for parallel
layer downloads:

```
fetch() generator
    └── Yield config file first (synchronous)
    └── Submit all layer downloads to ThreadPoolExecutor
    └── If ordered=True: yield layers in manifest order
    └── If ordered=False: yield layers as downloads complete
        (each with layer_index for reordering)

_download_layer() worker
    └── Fetch layer blob from registry
    └── Decompress to temp file with hash verification
    └── Retry on connection failures (exponential backoff)
```

Key design considerations:
- All layers are downloaded in parallel to maximize throughput
- When `ordered=True`, layers are yielded in manifest order despite
  parallel download
- When `ordered=False`, layers are yielded as downloads complete via
  `as_completed()`, with `layer_index` set for manifest reconstruction
- Authentication token updates are protected by a threading lock
- The `max_workers` parameter controls parallelism (default: 4)
- Temp files are cleaned up after each layer is processed
- Temp file location is configurable via `--temp-dir` CLI option

### Parallel Compression and Uploads

The registry output (`outputs/registry.py`) uses `ThreadPoolExecutor` for both
layer compression and uploads in parallel:

```
process_image_element() called for each layer
    └── Read layer data
    └── Submit (compress + upload) task to ThreadPoolExecutor (non-blocking)
    └── Main thread continues to next layer immediately

finalize()
    └── Wait for all compression/upload futures to complete
    └── Collect layer metadata from futures (in submission order)
    └── Check for any failures
    └── Push manifest only after all blobs uploaded
```

Key design considerations:
- Both compression and upload run in the thread pool, allowing multiple layers
  to compress simultaneously on multi-core systems
- Layer order is preserved by tracking `layer_index` on each future and
  sorting at finalize time
- Progress is reported every 10 seconds during the wait phase
- Authentication token updates are protected by a threading lock for
  thread-safety
- The `max_workers` parameter controls parallelism (default: 4)
- Granular timing is collected per-layer (compress time, upload time, input
  size) using a thread-safe lock, and reported in the summary line
- Upload skip count tracks how many blobs already existed in the registry
- Cross-invocation layer cache (`--layer-cache`) skips layers that were
  previously processed with the same filter configuration and whose
  compressed blobs still exist in the target registry

### Out-of-Order Layer Delivery

The pipeline supports out-of-order layer delivery to maximize throughput.
Outputs declare whether they need layers in manifest order via the
`requires_ordered_layers` property:

| Output | `requires_ordered_layers` | Reason |
|--------|---------------------------|--------|
| `dir` (expand=True) | `True` | Whiteout processing needs order |
| `oci` | `True` | Inherits from DirWriter with expand |
| `dir` (expand=False) | `False` | Just writes files |
| `tar` | `False` | Manifest built at finalize |
| `docker` | `False` | Manifest built at finalize |
| `registry` | `False` | Manifest built at finalize |
| `mounts` | `False` | Just writes files |

When the output declares `requires_ordered_layers = False`:
1. `_fetch()` in `main.py` passes `ordered=False` to the input's
   `fetch()` method
2. The input yields `ImageElement` objects with `layer_index` set to the
   layer's position in the manifest
3. The output stores layers with their indices and reconstructs the
   correct manifest order in `finalize()`

This eliminates unnecessary waiting in the registry input (layers are
yielded via `as_completed()` instead of waiting for each in sequence)
and reduces temp file buffering in the Docker input.

Filters propagate `requires_ordered_layers` from their wrapped output,
so the entire filter chain respects the final output's ordering needs.

### Cross-Invocation Layer Cache

The `layer_cache.py` module provides a persistent cache that maps input layer
DiffIDs to their compressed output digests. When pushing multiple images that
share base layers (common in CI), the cache allows subsequent invocations to
skip layers entirely -- no fetch, no filter, no compress, no upload.

```
fetch_callback(digest)
    └── Check cache for (digest, filters_hash)
    └── If found: HEAD request to verify registry still has blob
    └── If registry has blob: skip layer entirely
    └── If not: process normally

_compress_and_upload_layer()
    └── After successful compress + upload: record to cache

finalize()
    └── Save cache to disk (atomic write via temp file + rename)
```

Key design considerations:
- Cache keyed by `(input_diffid, filters_hash)` so different pipeline
  configurations get separate entries
- Original DiffIDs are tracked via a FIFO queue in `fetch_callback` and
  consumed in `process_image_element`, because content-modifying filters
  (ExcludeFilter, TimestampNormalizer) recalculate the layer hash —
  the cache must record under the original DiffID, not the transformed one
- `filters_hash` is computed from the serialized filter chain **and**
  compression type, so gzip vs zstd pipelines do not share entries
- Registry blob existence is verified on each run (`HEAD` request) to handle
  registry garbage collection
- Cache file is JSON, stored at the path specified by `--layer-cache`
- Atomic save via temporary file and `os.replace` prevents corruption
- Cache statistics (hits) are included in the summary line

### Layer Compression

The `compression.py` module provides unified handling for layer compression
formats:

- **Detection**: Magic byte and media type detection for gzip/zstd
- **Streaming decompression**: Used by registry input for downloading layers
- **Streaming compression**: Used by registry output for uploading layers
- **Configurable output**: `--compression` CLI option (gzip default, zstd optional)
- **Deterministic output**: gzip uses `mtime=0` to suppress header timestamps;
  zstd is inherently deterministic (no timestamps in format)

Deterministic compression is important for blob deduplication: the registry
output checks if a blob already exists before uploading (`HEAD` on the blob
digest), and this only works when identical input always produces the same
compressed output with the same SHA256 digest.

Media type constants in `constants.py` define Docker and OCI layer types:
- `MEDIA_TYPE_DOCKER_LAYER_GZIP` / `MEDIA_TYPE_DOCKER_LAYER_ZSTD`
- `MEDIA_TYPE_OCI_LAYER_GZIP` / `MEDIA_TYPE_OCI_LAYER_ZSTD`

## HTTP Layer

Registry HTTP communication uses httpx (`util.py`), providing connection pooling,
HTTP/2 support, and structured retry/rate-limiting.

### httpx and Connection Pooling

All registry-facing HTTP requests go through `util.request_url()`, which uses
httpx instead of requests. Since `httpx.Client` is not thread-safe, each
worker thread in a `ThreadPoolExecutor` gets its own client via the
`ThreadSafeClientMixin` in `util.py`. The main thread is seeded with the
primary client, and worker threads lazily create their own:

```python
# util.py — shared mixin used by Image and RegistryWriter
class ThreadSafeClientMixin:
    def _init_thread_clients(self): ...
    def _get_thread_client(self): ...
    def _close_thread_clients(self): ...
```

The `create_client()` factory in `util.py` creates each client with connection
pooling and optional HTTP/2:

```python
limits = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10)
client = httpx.Client(
    http2=True,
    limits=limits,
    follow_redirects=True,
    timeout=httpx.Timeout(30.0, connect=10.0))
```

When no client is provided to `request_url()`, a temporary client is created
and closed after the request — this is the fallback for one-off calls.

**Docker daemon communication** (`inputs/docker.py`, `inputs/dockerpush.py`,
`outputs/docker.py`) still uses `requests-unixsocket` for Unix domain socket
access, since httpx does not natively support Unix sockets. This code is not
affected by the httpx migration.

### HTTP/2

httpx negotiates HTTP/2 via ALPN during TLS handshake. If the registry
supports HTTP/2, it is used automatically; otherwise, httpx falls back to
HTTP/1.1. HTTP/2 provides multiplexed streams over a single connection, which
can reduce latency when many requests are in flight (e.g., parallel layer
downloads/uploads).

### Retry Logic

`request_url()` retries on transient failures with exponential backoff
(base 2 seconds):

- **429 (Too Many Requests)**: Respects the `Retry-After` header when present;
  otherwise uses exponential backoff. Retried up to `retries` times.
- **5xx (Server Errors)**: Retried with exponential backoff.
- **Connection errors** (`httpx.ConnectError`, `httpx.RemoteProtocolError`,
  `httpx.ReadError`): Retried with exponential backoff.

The retry count defaults to 3 and is configurable via the `--retries` CLI
flag or `OCCYSTRAP_RETRIES` environment variable.

### Rate Limiting

The `RateLimiter` class in `util.py` implements a simple token-bucket algorithm
that enforces a maximum request rate (requests per second). It is thread-safe,
using a lock to ensure correct spacing across parallel download/upload threads.

Rate limiting is enabled via the `--rate-limit` CLI flag or
`OCCYSTRAP_RATE_LIMIT` environment variable. When set, every call to
`request_url()` calls `rate_limiter.acquire()` before making the HTTP request,
blocking if needed to maintain the configured rate.

### CLI Integration

The `--retries` and `--rate-limit` global CLI options are stored in the Click
context and passed through `PipelineBuilder` to `Image`, `RegistryWriter`, and
`QuayClient` constructors, which forward them to `create_client()` and
`request_url()`.

## Data Flow

```
                                    +-----------------+
                                    |                 |
Input URI  -->  Input Source  -->  | Filter Chain    |  -->  Output Writer  -->  Files
                     |              | (optional)      |            |
                   fetch()          +-----------------+        finalize()
                     |                     |                 (reorder by
               yields ImageElement  process_image_element()  layer_index)
                     |                     |
                     +---------------------+
                        fetch_callback
                       (skip/include)
```

The `_fetch()` function in `main.py` connects the pipeline. It wraps
the loop in `redirect_logging()` (from `progress.py`) so that tqdm
progress bars and log output do not interfere with each other:

```python
def _fetch(img, output):
    ordered = output.requires_ordered_layers
    with redirect_logging():
        for element in img.fetch(
                fetch_callback=output.fetch_callback,
                ordered=ordered):
            output.process_image_element(element)
        output.finalize()
```
