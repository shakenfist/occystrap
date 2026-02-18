# Occystrap Documentation Updates

## Golden Rule

**Every user-visible change requires documentation updates.**

When adding or modifying commands, flags, filters, input/output
formats, or pipeline behavior, you MUST update the relevant
documentation before committing.

## Documentation Locations

| Change Type | Documentation to Update |
|-------------|------------------------|
| New CLI flag | `docs/command-reference.md`, `README.md` |
| New command | `docs/command-reference.md`, `README.md` |
| New filter | `docs/command-reference.md`, `ARCHITECTURE.md` |
| New input source | `docs/command-reference.md`, `ARCHITECTURE.md` |
| New output writer | `docs/command-reference.md`, `ARCHITECTURE.md` |
| Compression changes | `docs/command-reference.md` |
| URI scheme changes | `docs/command-reference.md` |
| Pipeline behavior | `ARCHITECTURE.md` |
| Logging changes | `AGENTS.md` (logging conventions) |
| New use case | `docs/use-cases.md` |

## Checklist for Feature Changes

When implementing a new feature or changing behavior:

- [ ] Update `docs/command-reference.md` if it affects CLI usage
- [ ] Update `README.md` if it affects basic usage
- [ ] Update `ARCHITECTURE.md` if it changes system design
- [ ] Update `AGENTS.md` if it changes patterns for AI assistants
- [ ] Update `docs/use-cases.md` if it enables new workflows
- [ ] Add examples showing before/after behavior

## Documentation Style

- Wrap lines at 80 characters
- Use single quotes for Python strings (except docstrings)
- Use f-strings for string formatting
- Show concrete examples with real registry paths
