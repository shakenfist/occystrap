# Occystrap PR Preparation

## PR Readiness Checklist

Before creating or updating a PR, complete ALL of these steps:

### 1. Pre-commit Passes

```bash
cd /srv/kasm_profiles/mikal/vscode/src/shakenfist/occystrap
pre-commit run --all-files
```

Must show all checks passing. Fix any failures before proceeding.

### 2. Documentation Updated

Check whether docs/ needs updates:

- `docs/command-reference.md` for CLI changes
- `docs/pipeline.md` for pipeline and interface changes
- `docs/internals.md` for Docker, Quay, proxy, parallelism, caching
  and HTTP internals
- `docs/use-cases.md` for new workflows
- `docs/index.md` when adding a document to `docs/`
- `README.md` only when the pitch, install story or curated links change
- `AGENTS.md` for pattern changes
- `ARCHITECTURE.md` for structural changes -- the module inventory or
  directory structure. It is a summary and an index into `docs/`

### 3. Plan File Updated

If working from a plan in `docs/plans/`, update it with:

- Implementation summary (commit hashes)
- Future work items discovered during implementation
- Bugs found and fixed

## Creating the PR

Only after all checks pass:

```bash
git push -u origin <branch-name>
gh pr create --title "..." --body "..."
```

## PR Description Template

```markdown
## Summary
[Brief description of changes]

## Test plan
- [ ] `pre-commit run --all-files` passes
- [ ] Unit tests pass (`tox -epy3`)
- [ ] Documentation updated for user-visible changes
```

## Red Flags -- Do NOT Push

Never push a PR if:

- Pre-commit has failures
- Unit tests are failing
- User-visible changes lack documentation updates
- New filters/inputs/outputs lack unit tests
