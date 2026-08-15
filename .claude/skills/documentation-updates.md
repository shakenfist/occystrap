# Occystrap Documentation Updates

## Golden Rule

**Every user-visible change requires documentation updates.**

When adding or modifying commands, flags, filters, input/output
formats, or pipeline behavior, you MUST update the relevant
documentation before committing.

## Documentation Locations

| Change Type | Documentation to Update |
|-------------|------------------------|
| New CLI flag | `docs/command-reference.md` |
| New command | `docs/command-reference.md` |
| New filter | `docs/command-reference.md`, `docs/pipeline.md` |
| New input source | `docs/command-reference.md`, `docs/pipeline.md` |
| New output writer | `docs/command-reference.md`, `docs/pipeline.md` |
| Compression changes | `docs/command-reference.md` |
| URI scheme changes | `docs/command-reference.md` |
| Pipeline behavior | `docs/pipeline.md` |
| Proxy, Quay, parallelism, caching, HTTP | `docs/internals.md` |
| Logging changes | `AGENTS.md` (logging conventions) |
| New use case | `docs/use-cases.md` |
| New module or directory | `ARCHITECTURE.md` |
| New document in `docs/` | `docs/index.md` |

## Checklist for Feature Changes

When implementing a new feature or changing behavior:

- [ ] Update `docs/command-reference.md` if it affects CLI usage
- [ ] Update `docs/pipeline.md` if it changes pipeline structure or
      the input/filter/output interfaces
- [ ] Update `docs/internals.md` if it changes the Docker, Quay,
      proxy, parallelism, caching or HTTP internals
- [ ] Update `docs/index.md` if it adds a document to `docs/`
- [ ] Update `ARCHITECTURE.md` only if it changes the module
      inventory or directory structure -- it is a summary and an
      index into `docs/`, not a place for detail
- [ ] Update `AGENTS.md` if it changes patterns for AI assistants
- [ ] Update `docs/use-cases.md` if it enables new workflows
- [ ] Add examples showing before/after behavior

`README.md` is a pitch, not a feature list. It changes only when the
pitch, the install story, or the curated links into `docs/` change.

## Documentation Style

- Wrap lines at 80 characters
- Use single quotes for Python strings (except docstrings)
- Use f-strings for string formatting
- Show concrete examples with real registry paths
