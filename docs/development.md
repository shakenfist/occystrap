# Development

How to set up, test, and release occystrap.

## Install for development

```
pip install -e ".[test]"
```

## Pre-commit hooks

This project uses pre-commit hooks to validate code before commits. Install them
with:

```
pip install pre-commit
pre-commit install
```

The hooks run:

- `skillsaw` - Lints the agent context (`AGENTS.md`, `CLAUDE.md`, the
  skills) for malformed frontmatter, smuggled unicode and pasted secrets
- `actionlint` - GitHub Actions workflow validation
- `shellcheck` - Shell script linting
- `check-log-levels` - Enforces max LOG.info() calls per file
- `tox -eflake8` - Python code style checks
- `tox -epy3` - Unit tests

To run the hooks manually:

```
pre-commit run --all-files
```

## Running tests

Unit tests are in `occystrap/tests/` and can be run with:

```
tox -epy3
```

Functional tests are in `deploy/occystrap_ci/tests/` and are run in CI.

## Supply chain checks

The `Supply chain` workflow runs the two checks which look at the
content of the repository itself rather than at the code it builds:
credential scanning, and the agent context lint.

### Credential scanning

It scans every commit reachable from `HEAD` for
leaked credentials with [gitleaks](https://github.com/gitleaks/gitleaks),
on every pull request, on pushes to `develop`, and weekly. It is
deliberately not path filtered: a credential pasted into a documentation
code sample is still a credential.

To run the same scan locally:

```
sudo apt-get install -y gitleaks    # Debian 13 or later
tools/gitleaks-scan.sh
```

The script needs a full clone, not a shallow one -- a secret which was
committed and later reverted is still in the history, and still needs
rotating. Before it trusts a clean result it plants a GitHub token and
an SSH private key in a scratch directory and fails if gitleaks does not
report both, so a pass means "scanned and found nothing" rather than
"the scanner is broken". Pass `--gitleaks PATH` to use a downloaded
binary instead of the packaged one.

If the scan reports something, the credential needs rotating wherever it
was trusted -- history cannot be rewritten to unpublish it. A recurring
false positive (a documentation placeholder, a test fixture) is
allowlisted by adding a `.gitleaks.toml` keyed on the text; there is no
such file yet, because there has been nothing to forgive.

### Agent context lint

The `agent context` job runs the `skillsaw` pre-commit hook over the
files an agent obeys. CI runs the hook rather than the linter, so
`.pre-commit-config.yaml` stays the only place the skillsaw version is
written down, and running it in both places is deliberate: a hook can be
skipped with `--no-verify`, or by a clone which never ran `pre-commit
install`.

## Releasing

Releases are automated via GitHub Actions. Push a version tag to trigger the
pipeline:

```
git tag -s v0.5.0 -m "Release v0.5.0"
git push origin v0.5.0
```

The workflow builds the package, signs the tag with Sigstore, publishes to
PyPI, and creates a GitHub Release. See
[RELEASE-SETUP.md](https://github.com/shakenfist/occystrap/blob/develop/RELEASE-SETUP.md)
for one-time configuration steps.

## Developer automation

This project supports automated CI helpers via PR comments. To use these
commands, comment on a pull request with one of the following:

- `@shakenfist-bot please retest` - Re-run the functional test suite
- `@shakenfist-bot please attempt to fix` - Have Claude Code attempt to fix
  test failures
- `@shakenfist-bot please re-review` - Request another automated code review
- `@shakenfist-bot please address comments` - Have Claude Code address the
  automated review comments

These commands are only available to repository collaborators with write access.

## Claude Code skills

The `.claude/skills/` directory contains guidance for AI agents working on
this codebase, covering documentation updates, testing discipline, and PR
preparation.

Each skill lives in its own directory as `.claude/skills/<name>/SKILL.md`,
with `name` and `description` frontmatter. That layout is what an agent
discovers -- a bare markdown file directly in `.claude/skills/` is never
loaded, and is not linted by skillsaw either, so it looks like guidance
while doing nothing.
