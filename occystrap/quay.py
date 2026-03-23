"""Quay.io REST API v1 client for repository discovery.

This module wraps the quay.io proprietary API for listing repositories
within an organization and checking tag existence. It is used by the
quay:// URI scheme to discover images for bulk operations.

Note: this is NOT the Docker Registry V2 API. The quay.io API v1 is a
separate REST API at /api/v1/ that provides organization-level operations
not available via the standard registry protocol.
"""

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

    def list_repositories(self, namespace):
        """List all repositories in a quay.io namespace.

        Handles pagination automatically using the opaque cursor
        tokens returned by the API. The quay.io API returns pages
        of 100 repositories.

        Args:
            namespace: The organization / namespace name
                (e.g., 'kolla').

        Returns:
            A list of repository name strings (the name within
            the namespace, not the full namespace/name path).
            For example, ['nova-api', 'keystone', 'glance-api'].
        """
        repos = []
        url = '%s/repository?namespace=%s&public=true' % (
            QUAY_API_BASE, namespace)

        page_num = 0
        while url:
            page_num += 1
            LOG.info('Listing repositories in %s (page %d)...'
                     % (namespace, page_num))

            r = self._request(url)
            data = r.json()

            page_repos = data.get('repositories', [])
            for repo in page_repos:
                repos.append(repo['name'])

            next_page = data.get('next_page')
            if next_page:
                url = (
                    '%s/repository?namespace=%s&public=true'
                    '&next_page=%s'
                    % (QUAY_API_BASE, namespace, next_page)
                )
            else:
                url = None

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
            True if the tag exists and is active, False otherwise.
            Also returns False if the repository does not exist
            (404 from the API).
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
                return False
            raise

        data = r.json()
        tags = data.get('tags', [])
        exists = len(tags) > 0

        if exists:
            LOG.debug('Tag %s:%s/%s exists (digest: %s)'
                      % (tag, namespace, repo,
                         tags[0].get('manifest_digest', 'unknown')))
        else:
            LOG.debug('Tag %s:%s/%s does not exist'
                      % (tag, namespace, repo))

        return exists


def resolve_quay_uri(namespace, repo_glob, tag, token=None):
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

    Returns:
        List of ('quay.io', 'namespace/repo', tag) tuples
        for each repo that matches the glob and has the tag.
    """
    client = QuayClient(token=token)
    all_repos = client.list_repositories(namespace)

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
        if client.has_tag(namespace, repo, tag):
            results.append(('quay.io', '%s/%s' % (namespace, repo), tag))

    LOG.info('Found %d images matching %s/%s:%s'
             % (len(results), namespace, repo_glob, tag))
    return results
