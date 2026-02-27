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

## Approach: SafeHeaderMixin

Override `send_header()` via a shared mixin class that both HTTP
handler classes inherit from. The mixin strips `\r` and `\n` from
header values before delegating to `BaseHTTPRequestHandler`.

This is the most "mandatory" approach -- impossible to bypass
without deliberately calling `super().send_header()` from two
levels up. All existing and future `send_header()` calls are
automatically protected.

CodeQL's `ReplaceLineBreaksSanitizer` recognizes `.replace('\n',
...)` calls as sanitizers, and its interprocedural taint tracking
follows through method calls, so the mixin pattern should clear
the alerts.

Other shakenfist projects (kerbside, shakenfist, agent-python)
use Flask/Werkzeug, which already rejects header values containing
line breaks. Only occystrap uses raw `http.server`.

## Changes

### 1. `occystrap/util.py` -- add SafeHeaderMixin

```python
class SafeHeaderMixin:
    """Mixin for BaseHTTPRequestHandler subclasses
    that sanitizes header values to prevent HTTP
    response splitting (CWE-113).

    Strips CR and LF from header values before
    passing to BaseHTTPRequestHandler.send_header().
    """

    def send_header(self, keyword, value):
        """Strip CR/LF from values to prevent HTTP
        response splitting."""
        value = str(value).replace(
            '\r', '').replace('\n', '')
        super().send_header(keyword, value)
```

### 2. `occystrap/inputs/dockerpush.py` -- use mixin

- Add `from occystrap.util import SafeHeaderMixin`
- Change class definition to:
  `class EmbeddedRegistryHandler(SafeHeaderMixin,
  http.server.BaseHTTPRequestHandler):`
- Mixin **must** come first in MRO

Fixes 7 open CodeQL alerts (lines 201, 216, 263, 320, 322,
428, 432).

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
