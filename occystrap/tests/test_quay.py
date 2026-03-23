"""Tests for the quay.io API client and URI parsing."""

import unittest
from unittest import mock

from occystrap import uri
from occystrap import util
from occystrap.quay import QuayClient, QuayAPIError, QUAY_API_BASE, resolve_quay_uri


def _mock_response(json_data):
    """Create a mock response object with a .json() method."""
    resp = mock.MagicMock()
    resp.json.return_value = json_data
    return resp


class TestListRepositories(unittest.TestCase):
    """Tests for QuayClient.list_repositories()."""

    @mock.patch('occystrap.quay.util.request_url')
    def test_single_page(self, mock_request):
        """Repos returned from a single page with no next_page key."""
        mock_request.return_value = _mock_response({
            'repositories': [
                {'name': 'nova-api', 'namespace': 'kolla'},
                {'name': 'keystone', 'namespace': 'kolla'},
            ]
        })

        client = QuayClient()
        repos = client.list_repositories('kolla')

        self.assertEqual(repos, ['nova-api', 'keystone'])
        mock_request.assert_called_once()
        call_url = mock_request.call_args[0][1]
        self.assertIn('namespace=kolla', call_url)
        self.assertIn('public=true', call_url)

    @mock.patch('occystrap.quay.util.request_url')
    def test_pagination(self, mock_request):
        """Repos spanning two pages with opaque cursor tokens."""
        mock_request.side_effect = [
            _mock_response({
                'repositories': [
                    {'name': 'nova-api', 'namespace': 'kolla'},
                ],
                'next_page': 'opaque_cursor_token_abc',
            }),
            _mock_response({
                'repositories': [
                    {'name': 'keystone', 'namespace': 'kolla'},
                ],
            }),
        ]

        client = QuayClient()
        repos = client.list_repositories('kolla')

        self.assertEqual(repos, ['nova-api', 'keystone'])
        self.assertEqual(mock_request.call_count, 2)
        second_call_url = mock_request.call_args_list[1][0][1]
        self.assertIn('next_page=opaque_cursor_token_abc',
                      second_call_url)

    @mock.patch('occystrap.quay.util.request_url')
    def test_empty(self, mock_request):
        """Empty org returns empty list."""
        mock_request.return_value = _mock_response({
            'repositories': []
        })

        client = QuayClient()
        repos = client.list_repositories('emptyorg')

        self.assertEqual(repos, [])

    @mock.patch('occystrap.quay.util.request_url')
    def test_auth_header_sent(self, mock_request):
        """When a token is provided, Authorization header is sent."""
        mock_request.return_value = _mock_response({
            'repositories': []
        })

        client = QuayClient(token='my_secret_token')
        client.list_repositories('myorg')

        call_headers = mock_request.call_args[1].get(
            'headers', mock_request.call_args[0][2]
            if len(mock_request.call_args[0]) > 2 else {})
        self.assertEqual(
            call_headers.get('Authorization'),
            'Bearer my_secret_token')

    @mock.patch('occystrap.quay.util.request_url')
    def test_no_auth_header_without_token(self, mock_request):
        """When no token is provided, no Authorization header is sent."""
        mock_request.return_value = _mock_response({
            'repositories': []
        })

        client = QuayClient()
        client.list_repositories('publicorg')

        call_headers = mock_request.call_args[1].get(
            'headers', mock_request.call_args[0][2]
            if len(mock_request.call_args[0]) > 2 else {})
        self.assertNotIn('Authorization', call_headers)

    @mock.patch('occystrap.quay.util.request_url')
    def test_unauthorized_raises_quay_error(self, mock_request):
        """401 from the API raises QuayAPIError with helpful message."""
        mock_request.side_effect = util.UnauthorizedException(
            'API request failed', 'GET',
            '%s/repository' % QUAY_API_BASE,
            401, 'Unauthorized', {})

        client = QuayClient()
        with self.assertRaises(QuayAPIError) as cm:
            client.list_repositories('privateorg')
        self.assertIn('quay.io API token', str(cm.exception))


