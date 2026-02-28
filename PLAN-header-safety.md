# Security Sanitization Helpers

## Context

GitHub CodeQL flags security issues when user-controlled data
flows into sensitive sinks without sanitization. This plan
documents two systemic approaches applied to occystrap.

Rather than fixing each call site individually, we use shared
helper functions and mixins that fix all current alerts AND
prevent future ones.

## HTTP Response Splitting (CWE-113)

### Approach: Two-Layer Defense

#### Layer 1: `sanitize_header_value()` at call sites

CodeQL's sink for `py/http-response-splitting` is the value
argument at the `send_header()` call site. Its
`ReplaceLineBreaksSanitizer` recognizes `.replace('\n', ...)`
calls on the data flow path *before* the sink. A
`sanitize_header_value()` function in `util.py` strips `\r`
and `\n` from values, and is called at each tainted call site
so CodeQL sees the sanitization before the sink.

#### Layer 2: `SafeHeaderMixin` as defense in depth

Override `send_header()` via a shared mixin class that both
HTTP handler classes inherit from. The mixin strips `\r` and
`\n` from header values before delegating to
`BaseHTTPRequestHandler`. This catches any call site that
forgets to use `sanitize_header_value()`.

Note: CodeQL does not recognize the mixin as a sanitizer
because it tracks taint to the call site argument (the sink),
not through the method override. The mixin provides real
security but does not suppress CodeQL alerts.

Other shakenfist projects (kerbside, shakenfist, agent-python)
use Flask/Werkzeug, which already rejects header values
containing line breaks. Only occystrap uses raw `http.server`.

### Files affected

- `occystrap/util.py`: `sanitize_header_value()` and
  `SafeHeaderMixin`
- `occystrap/inputs/dockerpush.py`: mixin inheritance +
  sanitize `digest_hex`, `location`, `expected_hex`
- `occystrap/proxy.py`: mixin inheritance + sanitize
  `digest_hex`, `location`, `expected_hex`, `upload_uuid`,
  forwarded upstream headers, `digest`/`cl` in
  `_handle_head_blob`

## Path Injection (CWE-22)

### Approach: `safe_path_join()`

CodeQL's `py/path-injection` flags file operations where
user-controlled data flows into path construction without
validation. The `safe_path_join()` helper in `util.py`
resolves the joined path via `os.path.realpath()` and
verifies it stays within the intended base directory.
Raises `PathEscapeError` if traversal is detected.

This uses `os.path.realpath()` which CodeQL recognizes as
a path sanitizer, combined with a `startswith()` check
which CodeQL recognizes as a path validation guard.

### Files affected

- `occystrap/util.py`: `safe_path_join()` and
  `PathEscapeError`
- `occystrap/outputs/directory.py`: all user-controlled
  `os.path.join()` calls replaced with `safe_path_join()`

## Unit Tests

In `occystrap/tests/test_proxy.py`:

- `TestSafeHeaderMixin`: 7 tests for mixin behavior
- `TestSanitizeHeaderValue`: 6 tests for function behavior
- `TestSafePathJoin`: 6 tests for path validation

## Audit Entry

`development/PROJECT-CONSISTENCY-AUDITS.md` documents both
patterns for cross-project consistency.

## Verification

```bash
# Unit tests
tox -epy3 -- test_proxy

# Pre-commit (flake8, etc.)
pre-commit run --all-files

# After push: verify CodeQL alerts resolve on next scan
```
