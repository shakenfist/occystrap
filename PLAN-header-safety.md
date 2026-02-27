# HTTP Response Splitting Prevention (SafeHeaderMixin)

## Context

GitHub CodeQL flags `py/http-response-splitting` (CWE-113) on 7
open alerts in `occystrap/inputs/dockerpush.py`, with identical
patterns in `occystrap/proxy.py` (which will trigger once PR #56
merges). User-controlled data from URL paths, query parameters,
and upstream registry response headers flows into
`self.send_header()` without stripping `\r`/`\n` characters.

Rather than fixing each call site individually, we want a holistic
approach that fixes all current alerts AND prevents future ones.

## Approach: Two-Layer Defense

### Layer 1: `sanitize_header_value()` at call sites

CodeQL's sink for `py/http-response-splitting` is the value
argument at the `send_header()` call site. Its
`ReplaceLineBreaksSanitizer` recognizes `.replace('\n', ...)`
calls on the data flow path *before* the sink. A
`sanitize_header_value()` function in `util.py` strips `\r`
and `\n` from values, and is called at each tainted call site
so CodeQL sees the sanitization before the sink.

### Layer 2: `SafeHeaderMixin` as defense in depth

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
use Flask/Werkzeug, which already rejects header values containing
line breaks. Only occystrap uses raw `http.server`.

## Changes

### 1. `occystrap/util.py` -- add helpers

`sanitize_header_value(value)`: strips `\r` and `\n` from
a header value. Used at tainted call sites so CodeQL sees
the `.replace()` on the data flow path before the sink.

`SafeHeaderMixin`: overrides `send_header()` to sanitize
values automatically. Defense in depth for any call site
that forgets `sanitize_header_value()`.

### 2. `occystrap/inputs/dockerpush.py`

- Inherit from `SafeHeaderMixin` (first in MRO)
- Call `sanitize_header_value()` on `digest_hex` (from
  URL path), `location` (from URL path), and
  `expected_hex` (from query parameter)

Fixes 7 open CodeQL alerts.

### 3. `occystrap/proxy.py`

- Inherit from `SafeHeaderMixin` (first in MRO)
- Call `sanitize_header_value()` on `digest_hex`,
  `location`, `expected_hex`, forwarded upstream headers
  (in `_forward_headers`), and `digest`/`cl` in
  `_handle_head_blob`

### 3. `occystrap/proxy.py` -- use mixin

- Add `from occystrap.util import SafeHeaderMixin`
- Change class definition to:
  `class ProxyRegistryHandler(SafeHeaderMixin,
  http.server.BaseHTTPRequestHandler):`

Protects ~10 tainted `send_header` calls including
`_forward_headers()` which copies from upstream responses.

### 4. Unit tests

Add `TestSafeHeaderMixin` in `test_proxy.py`:

- **test_mixin_strips_newline**: Verify `\n` stripped
- **test_mixin_strips_carriage_return**: Verify `\r` stripped
- **test_mixin_strips_crlf**: Verify `\r\n` stripped
- **test_mixin_preserves_clean_value**: Clean values pass through
- **test_mixin_handles_non_string**: Integer values work
- **test_mro_order**: SafeHeaderMixin before
  BaseHTTPRequestHandler in both handler classes

### 5. `development/PROJECT-CONSISTENCY-AUDITS.md`

Add new section after "GitHub CodeQL advanced security":

```markdown
## HTTP response header sanitization

Projects using `http.server.BaseHTTPRequestHandler` directly
must override `send_header()` to strip `\r` and `\n` from
header values, preventing HTTP response splitting (CWE-113,
CodeQL `py/http-response-splitting`).

The canonical implementation is `SafeHeaderMixin` in
`occystrap/util.py`. All `BaseHTTPRequestHandler` subclasses
must inherit from this mixin (listed first in the class bases
for correct MRO).

Projects using Flask (kerbside, shakenfist, agent-python)
are already protected by Werkzeug's `Headers` class. When
adding new HTTP servers, prefer Flask. If `http.server` must
be used, always use the `SafeHeaderMixin` pattern.
```

### 6. Documentation updates

Update ARCHITECTURE.md, AGENTS.md, README.md with brief
mention of the SafeHeaderMixin security pattern.

## Files Modified

| File | Changes |
|------|---------|
| `occystrap/util.py` | Add `SafeHeaderMixin` class |
| `occystrap/inputs/dockerpush.py` | Import + mixin inheritance |
| `occystrap/proxy.py` | Import + mixin inheritance |
| `occystrap/tests/test_proxy.py` | `TestSafeHeaderMixin` tests |
| `development/PROJECT-CONSISTENCY-AUDITS.md` | New audit section |
| `ARCHITECTURE.md` | Mention SafeHeaderMixin |
| `AGENTS.md` | Mention SafeHeaderMixin |
| `README.md` | Mention SafeHeaderMixin |

## Verification

```bash
# Unit tests
tox -epy3 -- test_proxy

# Pre-commit (flake8, etc.)
pre-commit run --all-files

# After push: verify CodeQL alerts resolve on next scan
```
