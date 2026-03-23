# Phase 4: Functional tests and documentation

## Context

This is phase 4 of the
[quay.io tag-based bulk image discovery plan](PLAN-quay-label-search.md).
Phases 1-3 implemented the quay.io API client, URI parsing,
multi-image resolution, and command integration. This phase adds
functional tests and updates all documentation.

## Goal

1. Add functional tests that exercise the quay:// pipeline
   end-to-end.
2. Update all documentation to describe the new feature.

## Functional tests

### Test strategy

The existing functional tests in `deploy/occystrap_ci/tests/`
hit a local Docker registry at `localhost:5000`. For quay://
tests, we have two options:

**Option A: Hit the real quay.io API.** This is a true
integration test but makes CI dependent on an external service.
The quay.io API is public for public orgs and does not require
auth, so it should be reliable, but it could be flaky due to
network issues or rate limiting.

**Option B: Mock the quay.io API at the HTTP level.** Use
`unittest.mock.patch` on `util.request_url` to simulate the
quay.io API responses, then let the resolver produce
`registry://localhost:5000/...` references that hit the local
registry. This tests the full pipeline (resolver + registry
pull) without depending on quay.io.

**Recommendation: Use Option B** (mocked quay.io API, real local
registry). The unit tests in phases 1-3 already verify the API
client and resolver logic in isolation. The functional test
should verify that the full command flow works end-to-end:
resolution produces the right references, and those references
are successfully fetched from a real registry. Mocking only the
quay.io API (not the registry pull) achieves this.

### Test file

New file: `deploy/occystrap_ci/tests/test_quay_bulk.py`

Following the existing functional test pattern:
- Uses `testtools.TestCase`
- Uses `CliRunner` from Click
- Runs against `localhost:5000` with `--insecure`

### Test cases

1. **`test_info_quay_text`** — Mock `resolve_quay_uri` to return
   2 images from `localhost:5000` (e.g., `library/busybox` and
   `library/hello-world`). Run `info` with a `quay://` URI.
   Verify output contains both image names and the `---`
   separator.

2. **`test_info_quay_json`** — Same as above but with `-O json`.
   Verify output is a valid JSON array with 2 entries, each
   having `image`, `tag`, `layer_count` fields.

3. **`test_process_quay_to_dir`** — Mock `resolve_quay_uri` to
   return 2 images. Run `process` with `quay://` source and
   `dir://...?unique_names=true` destination. Verify the
   output directory contains manifest files for both images.

4. **`test_info_quay_empty`** — Mock `resolve_quay_uri` to
   return an empty list. Verify the command exits 0 and
   prints "No images found".

5. **`test_process_quay_tar_rejected`** — Mock `resolve_quay_uri`
   to return 1 image. Run `process` with `tar://` destination.
   Verify exit code is non-zero with an error message about
   tar:// not supporting multi-image sources.

### Mocking approach

The functional tests mock at the `_resolve_quay_images` level
in `main.py`, making them return tuples pointing at the local
registry (`localhost:5000`). This means:

- The quay.io API is not called (no external dependency)
- The actual image pull happens against the real local registry
  (tests the full pipeline from `registry.Image` onward)
- The tests are fast and reliable

## Documentation updates

### `docs/command-reference.md`

1. Add `quay://` to the **Input URI Schemes** section (new
   subsection after `registry://`).
2. Add `quay://` to the input URI list in the `process` command.
3. Add `quay://` to the input URI list in the `info` command.
4. Add examples showing multi-image usage.

### `ARCHITECTURE.md`

1. Add a new subsection describing the quay.io client module
   (`occystrap/quay.py`) and its role.
2. Add `quay.py` and `tests/test_quay.py` to the directory
   listing.
3. Document the multi-image resolution flow (quay:// URI →
   resolver → list of registry:// references → existing
   pipeline per image).

### `README.md`

1. Add `quay://ORG/GLOB:TAG` to the Input URI Schemes list.
2. Add a brief example showing bulk fetch from quay.io.

### `AGENTS.md`

1. Add a bullet point about the `quay://` multi-image input
   pattern — noting that it is not an `ImageInput` subclass
   but a resolver that expands to multiple `registry://`
   operations.

### `docs/plans/index.md`

Already updated in phase 1 commit. No changes needed.

## Commit plan

Two commits:
1. Functional tests (`deploy/occystrap_ci/tests/test_quay_bulk.py`)
2. Documentation updates (all doc files in one commit)
