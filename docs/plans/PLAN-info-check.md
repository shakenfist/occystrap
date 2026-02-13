# Implementing `info` and `check` subcommands for occystrap

## Prompt

Before responding to questions or discussion points in this
document, explore the occystrap codebase thoroughly. Read relevant
source files, understand existing patterns (project structure,
command-line argument handling, input source abstractions, output
formatting, error handling), and ground your answers in what the
code actually does today. Do not speculate about the codebase when
you could read it instead. Where a question touches on external
concepts (OCI image specs, Docker/Podman compatibility, registry
APIs), research as needed to give a confident answer. Flag any
uncertainty explicitly rather than guessing.

## Situation

I am trying something new with this document -- having a
conversation with Claude in a document instead of chat, and then
using the document as the implementation plan instead of having
Claude generate one to execute. Perhaps this is more "human in
the loop", but perhaps it is also "weird and inefficient". We'll
see I suppose.

This document is partially modelled on the western military
process of SMEAC OPORDs because I think the structure looks super
useful in general.

## Mission and problem statement

`occystrap`'s `process` command now supports some fairly
complicated content manipulation features like filtering and
changing timestamps. I'd like to implement more of those as the
need arises, but I am left thinking that its hard to catch bugs
in `occystrap`'s output. Its not a simple container converter
any more.

A recent example which raised this concern for me is this error
I am seeing in CI when using a docker local API -> filtration ->
registry push flow:

```
Unknown error message: wrong diff id
"sha256:9002b1c0c97baaa58d3bd29d02114743adaee9b3e601ededf6f65b138aae01df"
calculated on extraction
"sha256:123a078714d5ea9382d4d9f550753aefce8b34ec5ae11ae8273038d3bcbb943f",
desc "sha256:2914167652f8241cc96f909543ca0f525f067170ff80482695d1094d84abefea"
```

Now we could fix that one specific bug, but I am more interested
in ways we could ensure we don't have bugs like this **ever**. We
could for example pull the image we just pushed to the registry
in this example and then validate that the image is correct.

I am therefore proposing `occystrap`'s expansion to have two more
subcommands apart from `process`, at least partially inspired by
`qemu-img`.

### `occystrap info`

This subcommand would dump information about a given image to the
console in one of two formats -- human readable, and machine
friendly JSON depending on a global output flag. That output flag
should also be retrofitted to `process` so might well exist at
the logging layer.

The subcommand would support all of the input sources that
`process` currently supports.

### `occystrap check`

This command would perform an in-depth check of the validity of
the image: whether compression is supported; if the image will
only work on certain versions of Docker or Podman; if the manifest
elements all exist; etc etc. Literally everything we can think of.
It too would support both human and JSON output, and reuse the
`process` input sources.

## Open questions

### Do existing tools already cover info and check?

**Inspection tools:** `skopeo inspect` dumps image metadata as
JSON (digests, tags, creation date, architecture, layers).
`crane manifest` and `crane config` dump raw manifest and config
blobs. `regctl image inspect` is similar. These are adequate for
raw data but none present a concise human-readable summary
tailored to occystrap's use cases (e.g., "this image has 5
layers, 2 of which use zstd compression, total uncompressed size
is 340MB").

**Validation tools:** `crane validate` is the strongest existing
validator. It checks compressed layer digests against manifest
entries, uncompressed layer digests (diff_ids) against the config
blob, and the config blob's own digest against the manifest's
config descriptor. However, it has gaps that matter for
occystrap's layer manipulation:

* It does not check that the history array in the config is
  consistent with the layer count (non-empty history entries
  should equal the number of layers). When occystrap filters
  layers, it must also filter history entries -- no tool
  validates this.
* It does not verify that the declared mediaType matches the
  actual compression format of the blob (e.g., manifest says
  gzip but blob is actually zstd). This is a real-world
  interoperability trap.
* It does not check whiteout file preservation -- if occystrap's
  exclude filter accidentally removes `.wh.*` entries, the
  filesystem semantics are silently corrupted.
* It does not warn about Docker-vs-Podman compatibility issues
  (media type differences, `ArgsEscaped` deprecation, zstd
  support requirements).

**Conclusion:** Implementing `info` and `check` in occystrap is
justified. `crane validate` should be used in the test suite as
a baseline sanity check on occystrap's output, while
`occystrap check` adds the deeper, manipulation-aware checks
that crane misses. `diffoci` (a semantic image comparison tool)
could also be useful for regression testing.

### What information should info display?

* Image name and tag
* Manifest digest and schema version
* Media type (Docker v2 vs OCI) and what that implies for
  compatibility
* Architecture, OS, and variant
* Config digest and creation timestamp
* Number of layers, total compressed size, total uncompressed
  size
