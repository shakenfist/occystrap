"""Tests for the quay.io API client."""

import unittest
from unittest import mock

from occystrap import util
from occystrap.quay import QuayClient, QuayAPIError, QUAY_API_BASE


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
