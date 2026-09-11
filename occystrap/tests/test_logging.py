"""Tests for the process wide logging setup in main.configure_logging().

These assert on what is printed rather than on logger levels, because
what matters is observable: occystrap's own milestones appear exactly
once and not twice, a dependency's warnings appear at all, and a
dependency's INFO chatter stays behind --debug. httpx logs a line per
HTTP request at INFO, so "libraries are quiet by default" is the
difference between a readable run and several hundred lines of it.
"""

import contextlib
import io
import logging
import unittest

from shakenfist_utilities import logs

from occystrap import main


# The name of a logger set up the way every occystrap module sets one up:
# its own console handler, sitting under the occystrap package. That shape
# is the one which doubles if propagation is left on once the root logger
# has a handler of its own.
MODULE_LOGGER = 'occystrap.tests.fake_module'


class ConfigureLoggingTestCase(unittest.TestCase):
    """configure_logging() reconfigures the process, so restore all of it."""

    def setUp(self):
        self.saved_root_handlers = logging.root.handlers
        self.saved_root_level = logging.root.level
        self.saved_loggers = {}
        for name in ('occystrap', 'occystrap.main', MODULE_LOGGER, 'httpx'):
            log = logging.getLogger(name)
            self.saved_loggers[name] = (log.level, log.propagate,
                                        list(log.handlers))

        # basicConfig() does nothing once root has a handler, and the
        # handler it installs writes to whichever sys.stderr existed when
        # it was installed. Clearing the list makes each test start from
        # a process which has not configured logging yet, and lets the
        # handler find the capture buffer.
        logging.root.handlers = []

        # Stand the module logger up before configure_logging() runs, as
        # importing an occystrap module does.
        self.module_log = logs.setup_console(MODULE_LOGGER)

    def tearDown(self):
        logging.root.handlers = self.saved_root_handlers
        logging.root.setLevel(self.saved_root_level)
        for name, (level, propagate, handlers) in self.saved_loggers.items():
            log = logging.getLogger(name)
            log.setLevel(level)
            log.propagate = propagate
            log.handlers = handlers

    @contextlib.contextmanager
    def _captured(self):
        """Capture both streams.

        Module loggers print() to stdout and the root handler writes to
        stderr, and a test which only watched one of them would count a
        doubled line as a single one.
        """
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err

    def _run_cli(self, args, probe):
        """Run the real command line, which is where --debug is handled.

        The group callback is the code under test, so it is reached
        through click rather than called directly, and a throwaway
        subcommand gives it something to dispatch to.
        """
        main.cli.command(name='_logging_probe')(probe)
        try:
            with main.cli.make_context('occystrap', args) as ctx:
                main.cli.invoke(ctx)
        finally:
            main.cli.commands.pop('_logging_probe', None)

    def test_module_milestone_printed_once(self):
        with self._captured() as (out, err):
            main.configure_logging()
            self.module_log.info('a module milestone')
        printed = out.getvalue() + err.getvalue()
        self.assertEqual(1, printed.count('a module milestone'), printed)

    def test_library_warning_is_printed(self):
        with self._captured() as (out, err):
            main.configure_logging()
            logging.getLogger('httpx').warning('a library warning')
        printed = out.getvalue() + err.getvalue()
        self.assertEqual(1, printed.count('a library warning'), printed)

    def test_library_info_is_quiet_by_default(self):
        with self._captured() as (out, err):
            main.configure_logging()
            logging.getLogger('httpx').info('HTTP Request: GET https://example.com')
        printed = out.getvalue() + err.getvalue()
        self.assertNotIn('HTTP Request', printed)

    def test_debug_surfaces_library_info(self):
        def probe():
            logging.getLogger('httpx').info('HTTP Request: GET https://example.com')
            self.module_log.debug('a module debug line')

        with self._captured() as (out, err):
            self._run_cli(['--debug', '_logging_probe'], probe)
        printed = out.getvalue() + err.getvalue()
        self.assertIn('HTTP Request', printed)
        self.assertEqual(1, printed.count('a module debug line'), printed)

    def test_verbose_leaves_libraries_quiet(self):
        def probe():
            logging.getLogger('httpx').info('HTTP Request: GET https://example.com')
            self.module_log.debug('a module debug line')

        with self._captured() as (out, err):
            self._run_cli(['--verbose', '_logging_probe'], probe)
        printed = out.getvalue() + err.getvalue()
        self.assertNotIn('HTTP Request', printed)
        self.assertEqual(1, printed.count('a module debug line'), printed)
