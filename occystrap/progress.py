"""Progress tracking for slow operations.

Provides a unified progress API that uses tqdm progress
bars in interactive (TTY) sessions and falls back to
periodic log messages in non-interactive contexts.

Usage::

    from occystrap.progress import LayerProgress

    # Overall layer progress (N of M) -- INFO level
    with LayerProgress(total=10, desc='Layers') as lp:
        for i in range(10):
            # ... process layer ...
            lp.update(1)

    # Byte-level progress for a single download -- DEBUG
    with LayerProgress(
            total=size, desc='Layer abc123',
            unit='B', unit_scale=True,
            position=1, log_level='debug') as bp:
        for chunk in stream:
            bp.update(len(chunk))

When stderr is not a TTY (piped, CI, cron), tqdm is
disabled and a periodic log message is emitted instead.
The log_level parameter controls the level used for
non-TTY fallback messages (default: 'info'). Use 'debug'
for byte-level per-layer progress to avoid noise.
"""

import sys
import time

from shakenfist_utilities import logs
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm


LOG = logs.setup_console(__name__)

# How often to emit fallback log messages (seconds)
_FALLBACK_INTERVAL = 30


class LayerProgress:
    """Progress tracker with tqdm/logging fallback.

    In interactive mode (TTY), displays a tqdm progress
    bar. In non-interactive mode, emits periodic log
    messages at the configured level.

    Can be used as a context manager or manually via
    update()/close().
    """

    def __init__(self, total=None, desc='',
                 unit='it', unit_scale=False,
                 position=None, log_level='info'):
        """Initialize the progress tracker.

        Args:
            total: Total expected count (layers, bytes).
            desc: Short description for the progress bar.
            unit: Unit label (default: 'it').
            unit_scale: Auto-scale units (for bytes).
            position: Bar position for stacking (0-based).
            log_level: Level for non-TTY fallback messages
                ('info' or 'debug'). Use 'debug' for
                byte-level per-layer progress.
        """
        self._total = total
        self._desc = desc
        self._unit = unit
        self._unit_scale = unit_scale
        self._position = position
        self._log_level = log_level
        self._is_tty = sys.stderr.isatty()
        self._current = 0
        self._last_log_time = time.time()
        self._bar = None

    def _log(self, msg):
        """Emit a log message at the configured level."""
        if self._log_level == 'debug':
            LOG.debug(msg)
        else:
            LOG.info(msg)

    def __enter__(self):
        if self._is_tty:
            self._bar = tqdm(
                total=self._total,
                desc=self._desc,
                unit=self._unit,
                unit_scale=self._unit_scale,
                position=self._position,
                file=sys.stderr,
                leave=True,
            )
        else:
            if self._total:
                self._log('%s: starting (%d %s)'
                          % (self._desc, self._total,
                             self._unit))
            else:
                self._log('%s: starting' % self._desc)
        return self

    def update(self, n=1):
        """Advance the progress by n units."""
        self._current += n
        if self._bar is not None:
            self._bar.update(n)
        else:
            now = time.time()
            if now - self._last_log_time \
                    >= _FALLBACK_INTERVAL:
                self._last_log_time = now
                if self._total:
                    pct = (self._current * 100
                           // self._total)
                    self._log(
                        '%s: %d/%d %s (%d%%)'
                        % (self._desc, self._current,
                           self._total, self._unit,
                           pct))
                else:
                    self._log(
                        '%s: %d %s'
                        % (self._desc, self._current,
                           self._unit))

    def close(self):
        """Close the progress tracker."""
        if self._bar is not None:
            self._bar.close()
            self._bar = None

    def __exit__(self, *args):
        self.close()


def redirect_logging():
    """Context manager to redirect logging through tqdm.

    Use this around code that uses both tqdm progress bars
    and logging to prevent garbled output. In non-TTY
    contexts, this is a no-op.

    Usage::

        with redirect_logging():
            with LayerProgress(...) as p:
                LOG.info('This message won't garble')
                p.update(1)
    """
    if sys.stderr.isatty():
        return logging_redirect_tqdm()

    # No-op context manager for non-TTY
    class _NoOp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return _NoOp()
