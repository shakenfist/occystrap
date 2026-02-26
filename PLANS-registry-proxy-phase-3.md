# Phase 3: Pull-Through / Full Proxy

## Context

Phases 1-2 implemented a push-only filtering registry proxy
(`proxy.py`). Phase 3 adds pull-through: when a client pulls
from the proxy, occystrap fetches from an upstream registry,
applies filters, pushes to the downstream registry (which acts
as a persistent cache), and serves the filtered result. Subsequent
pulls serve directly from downstream without re-processing.

```
Client pull → proxy → downstream hit  → serve from downstream
                    → downstream miss → upstream → filter →
                      push downstream → serve from downstream
```

## Changes Required

### 1. Extend `_ProxyState` (`proxy.py`)

New constructor params and fields:
- `upstream_uri` (str or None, default None)
- `upstream_username`, `upstream_password` (optional)
- `downstream_images: dict` — `repo_name -> Image` instance
  cache for downstream reads (under `self.lock`). Reuses
  `inputs/registry.Image` for authenticated requests, token
  caching, `get_manifest()`, and `request_url(stream=True)`.
- `pull_locks: dict` — `'repo:tag' -> threading.Lock()` (under
  `self.lock`) to prevent duplicate upstream fetches
- Stats: `images_pulled`, `pull_cache_hits`, `pull_cache_misses`

### 2. Refactor `_process_image` (`proxy.py`)

Extract output pipeline construction into
`_build_output_pipeline(repo_name, tag)` — builds
`PipelineBuilder.build_output()` + wraps with filters. Both
push path (`_process_image`) and pull path (`_pull_and_process`)
call this, eliminating duplication.

### 3. New `_pull_and_process` (`proxy.py`)

Creates `input_registry.Image` from upstream, builds output via
`_build_output_pipeline()`, runs the pipeline. Result: filtered
image now exists in downstream. Add
`from occystrap.inputs import registry as input_registry`.

### 4. Pull-path HTTP handlers (`proxy.py`)

**`_handle_pull_manifest(path)`:**
1. Parse repo_name, reference via `_parse_manifest_path()`
2. If no upstream → 404
3. Get or create downstream `Image` instance from
   `state.downstream_images` cache, call `get_manifest()`
4. **Cache hit**: proxy manifest to client, increment
   `pull_cache_hits`
5. **Cache miss**: acquire per-image lock (double-check pattern),
   acquire `processing_semaphore`, increment `active_processing`,
   call `_pull_and_process()`, release semaphore/counter/lock,
   serve manifest from downstream, increment `pull_cache_misses`
6. Increment `images_pulled`
7. On upstream failure → 502 Bad Gateway

**`_handle_pull_blob(path)`:**
1. Parse repo_name, digest from path
2. Get downstream `Image` instance, call
   `request_url('GET', url, stream=True)` for the blob
3. Stream to client (1MB chunks via `_proxy_downstream_response`)
4. If downstream raises (404) → 404 to client

**`_proxy_downstream_response(resp)`:**
Forward status, Content-Type, Content-Length,
Docker-Content-Digest headers. Stream body chunks.

### 5. Route changes (`proxy.py`)

**`do_GET()`**: Route `/v2/{name}/manifests/{ref}` →
`_handle_pull_manifest`, `/v2/{name}/blobs/sha256:{digest}` →
`_handle_pull_blob`. Keep `/v2/` version check.

**`do_HEAD()`**: Add manifest HEAD (check downstream). Extend
blob HEAD to also check downstream when upstream is configured
(currently only checks local push-path blobs).

### 6. Path parsing (`proxy.py`)

Add `_parse_blob_path(path)` → `(repo_name, digest)`. Reuse
existing `_parse_manifest_path()` for manifest GETs.

### 7. CLI changes (`main.py`)

Add `--upstream/-u` to `proxy_cmd`. Parse `user:pass@host`
format to extract optional credentials. Pass `upstream_uri`,
`upstream_username`, `upstream_password` to `run_proxy()`.

### 8. `run_proxy()` changes (`proxy.py`)

Add `upstream_uri=None`, `upstream_username=None`,
`upstream_password=None` parameters. Pass to `_ProxyState`.
Log pull stats at shutdown alongside push stats.

## Files Modified

| File | Changes |
|------|---------|
| `occystrap/proxy.py` | Pull handlers, pipeline refactor, downstream Image cache, routing |
| `occystrap/main.py` | `--upstream/-u` option |
| `occystrap/tests/test_proxy.py` | Pull-through tests |
| `ARCHITECTURE.md` | Pull-through architecture |
| `AGENTS.md` | Pull-through guidance |
| `README.md` | Pull-through usage |
| `docs/command-reference.md` | `--upstream` option |

## Tests

- **Manifest GET cache hit**: downstream has image → served directly
- **Manifest GET cache miss**: downstream miss → upstream fetch →
  filter → push downstream → serve
- **Blob GET**: proxy streams from downstream
- **Blob GET 404**: non-existent blob → 404
- **HEAD manifest**: 200 with digest or 404
- **Pull disabled**: no `--upstream` → GET manifest returns 404
- **Concurrent pulls same image**: per-image lock deduplicates
- **Pull statistics**: correct counts for pulled/hits/misses
- **Downstream Image reuse**: token caching across requests
- **_build_output_pipeline reuse**: existing push tests still pass

## Implementation Sequence

1. `_ProxyState` additions (upstream fields, downstream Image
   cache, pull stats, pull_locks)
2. Refactor `_process_image` → `_build_output_pipeline` (verify
   existing push tests still pass)
3. `_pull_and_process`, `_handle_pull_manifest`,
   `_handle_pull_blob`, `_proxy_downstream_response`
4. `do_GET`/`do_HEAD` routing, `_parse_blob_path`
5. CLI `--upstream` option, `run_proxy()` signature
6. Tests
7. Documentation

## Verification

```bash
pre-commit run --all-files   # flake8, log levels, unit tests
```
