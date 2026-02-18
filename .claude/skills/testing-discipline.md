# Occystrap Testing Discipline

## Golden Rule

**Every code change must pass unit tests and pre-commit hooks.**

## Testing Workflow

### Before Committing

```bash
# Run pre-commit hooks (includes flake8, unit tests,
# actionlint, shellcheck, check-log-levels)
cd /srv/kasm_profiles/mikal/vscode/src/shakenfist/occystrap
pre-commit run --all-files
```

### Running Unit Tests Directly

```bash
tox -epy3
```

### Running Flake8

```bash
tox -eflake8
```

## Test Locations

| Test Type | Location |
|-----------|----------|
| Unit tests | `occystrap/tests/` |
| Functional tests | `deploy/occystrap_ci/tests/` (CI only) |

## Writing Unit Tests

When adding new functionality:

- Add unit tests in `occystrap/tests/`
- Test file naming: `test_<module>.py`
- Use `testtools.TestCase` as the base class
- Use `click.testing.CliRunner` for CLI tests
- Use `result.stdout` (not `result.output`) for JSON parsing
- Mock external calls (registry, Docker daemon, filesystem)

### CliRunner and JSON Output

Click 8.2+ changed `result.output` to mix stdout and stderr.
Tests that parse JSON must use `result.stdout` (stdout only):

```python
result = runner.invoke(cli, ['info', source])
data = json.loads(result.stdout)  # NOT result.output
```

## Log Level Enforcement

The `check-log-levels` pre-commit hook enforces a maximum of
10 `LOG.info()` calls per file. If you hit this limit:

- Audit existing info calls -- should any be debug?
- New per-layer/per-request operations should be `LOG.debug()`
- Only milestones (pipeline start/end, summaries) are `LOG.info()`

## Commit Checklist

Before committing, verify:

- [ ] `pre-commit run --all-files` passes
- [ ] New code has unit test coverage
- [ ] No new `LOG.info()` calls exceed the per-file limit
- [ ] `result.stdout` used (not `result.output`) for JSON tests
