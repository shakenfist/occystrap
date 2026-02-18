# Redirect ConsoleLoggingHandler to stderr.
# shakenfist_utilities.logs.ConsoleLoggingHandler uses
# print() which writes to stdout. For CLI tools, log
# output should go to stderr to avoid contaminating
# machine-readable output (e.g., JSON mode).
#
# This monkeypatch runs at import time, which is safe
# because occystrap is a CLI tool, never imported as a
# library. If that changes, upstream a stream parameter
# to ConsoleLoggingHandler instead.
import sys

from shakenfist_utilities.logs import ConsoleLoggingHandler


def _stderr_emit(self, record):
    try:
        print(self.format(record), file=sys.stderr)
    except Exception:
        self.handleError(record)


ConsoleLoggingHandler.emit = _stderr_emit
