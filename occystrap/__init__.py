# Redirect ConsoleLoggingHandler to stderr.
# shakenfist_utilities.logs.ConsoleLoggingHandler uses
# print() which writes to stdout. For CLI tools, log
# output should go to stderr to avoid contaminating
# machine-readable output (e.g., JSON mode).
import logging
import sys

from shakenfist_utilities.logs import ConsoleLoggingHandler


def _stderr_emit(self, record):
    try:
        self.level = logging._nameToLevel[
            record.levelname.upper()]
        print(self.format(record), file=sys.stderr)
    except Exception:
        self.handleError(record)


ConsoleLoggingHandler.emit = _stderr_emit
