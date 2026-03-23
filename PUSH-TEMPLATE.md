Thanks for your work on this. I appreciate it. Some final checks
before I push:

## Code quality

 * Did the changes introduce any significant amount of duplicated
   code? Are there any missed opportunities for code reuse or
   refactoring?
 * Should any new code be extracted into a shared module? Look for
   logic that a second command or input source would likely need.
 * Are there any TODO comments we should address as part of this
   work?
 * Please ensure all source code is wrapped at 120 characters.
 * Use single quotes for strings, double quotes for docstrings.

## Style conformance

 * Does the code follow the project conventions in `CLAUDE.md`?
   Check in particular:
   - Python conventions (error handling, module layout, logging).
   - CLI conventions (Click commands, global options like
     `--username`, `--password`, `-O`/`--output-format`).
   - Pipeline interface conventions (ImageInput, ImageOutput,
     ImageFilter base classes, ImageElement dataclass).
 * Does registry authentication follow the existing pattern in
   `inputs/registry.py`?

## Tests

 * Is there unit and functional test coverage for the changes?
   This should include normal and adversarial cases.
 * All tests should pass. We need to fix any failing tests now
   before we push.
 * What tests are skipped? Could we reduce that number?
 * Run `flake8 --max-line-length=120 occystrap/` and confirm
   clean output.
 * Run `pre-commit run --all-files` and confirm all hooks pass.

## Documentation

 * Has `docs/` been updated to reflect any new or changed
   commands? In particular, has `docs/command-reference.md` been
   updated?
 * Has `ARCHITECTURE.md` been updated if this change adds or
   modifies modules, commands, or pipeline components?
 * Has `README.md` been updated if usage instructions, project
   structure, or setup steps have changed?
 * Has `AGENTS.md` been updated?
 * Is all deferred work and pre-existing errors listed in a plan
   file?

## Security review

 * Review these changes as both a security reviewer and an
   experienced developer and correct any errors you find.
 * Are any user-controlled values (registry responses, image
   names, labels) used in file paths, HTTP headers, or shell
   commands without sanitization?

## Build verification

 * Does `pip install -e .` succeed?
 * Does `tox` pass?
