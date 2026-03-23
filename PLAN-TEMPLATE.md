# Title for the plan

## Prompt

Before responding to questions or discussion points in this
document, explore the occystrap codebase thoroughly. Read relevant
source files, understand existing patterns (pipeline architecture,
input/filter/output interfaces, URI parsing, CLI commands, registry
authentication, error handling), and ground your answers in what
the code actually does today. Do not speculate about the codebase
when you could read it instead. Where a question touches on
external concepts (Docker Registry V2, OCI specs, container image
formats, compression), research as needed to give a confident
answer. Flag any uncertainty explicitly rather than guessing.

Consult `ARCHITECTURE.md` for the pipeline pattern, element types,
input/filter/output interfaces, and cross-cutting concerns (layer
caching, parallel downloads, compression). Consult `CLAUDE.md` for
build commands and project conventions.

When we get to detailed planning, I prefer a separate plan file
per detailed phase. These separate files should be named for the
master plan, in the same directory as the master plan, and simply
have `-phase-NN-descriptive` appended before the `.md` file
extension. Tracking of these sub-phases should be done via a table
like this in this master plan under the Execution section:

```
| Phase | Plan | Status |
|-------|------|--------|
| 1. Registry listing API | PLAN-thing-phase-01-listing.md | Not started |
| 2. Label filtering | PLAN-thing-phase-02-labels.md | Not started |
| ...   | ...  | ...    |
```

I prefer one commit per logical change, and at minimum one commit
per phase. Do not batch unrelated changes into a single commit.
Each commit should be self-contained: it should build, pass tests,
and have a clear commit message explaining what changed and why.

## Situation

...

## Mission and problem statement

...

## Open questions

...

## Execution

...

## Administration and logistics

### Success criteria

We will know when this plan has been successfully implemented
because the following statements will be true:

* The code passes `flake8 --max-line-length=120` and
  `pre-commit run --all-files`.
* New code follows the existing pipeline pattern (input/filter/
  output interfaces) where applicable.
* There are unit tests for core logic and integration tests for
  new CLI commands.
* Lines are wrapped at 120 characters, single quotes for strings,
  double quotes for docstrings.
* Documentation in `docs/` has been updated to describe any new
  commands or features.
* `ARCHITECTURE.md`, `README.md`, and `AGENTS.md` have been
  updated if the change adds or modifies modules or CLI commands.

### Future work

We should list obvious extensions, known issues, unrelated bugs
we encountered, and anything else we should one day do but have
chosen to defer to here so that we don't forget them.

...

### Bugs fixed during this work

This section should list any bugs we encounter during development
that we fixed.

### Back brief

Before executing any step of this plan, please back brief the
operator as to your understanding of the plan and how the work
you intend to do aligns with that plan.