* Per-layer summary: index, compressed digest, diff_id,
  compressed size, compression format (detected from mediaType
  and/or blob magic bytes), and the corresponding history
  entry's `created_by` command (if present)
* Number of history entries and how many are `empty_layer: true`
* Labels, environment variables, entrypoint/cmd, working
  directory, exposed ports, volumes

### What things should check validate?

**Structural integrity (things that make an image invalid):**

1. `len(manifest.layers) == len(config.rootfs.diff_ids)` --
   layer count matches diff_id count
2. For each layer:
   `sha256(compressed_blob) == manifest.layers[i].digest` --
   compressed digest matches
3. For each layer:
   `sha256(uncompressed_blob) == config.rootfs.diff_ids[i]` --
   diff_id matches
4. `sha256(config_blob) == manifest.config.digest` and
   `len(config_blob) == manifest.config.size` -- config
   descriptor is correct
5. `config.rootfs.type == "layers"`
6. `manifest.schemaVersion == 2`

**History consistency (things that cause subtle runtime bugs):**

7. Number of history entries with `empty_layer != true` equals
   `len(manifest.layers)`
8. History entries are in the same order as layers

**Compression and compatibility (interoperability failures):**

9. Declared mediaType matches actual compression format of each
   layer blob (detect gzip vs zstd vs uncompressed from magic
   bytes)
10. If any layer uses zstd: warn that Docker Engine < 20.10 and
    containerd < 1.5 will not be able to pull this image
11. If manifest uses OCI media types: note that older Docker
    versions may not handle this correctly
12. If manifest uses Docker v2 media types: note that some
    OCI-only tooling may not handle this

**Filesystem integrity (corrupt container filesystem view):**

13. Whiteout files (`.wh.*` and `.wh..wh..opq`) are well-formed
14. Layer tar entries have consistent headers (no negative
    timestamps, reasonable permissions)

**Warnings (not errors, but worth reporting):**

15. Unreasonably large layers (> 1GB compressed)
16. Duplicate files across layers that could indicate missed
    deduplication opportunities
17. `config.ArgsEscaped` is set (Docker-specific, deprecated
    in OCI)

### Should process be called convert?

No. `process` is more accurate -- it does filtering, timestamp
normalization, searching, and inspection, not just format
conversion. Renaming would also break existing users. The
`qemu-img` analogy is useful for `info` and `check` but doesn't
need to extend to renaming `process`.

## Execution

### Shared prerequisite: output formatting

Both `info` and `check` need human-readable and JSON output
modes. The `search` command already has `--script-friendly` but
it's implemented ad-hoc with `click.echo` calls. We should
introduce a lightweight output abstraction before implementing
either command.

**Approach:** Add a `--output-format` / `-O` option to the CLI
group in `main.py` (choices: `text`, `json`; default: `text`).
Store it in the Click context so subcommands can access it via
`ctx.obj`. This also makes it available to `process` and `search`
if we want to retrofit them later.

The formatting logic itself can be minimal -- a helper function
that takes a dict/list and either pretty-prints it as a table
(using `prettytable`, already a dependency) or dumps it as JSON.
No need for a class hierarchy.

**Files touched:** `occystrap/main.py` (add option to `cli`
group).

### Implementation plan for `info`

**Step 1:** Add `info` command to `main.py`. It takes a single
`SOURCE` argument (URI string) using the same pattern as
`process`. Reuse `uri.parse_source()` and `pipeline.py`'s input
selection logic to construct an `ImageInput`.

**Step 2:** Call `input.fetch(ordered=True)` and consume only
the `CONFIG_FILE` element. Parse the config JSON to extract
image metadata. For registry inputs, we also have access to the
manifest via the input object's internals -- we may need to
expose a `manifest()` method or property on `ImageInput`
(currently the manifest is fetched internally but not exposed).

**Step 3:** For layer-level detail (compressed sizes, compression
format), we need the manifest's layer descriptors. For registry
inputs this is straightforward (the manifest is already fetched).
For tarball and docker inputs, the manifest is parsed internally.
We should expose a `get_manifest()` method on the base
`ImageInput` class that returns the parsed manifest dict, with
each input implementation providing it.

**Step 4:** Format and display the output using the shared output
formatting helper. Human-readable output should use `prettytable`
for the per-layer table and plain text for the summary fields.
JSON output should be a single dict with all fields.

**Files touched:** `occystrap/main.py` (new command),
`occystrap/inputs/base.py` (expose manifest),
`occystrap/inputs/registry.py`, `occystrap/inputs/docker.py`,
`occystrap/inputs/dockerpush.py`, `occystrap/inputs/tarfile.py`
(implement `get_manifest()`).

**Scope decision:** `info` should not need to download layer
blobs. It should work from the manifest and config alone. This
means it will report compressed sizes from manifest descriptors
but cannot report uncompressed sizes (those would require
downloading and decompressing every layer). This is the same
trade-off `crane validate --fast` makes and is the right default
for an info command.

