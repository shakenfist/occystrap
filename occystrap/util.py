import json
import os

from oslo_concurrency import processutils
from pbr.version import VersionInfo
import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError
from shakenfist_utilities import logs
import time


LOG = logs.setup_console(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # Exponential backoff: 2^attempt seconds


class APIException(Exception):
    pass


class UnauthorizedException(Exception):
    pass


STATUS_CODES_TO_ERRORS = {
    401: UnauthorizedException
}


def get_user_agent():
    try:
        version = VersionInfo('occystrap').version_string()
    except Exception:
        version = '0.0.0'
    return 'Mozilla/5.0 (Ubuntu; Linux x86_64) Occy Strap/%s' % version


def request_url(method, url, headers=None, data=None, stream=False, auth=None,
                retries=MAX_RETRIES):
    if not headers:
        headers = {}
    headers.update({'User-Agent': get_user_agent()})
    if data:
        headers['Content-Type'] = 'application/json'

    last_exception = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url,
                                 data=json.dumps(data),
                                 headers=headers,
                                 stream=stream,
                                 auth=auth)

            LOG.debug('-------------------------------------------------------')
            LOG.debug('API client requested: %s %s (stream=%s)'
                      % (method, url, stream))
            for h in headers:
                LOG.debug('Header: %s = %s' % (h, headers[h]))
            if data:
                LOG.debug('Data:\n    %s'
                          % ('\n    '.join(json.dumps(data,
                                                      indent=4,
                                                      sort_keys=True).split('\n'))))
            LOG.debug('API client response: code = %s' % r.status_code)
            for h in r.headers:
                LOG.debug('Header: %s = %s' % (h, r.headers[h]))
            if not stream:
                if r.text:
                    try:
                        LOG.debug('Data:\n    %s'
                                  % ('\n    '.join(json.dumps(
                                      json.loads(r.text),
                                      indent=4,
                                      sort_keys=True).split('\n'))))
                    except Exception:
                        LOG.debug('Text:\n    %s'
                                  % ('\n    '.join(r.text.split('\n'))))
            else:
                LOG.debug('Result content not logged for streaming requests')
            LOG.debug('-------------------------------------------------------')

            if r.status_code in STATUS_CODES_TO_ERRORS:
                raise STATUS_CODES_TO_ERRORS[r.status_code](
                    'API request failed', method, url, r.status_code,
                    r.text, r.headers)

            if r.status_code != 200:
                raise APIException(
                    'API request failed', method, url, r.status_code,
                    r.text, r.headers)
            return r

        except (ChunkedEncodingError, ConnectionError) as e:
            last_exception = e
            if attempt < retries:
                wait_time = RETRY_BACKOFF_BASE ** attempt
                LOG.warning('Request failed (attempt %d/%d): %s. '
                            'Retrying in %d seconds...'
                            % (attempt + 1, retries + 1, str(e), wait_time))
                time.sleep(wait_time)
            else:
                LOG.error('Request failed after %d attempts: %s'
                          % (retries + 1, str(e)))

    raise last_exception


def format_size(size_bytes):
    """Format a size in bytes as a human-readable string."""
    if size_bytes is None:
        return 'N/A'
    if size_bytes < 1024:
        return '%d B' % size_bytes
    elif size_bytes < 1024 * 1024:
        return '%.1f KB' % (size_bytes / 1024)
    elif size_bytes < 1024 * 1024 * 1024:
        return '%.1f MB' % (
            size_bytes / (1024 * 1024))
    else:
        return '%.1f GB' % (
            size_bytes / (1024 * 1024 * 1024))


def execute(command, check_exit_code=[0], env_variables=None,
            cwd=None):
    return processutils.execute(
        command, check_exit_code=check_exit_code,
        env_variables=env_variables, shell=True, cwd=cwd)


def sanitize_header_value(value):
    """Strip CR/LF from an HTTP header value to
    prevent HTTP response splitting (CWE-113).

    Call this on any user-controlled or external value
    before passing it to send_header(). This ensures
    CodeQL's taint tracking sees the sanitization on
    the data flow path before the sink.
    """
    return str(value).replace('\r', '').replace(
        '\n', '')


class PathEscapeError(Exception):
    """Raised when a constructed path escapes its
    intended base directory."""
    pass


def safe_path_join(base, *components):
    """Join path components and verify the result stays
    within the base directory (CWE-22 prevention).

    Resolves the joined path to an absolute path and
    checks it is still under base. Raises
    PathEscapeError if the result would escape.

    Use this on any path constructed from
    user-controlled data (image names, tags, digests)
    before passing it to open() or os.makedirs().
    """
    base = os.path.realpath(base)
    joined = os.path.realpath(
        os.path.join(base, *components))
    if not joined.startswith(base + os.sep) \
            and joined != base:
        raise PathEscapeError(
            'Path %r escapes base directory %r'
            % (joined, base))
    return joined


class SafeHeaderMixin:
    """Mixin for BaseHTTPRequestHandler subclasses
    that sanitizes header values to prevent HTTP
    response splitting (CWE-113).

    Strips CR and LF from header values before
    passing to BaseHTTPRequestHandler.send_header().
    Must be listed first in class bases for correct
    MRO.
    """

    def send_header(self, keyword, value):
        """Strip CR/LF from values to prevent HTTP
        response splitting."""
        super().send_header(
            keyword, sanitize_header_value(value))