class TestHasTag(unittest.TestCase):
    """Tests for QuayClient.has_tag()."""

    @mock.patch('occystrap.quay.util.request_url')
    def test_tag_exists(self, mock_request):
        """Tag that exists returns True."""
        mock_request.return_value = _mock_response({
            'tags': [
                {
                    'name': 'latest',
                    'manifest_digest': 'sha256:abc123',
                    'start_ts': 1774047466,
                }
            ],
            'page': 1,
            'has_additional': False,
        })

        client = QuayClient()
        result = client.has_tag('kolla', 'nova-api', 'latest')

        self.assertTrue(result)
        call_url = mock_request.call_args[0][1]
        self.assertIn('/kolla/nova-api/tag/', call_url)
        self.assertIn('specificTag=latest', call_url)
        self.assertIn('onlyActiveTags=true', call_url)
        self.assertIn('limit=1', call_url)

    @mock.patch('occystrap.quay.util.request_url')
    def test_tag_missing(self, mock_request):
        """Tag that does not exist returns False."""
        mock_request.return_value = _mock_response({
            'tags': [],
            'page': 1,
            'has_additional': False,
        })

        client = QuayClient()
        result = client.has_tag('kolla', 'nova-api', 'nonexistent')

        self.assertFalse(result)

    @mock.patch('occystrap.quay.util.request_url')
    def test_repo_not_found(self, mock_request):
        """404 for nonexistent repository returns False."""
        mock_request.side_effect = util.APIException(
            'API request failed', 'GET',
            '%s/repository/kolla/bogus/tag/' % QUAY_API_BASE,
            404, 'Not Found', {})

        client = QuayClient()
        result = client.has_tag('kolla', 'bogus', 'latest')

        self.assertFalse(result)

    @mock.patch('occystrap.quay.util.request_url')
    def test_unauthorized_raises_quay_error(self, mock_request):
        """401 on tag check raises QuayAPIError."""
        mock_request.side_effect = util.UnauthorizedException(
            'API request failed', 'GET',
            '%s/repository/private/repo/tag/' % QUAY_API_BASE,
            401, 'Unauthorized', {})

        client = QuayClient()
        with self.assertRaises(QuayAPIError):
            client.has_tag('private', 'repo', 'latest')

    @mock.patch('occystrap.quay.util.request_url')
    def test_other_api_error_propagates(self, mock_request):
        """Non-404 APIException propagates unchanged."""
        mock_request.side_effect = util.APIException(
            'API request failed', 'GET',
            '%s/repository/kolla/nova-api/tag/' % QUAY_API_BASE,
            500, 'Internal Server Error', {})

        client = QuayClient()
        with self.assertRaises(util.APIException):
            client.has_tag('kolla', 'nova-api', 'latest')


