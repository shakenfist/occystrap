"""Quay.io REST API v1 client for repository discovery.

This module wraps the quay.io proprietary API for listing repositories
within an organization and checking tag existence. It is used by the
quay:// URI scheme to discover images for bulk operations.

Note: this is NOT the Docker Registry V2 API. The quay.io API v1 is a
separate REST API at /api/v1/ that provides organization-level operations
not available via the standard registry protocol.
"""

import calendar
import datetime
from fnmatch import fnmatch

from shakenfist_utilities import logs

from occystrap import util


LOG = logs.setup_console(__name__)

QUAY_API_BASE = 'https://quay.io/api/v1'


class QuayAPIError(Exception):
    """Raised when the quay.io API returns an unexpected error."""
    pass


class QuayClient:
    """Client for the quay.io REST API v1.

    Provides methods for listing repositories in an organization
    and checking whether a specific tag exists for a repository.

    Args:
        token: Optional quay.io API bearer token for accessing
            private organizations. Not required for public repos.
    """

    def __init__(self, token=None):
        self.token = token

    def _headers(self):
        """Build request headers, including auth if a token is set."""
        headers = {}
        if self.token:
            headers['Authorization'] = 'Bearer %s' % self.token
        return headers

    def _request(self, url):
        """Make an authenticated GET request to the quay.io API.

        Wraps util.request_url with quay.io auth headers. Translates
        401 errors into a more helpful message mentioning quay.io
        API tokens.

        Returns:
            The requests.Response object.

        Raises:
            QuayAPIError: On authentication failure or unexpected errors.
        """
        try:
            return util.request_url('GET', url, headers=self._headers())
        except util.UnauthorizedException:
            raise QuayAPIError(
                'Authentication failed for quay.io API. '
                'Provide a valid quay.io API token for '
                'accessing private organizations.'
            )

    def list_repositories(self, namespace, since_ts=None):
        """List repositories in a quay.io namespace.

        Handles pagination automatically using the opaque cursor
        tokens returned by the API. The quay.io API returns pages
        of 100 repositories.

        When since_ts is provided, the API is asked for
        last_modified timestamps and repositories older than
        since_ts are filtered out during listing. This avoids
        expensive per-repo tag checks on stale repositories.

        Args:
            namespace: The organization / namespace name
                (e.g., 'kolla').
            since_ts: Optional Unix timestamp. If set, only
                return repositories whose last_modified is
                on or after this timestamp.

        Returns:
            A list of repository name strings (the name within
            the namespace, not the full namespace/name path).
            For example, ['nova-api', 'keystone', 'glance-api'].
        """
        repos = []
        skipped = 0
        base_params = 'namespace=%s&public=true' % namespace
        if since_ts is not None:
            base_params += '&last_modified=true'
        url = '%s/repository?%s' % (QUAY_API_BASE, base_params)

        page_num = 0
        while url:
            page_num += 1
            LOG.info('Listing repositories in %s (page %d)...'
                     % (namespace, page_num))

            r = self._request(url)
            data = r.json()

            page_repos = data.get('repositories', [])
            for repo in page_repos:
                if since_ts is not None:
                    repo_ts = repo.get('last_modified') or 0
                    if repo_ts < since_ts:
                        skipped += 1
                        continue
                repos.append(repo['name'])

            next_page = data.get('next_page')
            if next_page:
                url = (
                    '%s/repository?%s&next_page=%s'
                    % (QUAY_API_BASE, base_params, next_page)
                )
            else:
                url = None

        if skipped:
            LOG.info('Found %d repositories in %s '
                     '(%d skipped as older than since)'
                     % (len(repos), namespace, skipped))
        else:
            LOG.info('Found %d repositories in %s'
                     % (len(repos), namespace))
        return repos

    def has_tag(self, namespace, repo, tag):
        """Check whether a repository has a specific active tag.

        Uses the specificTag filter and limit=1 to minimize the
        response size. Only checks active (non-expired) tags.

        Args:
            namespace: The organization / namespace name.
            repo: The repository name within the namespace.
            tag: The tag name to check for.

        Returns:
            A dict with tag metadata (name, start_ts,
            manifest_digest, last_modified, etc.) if the tag
            exists and is active, or None if it does not exist
            or the repository is not found (404).
        """
        url = (
            '%s/repository/%s/%s/tag/'
            '?specificTag=%s&onlyActiveTags=true&limit=1'
            % (QUAY_API_BASE, namespace, repo, tag)
        )

        try:
            r = self._request(url)
        except QuayAPIError:
            raise
        except util.APIException as e:
            # APIException args: (message, method, url, status_code, text, headers)
            if len(e.args) >= 4 and e.args[3] == 404:
                LOG.debug('Repository %s/%s not found'
                          % (namespace, repo))
                return None
            raise

        data = r.json()
        tags = data.get('tags', [])

        if tags:
            tag_info = tags[0]
            LOG.debug('Tag %s:%s/%s exists (digest: %s)'
                      % (tag, namespace, repo,
                         tag_info.get('manifest_digest', 'unknown')))
            return tag_info

        LOG.debug('Tag %s:%s/%s does not exist'
                  % (tag, namespace, repo))
        return None


def resolve_quay_uri(namespace, repo_glob, tag, token=None, since=None):
    """Resolve a quay:// URI into matching image references.

    Lists all repositories in the namespace, filters by the
    glob pattern, checks tag existence for each match, and
    returns a list of (registry, image, tag) tuples suitable
    for constructing registry.Image inputs.

    Args:
        namespace: Quay.io organization name.
        repo_glob: Glob pattern for repository names
            (e.g., '*', 'nova-*').
        tag: Exact tag name to match.
        token: Optional quay.io API token for private orgs.
        since: Optional datetime.date. If set, only include
            images whose tag was created/updated on or after
            this date.

    Returns:
        List of ('quay.io', 'namespace/repo', tag) tuples
        for each repo that matches the glob and has the tag.
    """
    client = QuayClient(token=token)

    # Convert since date to unix timestamp for comparison
    since_ts = None
    if since is not None:
        since_ts = calendar.timegm(since.timetuple())

    # Pass since_ts to list_repositories so stale repos are
    # filtered during listing, avoiding expensive per-repo
    # tag checks.
    all_repos = client.list_repositories(
        namespace, since_ts=since_ts)

    # Filter by glob pattern
    matching_repos = [r for r in all_repos if fnmatch(r, repo_glob)]
    LOG.info('Glob %r matched %d of %d repositories'
             % (repo_glob, len(matching_repos), len(all_repos)))

    # Check tag existence for each matching repo
    results = []
    for i, repo in enumerate(matching_repos):
        LOG.info('Checking tag %r for repo %d of %d: %s/%s'
                 % (tag, i + 1, len(matching_repos),
                    namespace, repo))
        tag_info = client.has_tag(namespace, repo, tag)
        if not tag_info:
            continue

        # Filter by tag age if since is set
        if since_ts is not None:
            tag_ts = tag_info.get('start_ts', 0)
            if tag_ts < since_ts:
                tag_date = datetime.date.fromtimestamp(tag_ts)
                LOG.debug(
                    'Skipping %s/%s: tag %r is from %s, '
                    'before since=%s'
                    % (namespace, repo, tag, tag_date, since))
                continue

        results.append(('quay.io', '%s/%s' % (namespace, repo), tag))

    LOG.info('Found %d images matching %s/%s:%s'
             % (len(results), namespace, repo_glob, tag))
    return results
