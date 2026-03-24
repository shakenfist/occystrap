import json
import os
import threading

import httpx
from oslo_concurrency import processutils
from pbr.version import VersionInfo
from shakenfist_utilities import logs
import time


LOG = logs.setup_console(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # Exponential backoff: 2^attempt seconds


class RequestStats:
    """Thread-safe counters for HTTP request statistics.

    Tracks retries and rate-limit events across all
    threads sharing this instance. Pass to request_url()
    via the stats parameter.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.retries = 0
        self.rate_limits = 0

    def record_retry(self):
        with self._lock:
            self.retries += 1

    def record_rate_limit(self):
        with self._lock:
            self.rate_limits += 1


class RateLimiter:
    """Simple token-bucket rate limiter for HTTP requests.

    Thread-safe: uses a lock to ensure correct spacing
    between requests across multiple threads.
    """

    def __init__(self, rate):
        """Create a rate limiter.

        Args:
            rate: Maximum requests per second.
        """
        self._min_interval = 1.0 / rate
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self):
        """Block until a request is allowed."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


def create_client(http2=True, rate_limit=None):
    """Create an httpx.Client with connection pooling and
    optional HTTP/2.

    Args:
        http2: Enable HTTP/2 negotiation (default True).
            Falls back to HTTP/1.1 if the server does not
            support HTTP/2.
        rate_limit: Max requests per second (None means
            unlimited).

    Returns:
        A tuple of (httpx.Client, RateLimiter or None).
        Caller is responsible for closing the client.
    """
    limits = httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10)
    client = httpx.Client(
        http2=http2,
        limits=limits,
        headers={'User-Agent': get_user_agent()},
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0))
    limiter = RateLimiter(rate_limit) if rate_limit else None
    return client, limiter


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


def request_url(method, url, headers=None, data=None,
                stream=False, auth=None,
                retries=MAX_RETRIES, client=None,
                rate_limiter=None, stats=None):
    """Make an HTTP request with retry logic.

    Uses httpx for connection pooling and HTTP/2 support.

    Args:
        method: HTTP method (GET, POST, PUT, HEAD, etc.).
        url: The URL to request.
        headers: Optional dict of extra headers.
        data: Optional dict to JSON-encode as the body.
        stream: If True, return a streaming response.
            Caller must call r.close() when done, and
            uses r.iter_bytes() instead of r.iter_content().
        auth: Optional (username, password) tuple.
        retries: Max retry count for transient errors
            (connection errors, 429, 5xx).
        client: Optional httpx.Client for connection
            pooling. If None, a temporary client is
            created per request.
        rate_limiter: Optional RateLimiter instance.
        stats: Optional RequestStats instance for
            accumulating retry/rate-limit counts.
    """
    if not headers:
        headers = {}
    headers.update({'User-Agent': get_user_agent()})
    if data:
        headers['Content-Type'] = 'application/json'

    own_client = False
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0))
        own_client = True

    auth_param = None
    if auth:
        auth_param = httpx.BasicAuth(auth[0], auth[1])

    content = json.dumps(data) if data else None

    last_exception = None
    try:
        for attempt in range(retries + 1):
            try:
                if rate_limiter:
                    rate_limiter.acquire()

                if stream:
                    # Use send() with stream=True so the
                    # caller can iterate with iter_bytes().
                    # Caller must call r.close() when done.
                    req = client.build_request(
                        method, url, headers=headers,
                        content=content)
                    r = client.send(
                        req, stream=True,
                        auth=auth_param)
                else:
                    r = client.request(
                        method, url, headers=headers,
                        content=content, auth=auth_param)

                _log_request_debug(
                    method, url, stream, headers, data, r)

                # For error status codes on streaming
                # responses, read the body so .text is
                # available for error messages.
                if stream and r.status_code != 200:
                    r.read()

                if r.status_code == 429:
                    retry_after = r.headers.get(
                        'Retry-After')
                    if retry_after:
                        try:
                            wait_time = int(retry_after)
                        except ValueError:
                            wait_time = (
                                RETRY_BACKOFF_BASE
                                ** attempt)
                    else:
                        wait_time = (
                            RETRY_BACKOFF_BASE ** attempt)
                    if attempt < retries:
                        if stats:
                            stats.record_rate_limit()
                            stats.record_retry()
                        LOG.warning(
                            'Rate limited (429) on %s %s'
                            ' (attempt %d/%d). Retrying'
                            ' in %d seconds...'
                            % (method, url, attempt + 1,
                               retries + 1, wait_time))
                        r.close()
                        time.sleep(wait_time)
                        continue
                    raise APIException(
                        'API request rate limited',
                        method, url, r.status_code,
                        r.text, dict(r.headers))

                if (r.status_code >= 500
                        and attempt < retries):
                    if stats:
                        stats.record_retry()
                    wait_time = (
                        RETRY_BACKOFF_BASE ** attempt)
                    LOG.warning(
                        'Server error (%d) on %s %s'
                        ' (attempt %d/%d). Retrying'
                        ' in %d seconds...'
                        % (r.status_code, method, url,
                           attempt + 1, retries + 1,
                           wait_time))
                    r.close()
                    time.sleep(wait_time)
                    continue

                if r.status_code in STATUS_CODES_TO_ERRORS:
                    raise STATUS_CODES_TO_ERRORS[
                        r.status_code](
                        'API request failed', method,
                        url, r.status_code, r.text,
                        dict(r.headers))

                if r.status_code != 200:
                    raise APIException(
                        'API request failed', method,
                        url, r.status_code, r.text,
                        dict(r.headers))
                return r

            except (httpx.ConnectError,
                    httpx.RemoteProtocolError,
                    httpx.ReadError) as e:
                last_exception = e
                if stats and attempt < retries:
                    stats.record_retry()
                if attempt < retries:
                    wait_time = (
                        RETRY_BACKOFF_BASE ** attempt)
                    LOG.warning(
                        'Request failed (attempt %d/%d):'
                        ' %s. Retrying in %d seconds...'
                        % (attempt + 1, retries + 1,
                           str(e), wait_time))
                    time.sleep(wait_time)
                else:
                    LOG.error(
                        'Request failed after %d'
                        ' attempts: %s'
                        % (retries + 1, str(e)))

        raise last_exception

    finally:
        if own_client:
            client.close()


def _log_request_debug(method, url, stream, headers,
                       data, r):
    """Log request and response details at debug level."""
    LOG.debug(
        '--------------------------------------------'
        '---')
    LOG.debug(
        'API client requested: %s %s (stream=%s)'
        % (method, url, stream))
    for h in headers:
        LOG.debug('Header: %s = %s' % (h, headers[h]))
    if data:
        LOG.debug(
            'Data:\n    %s'
            % ('\n    '.join(
                json.dumps(
                    data, indent=4,
                    sort_keys=True).split('\n'))))
    LOG.debug(
        'API client response: code = %s'
        % r.status_code)
    for h in r.headers:
        LOG.debug(
            'Header: %s = %s' % (h, r.headers[h]))
    if not stream:
        if r.text:
            try:
                LOG.debug(
                    'Data:\n    %s'
                    % ('\n    '.join(
                        json.dumps(
                            json.loads(r.text),
                            indent=4,
                            sort_keys=True
                        ).split('\n'))))
            except Exception:
                LOG.debug(
                    'Text:\n    %s'
                    % ('\n    '.join(
                        r.text.split('\n'))))
    else:
        LOG.debug(
            'Result content not logged for '
            'streaming requests')
    LOG.debug(
        '--------------------------------------------'
        '---')


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
