# Registry Proxy: Future Work and Known Issues

## Known Issues

### CliRunner mix_stderr in tests

Click 8.x defaults `CliRunner(mix_stderr=True)`, which mixes
stderr (logging) into `result.output` and `result.stdout`. Tests
that parse JSON from `result.stdout` after real (non-mocked) I/O
will fail if the command produces log output. Fixed for
`test_check_tar_source` by using `CliRunner(mix_stderr=False)`.
Other JSON-parsing tests currently pass because they mock
`PipelineBuilder` (suppressing real I/O), but any new test that
exercises real I/O and parses JSON from stdout should use
`mix_stderr=False`.

## Deferred Work

### Pull-through cache invalidation

The downstream registry acts as a permanent cache with no TTL or
purge mechanism. Once an image is cached, it is never re-fetched
from upstream even if the upstream tag has been updated. Options:

- Add `--cache-ttl` option to re-check upstream after N seconds
- Add a HEAD-based freshness check (compare upstream digest to
  cached digest before serving)
- Manual purge via DELETE endpoint or admin command

### Multi-architecture support

Both push and pull paths reject manifest lists (multi-arch
images) with a 400 error. Supporting multi-arch would require:

- Parsing manifest list to identify platform-specific manifests
- Processing each platform manifest independently
- Reassembling and pushing the manifest list to downstream

This is tracked in PLAN-compatibility.md as Priority 3.

### Functional tests for pull-through

Unit tests mock the upstream registry and downstream Image. A
full integration test would start an upstream registry container,
push a test image, run the proxy with `--upstream`, and verify
that `docker pull` through the proxy succeeds. This belongs in
`deploy/occystrap_ci/tests/`.

### Layer cache integration with pull-through

The pull-through path creates a fresh `PipelineBuilder` and
`RegistryWriter` per image, which will use `--layer-cache` if
configured. However, this hasn't been tested end-to-end with
pull-through. Shared base layers across pull-through images
should benefit from the layer cache.