### Implementation plan for `check`

**Step 1:** Add `check` command to `main.py`. Same `SOURCE`
argument and input selection as `info`.

**Step 2:** Implement a `CheckResult` dataclass or simple dict
structure to accumulate errors, warnings, and informational
messages. Each check produces entries tagged with a severity
(error, warning, info) and a human-readable description.

**Step 3:** Implement the structural integrity checks (items 1-6
from the check list above). These require both the manifest and
the config blob. Items 2-3 (digest verification) require
downloading and hashing every layer -- this makes `check` a slow
operation by design. Add a `--fast` flag that skips layer
download and only checks metadata consistency (items 1, 4, 5, 6,
7, 8, and the compatibility warnings).

**Step 4:** Implement the history consistency checks (items 7-8).

**Step 5:** Implement the compression and compatibility checks
(items 9-12). Item 9 requires reading the first few bytes of
each layer blob to detect the actual compression format (gzip
magic: `\x1f\x8b`, zstd magic: `\x28\xb5\x2f\xfd`). This can
piggyback on the layer download in step 3.

**Step 6:** Implement the filesystem integrity checks
(items 13-14). These require decompressing layers and scanning
tar entries. This also piggybacks on the layer download.

**Step 7:** Implement the warnings (items 15-17). These are
derived from data already collected in earlier steps.

**Step 8:** Format and display results using the shared output
formatting helper. Human-readable output should group by severity
(errors first, then warnings, then info). JSON output should be
a structured list of check results. Exit code should be non-zero
if any errors were found (useful for CI integration).

**Files touched:** `occystrap/main.py` (new command), potentially
a new `occystrap/check.py` module for the check logic if it grows
large enough to warrant separation from `main.py`.

### Testing strategy

**For `info`:** Create test images with known properties (specific
layer counts, compression formats, labels, history entries) and
verify `info`'s JSON output matches expected values. The JSON
output mode makes this straightforward -- parse the output and
assert on fields.

**For `check`:** We need images with known defects. Create these
programmatically in test fixtures:

* An image where `manifest.layers` has more entries than
  `config.rootfs.diff_ids` (layer count mismatch)
* An image where a layer's compressed digest doesn't match the
  manifest (corrupt digest)
* An image where the config blob's digest doesn't match the
  manifest's config descriptor (stale config reference)
* An image where history entries don't align with layers
* An image with mismatched mediaType vs actual compression

Also run `check` against known-good images produced by `process`
to verify they pass cleanly. This is the CI integration use case
from the problem statement -- after `process` produces an image,
`check` validates it.

**Existing test infrastructure:** The project uses
testtools/stestr with tox. New tests should follow the existing
patterns in `occystrap/tests/`. Functional tests that require
actual Docker/registry interaction go in
`deploy/occystrap_ci/tests/`.

## Administration and logistics

### Success criteria

We will know when this plan has been successfully implemented
because the following statements will be true:

* There are unit and functional tests for these features.
* There are a test suite of sample container images in
  `shakenfist/occystrap-testdata` that exercises these features
  and ensures they work correctly, including that their output
  agrees with other comparable tooling.
* Functional testing leverages these new commands to ensure
  that other `occystrap` commands produce valid output.
* Unit and functional tests pass.
* Documentation in `docs/` has been updated to describe these
  new features and how we use them.

### Future work

We should list obvious extensions, known issues, unrelated bugs
we encountered, and anything else we should one day do but have
chosen to defer to here so that we don't forget them.

* **Multi-architecture index validation:** `check` initially
  targets single-platform manifests. Validating image indexes
  (fat manifests) -- ensuring all platform entries point to valid
  manifests with matching architecture/OS fields -- is a natural
  extension.
* **Retrofit `--output-format` to `process` and `search`:** Once
  the output formatting infrastructure exists, the `search`
  command's ad-hoc `--script-friendly` flag could be replaced
  with the shared mechanism, and `process` could gain structured
  JSON progress reporting.
* **`check` as a post-`process` pipeline stage:** Consider
  allowing `process` to automatically run `check` on its output
  (e.g., `--verify` flag). This directly addresses the CI use
  case from the problem statement without requiring a separate
  invocation.
* **Remote-only fast checks for registries:** When the input is
  a registry, `check --fast` could use HEAD requests to verify
  blob existence without downloading anything, similar to how
  the registry output's `fetch_callback` already works.
* **`regctl image mod` as a reference comparison:** `regctl`
  provides similar manipulation capabilities (timestamps,
  compression, format conversion). Its output could be used as
  a reference in tests to verify occystrap produces equivalent
  results.

### Back brief

Before executing any step of this plan, please back brief the
operator as to your understanding of the plan and how the work
you intend to do aligns with that plan.