class TestParseQuayUri(unittest.TestCase):
    """Tests for parse_quay_uri()."""

    def test_basic(self):
        """quay://kolla/*:latest parses correctly."""
        spec = uri.parse_uri('quay://kolla/*:latest')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(repo_glob, '*')
        self.assertEqual(tag, 'latest')
        self.assertEqual(options, {})

    def test_with_glob(self):
        """quay://kolla/centos-*:v1 parses glob and tag."""
        spec = uri.parse_uri('quay://kolla/centos-*:v1')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(repo_glob, 'centos-*')
        self.assertEqual(tag, 'v1')

    def test_no_tag_defaults_to_latest(self):
        """quay://kolla/* defaults tag to latest."""
        spec = uri.parse_uri('quay://kolla/*')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(repo_glob, '*')
        self.assertEqual(tag, 'latest')

    def test_no_glob_no_tag(self):
        """quay://kolla defaults glob to * and tag to latest."""
        spec = uri.parse_uri('quay://kolla')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(repo_glob, '*')
        self.assertEqual(tag, 'latest')

    def test_with_token(self):
        """quay://kolla/*:v1?token=x includes token in options."""
        spec = uri.parse_uri('quay://kolla/*:v1?token=abc123')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(repo_glob, '*')
        self.assertEqual(tag, 'v1')
        self.assertEqual(options, {'token': 'abc123'})

    def test_parse_uri_recognizes_quay(self):
        """parse_uri returns scheme='quay' for quay:// URIs."""
        spec = uri.parse_uri('quay://kolla/*:latest')
        self.assertEqual(spec.scheme, 'quay')

    def test_quay_in_input_schemes(self):
        """quay is listed in INPUT_SCHEMES."""
        self.assertIn('quay', uri.INPUT_SCHEMES)

    def test_wrong_scheme_raises(self):
        """parse_quay_uri rejects non-quay URIs."""
        spec = uri.parse_uri('registry://docker.io/library/busybox:latest')
        with self.assertRaises(uri.URIParseError):
            uri.parse_quay_uri(spec)

    def test_missing_namespace_raises(self):
        """quay:// with no namespace raises URIParseError."""
        # urlparse puts empty string in netloc for 'quay://'
        spec = uri.URISpec(scheme='quay', host='', path='', options={})
        with self.assertRaises(uri.URIParseError):
            uri.parse_quay_uri(spec)

    def test_complex_tag(self):
        """Tags with dots and dashes parse correctly."""
        spec = uri.parse_uri('quay://kolla/*:2025.1-debian')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(repo_glob, '*')
        self.assertEqual(tag, '2025.1-debian')


class TestResolveQuayUri(unittest.TestCase):
    """Tests for resolve_quay_uri()."""

    @mock.patch('occystrap.quay.QuayClient')
    def test_basic_resolution(self, mock_client_cls):
        """Resolves repos that have the tag, skips those that don't."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = [
            'nova-api', 'keystone', 'glance-api'
        ]
        client.has_tag.side_effect = [True, False, True]

        results = resolve_quay_uri('kolla', '*', 'latest')

        self.assertEqual(results, [
            ('quay.io', 'kolla/nova-api', 'latest'),
            ('quay.io', 'kolla/glance-api', 'latest'),
        ])
        self.assertEqual(client.has_tag.call_count, 3)

    @mock.patch('occystrap.quay.QuayClient')
    def test_glob_filter(self, mock_client_cls):
        """Glob pattern filters repos before checking tags."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = [
            'nova-api', 'keystone', 'nova-scheduler'
        ]
        # Only nova-* repos should be checked
        client.has_tag.return_value = True

        results = resolve_quay_uri('kolla', 'nova-*', 'latest')

        self.assertEqual(results, [
            ('quay.io', 'kolla/nova-api', 'latest'),
            ('quay.io', 'kolla/nova-scheduler', 'latest'),
        ])
        # keystone should not have been checked
        self.assertEqual(client.has_tag.call_count, 2)

    @mock.patch('occystrap.quay.QuayClient')
    def test_no_matches(self, mock_client_cls):
        """All repos lack the tag, returns empty list."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = ['nova-api', 'keystone']
        client.has_tag.return_value = False

        results = resolve_quay_uri('kolla', '*', 'nonexistent')

        self.assertEqual(results, [])

    @mock.patch('occystrap.quay.QuayClient')
    def test_empty_org(self, mock_client_cls):
        """Empty org returns empty list without checking tags."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = []

        results = resolve_quay_uri('emptyorg', '*', 'latest')

        self.assertEqual(results, [])
        client.has_tag.assert_not_called()

    @mock.patch('occystrap.quay.QuayClient')
    def test_passes_token(self, mock_client_cls):
        """Token is passed through to QuayClient."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = []

        resolve_quay_uri('myorg', '*', 'latest', token='secret')

        mock_client_cls.assert_called_once_with(token='secret')
