"""Tests for the quay.io API client, URI parsing, and command integration."""

import datetime
import json
import unittest
from unittest import mock

from click.testing import CliRunner

from occystrap import uri
from occystrap import util
from occystrap.main import cli
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

        client = QuayClient(client=mock.MagicMock())
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

        client = QuayClient(client=mock.MagicMock())
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

        client = QuayClient(client=mock.MagicMock())
        repos = client.list_repositories('emptyorg')

        self.assertEqual(repos, [])

    @mock.patch('occystrap.quay.util.request_url')
    def test_auth_header_sent(self, mock_request):
        """When a token is provided, Authorization header is sent."""
        mock_request.return_value = _mock_response({
            'repositories': []
        })

        client = QuayClient(token='my_secret_token', client=mock.MagicMock())
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

        client = QuayClient(client=mock.MagicMock())
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

        client = QuayClient(client=mock.MagicMock())
        with self.assertRaises(QuayAPIError) as cm:
            client.list_repositories('privateorg')
        self.assertIn('quay.io API token', str(cm.exception))

    @mock.patch('occystrap.quay.util.request_url')
    def test_since_ts_filters_old_repos(self, mock_request):
        """Repos older than since_ts are excluded during listing."""
        # 2025-03-15 and 2021-06-15 as unix timestamps
        new_ts = 1742025600
        old_ts = 1623772800
        mock_request.return_value = _mock_response({
            'repositories': [
                {'name': 'new-repo', 'namespace': 'kolla',
                 'last_modified': new_ts},
                {'name': 'old-repo', 'namespace': 'kolla',
                 'last_modified': old_ts},
            ]
        })

        client = QuayClient(client=mock.MagicMock())
        # since_ts = 2024-01-01
        repos = client.list_repositories('kolla', since_ts=1704067200)

        self.assertEqual(repos, ['new-repo'])
        call_url = mock_request.call_args[0][1]
        self.assertIn('last_modified=true', call_url)

    @mock.patch('occystrap.quay.util.request_url')
    def test_since_ts_none_returns_all(self, mock_request):
        """Without since_ts, all repos are returned."""
        mock_request.return_value = _mock_response({
            'repositories': [
                {'name': 'new-repo', 'namespace': 'kolla'},
                {'name': 'old-repo', 'namespace': 'kolla'},
            ]
        })

        client = QuayClient(client=mock.MagicMock())
        repos = client.list_repositories('kolla', since_ts=None)

        self.assertEqual(repos, ['new-repo', 'old-repo'])
        call_url = mock_request.call_args[0][1]
        self.assertNotIn('last_modified', call_url)

    @mock.patch('occystrap.quay.util.request_url')
    def test_since_ts_handles_null_last_modified(self, mock_request):
        """Repos with null last_modified are treated as old."""
        mock_request.return_value = _mock_response({
            'repositories': [
                {'name': 'null-repo', 'namespace': 'kolla',
                 'last_modified': None},
                {'name': 'missing-repo', 'namespace': 'kolla'},
            ]
        })

        client = QuayClient(client=mock.MagicMock())
        repos = client.list_repositories('kolla', since_ts=1704067200)

        self.assertEqual(repos, [])


class TestHasTag(unittest.TestCase):
    """Tests for QuayClient.has_tag()."""

    @mock.patch('occystrap.quay.util.request_url')
    def test_tag_exists(self, mock_request):
        """Tag that exists returns tag metadata dict."""
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

        client = QuayClient(client=mock.MagicMock())
        result = client.has_tag('kolla', 'nova-api', 'latest')

        self.assertIsNotNone(result)
        self.assertEqual(result['name'], 'latest')
        self.assertEqual(result['start_ts'], 1774047466)
        self.assertEqual(result['manifest_digest'], 'sha256:abc123')
        call_url = mock_request.call_args[0][1]
        self.assertIn('/kolla/nova-api/tag/', call_url)
        self.assertIn('specificTag=latest', call_url)
        self.assertIn('onlyActiveTags=true', call_url)
        self.assertIn('limit=1', call_url)

    @mock.patch('occystrap.quay.util.request_url')
    def test_tag_missing(self, mock_request):
        """Tag that does not exist returns None."""
        mock_request.return_value = _mock_response({
            'tags': [],
            'page': 1,
            'has_additional': False,
        })

        client = QuayClient(client=mock.MagicMock())
        result = client.has_tag('kolla', 'nova-api', 'nonexistent')

        self.assertIsNone(result)

    @mock.patch('occystrap.quay.util.request_url')
    def test_repo_not_found(self, mock_request):
        """404 for nonexistent repository returns None."""
        mock_request.side_effect = util.APIException(
            'API request failed', 'GET',
            '%s/repository/kolla/bogus/tag/' % QUAY_API_BASE,
            404, 'Not Found', {})

        client = QuayClient(client=mock.MagicMock())
        result = client.has_tag('kolla', 'bogus', 'latest')

        self.assertIsNone(result)

    @mock.patch('occystrap.quay.util.request_url')
    def test_unauthorized_raises_quay_error(self, mock_request):
        """401 on tag check raises QuayAPIError."""
        mock_request.side_effect = util.UnauthorizedException(
            'API request failed', 'GET',
            '%s/repository/private/repo/tag/' % QUAY_API_BASE,
            401, 'Unauthorized', {})

        client = QuayClient(client=mock.MagicMock())
        with self.assertRaises(QuayAPIError):
            client.has_tag('private', 'repo', 'latest')

    @mock.patch('occystrap.quay.util.request_url')
    def test_other_api_error_propagates(self, mock_request):
        """Non-404 APIException propagates unchanged."""
        mock_request.side_effect = util.APIException(
            'API request failed', 'GET',
            '%s/repository/kolla/nova-api/tag/' % QUAY_API_BASE,
            500, 'Internal Server Error', {})

        client = QuayClient(client=mock.MagicMock())
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

    def test_with_since(self):
        """quay://kolla/*:latest?since=2024-01-01 includes since in options."""
        spec = uri.parse_uri('quay://kolla/*:latest?since=2024-01-01')
        namespace, repo_glob, tag, options = uri.parse_quay_uri(spec)
        self.assertEqual(namespace, 'kolla')
        self.assertEqual(options, {'since': '2024-01-01'})


class TestResolveQuayUri(unittest.TestCase):
    """Tests for resolve_quay_uri()."""

    @mock.patch('occystrap.quay.QuayClient')
    def test_basic_resolution(self, mock_client_cls):
        """Resolves repos that have the tag, skips those that don't."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = [
            'nova-api', 'keystone', 'glance-api'
        ]
        tag_info = {'name': 'latest', 'start_ts': 1774047466}

        def has_tag_side_effect(ns, repo, tag):
            if repo in ('nova-api', 'glance-api'):
                return tag_info
            return None

        client.has_tag.side_effect = has_tag_side_effect

        results = resolve_quay_uri('kolla', '*', 'latest')

        self.assertCountEqual(results, [
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
        client.has_tag.return_value = {'name': 'latest', 'start_ts': 1774047466}

        results = resolve_quay_uri('kolla', 'nova-*', 'latest')

        self.assertCountEqual(results, [
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
        client.has_tag.return_value = None

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

    @mock.patch('occystrap.quay.QuayClient')
    def test_since_filters_old_tags(self, mock_client_cls):
        """Tags older than since date are excluded."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = ['nova-api']
        # start_ts = 2021-06-15 (well before since=2024-01-01)
        client.has_tag.return_value = {
            'name': 'latest', 'start_ts': 1623772800,
        }

        results = resolve_quay_uri(
            'kolla', '*', 'latest',
            since=datetime.date(2024, 1, 1))

        self.assertEqual(results, [])

    @mock.patch('occystrap.quay.QuayClient')
    def test_since_includes_new_tags(self, mock_client_cls):
        """Tags newer than since date are included."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = ['nova-api']
        # start_ts = 2025-03-15 (after since=2024-01-01)
        client.has_tag.return_value = {
            'name': 'latest', 'start_ts': 1742025600,
        }

        results = resolve_quay_uri(
            'kolla', '*', 'latest',
            since=datetime.date(2024, 1, 1))

        self.assertEqual(results, [
            ('quay.io', 'kolla/nova-api', 'latest'),
        ])

    @mock.patch('occystrap.quay.QuayClient')
    def test_since_none_skips_filter(self, mock_client_cls):
        """since=None does not filter anything."""
        client = mock_client_cls.return_value
        client.list_repositories.return_value = ['nova-api']
        # Very old tag, but since is None
        client.has_tag.return_value = {
            'name': 'latest', 'start_ts': 1000000000,
        }

        results = resolve_quay_uri(
            'kolla', '*', 'latest', since=None)

        self.assertEqual(results, [
            ('quay.io', 'kolla/nova-api', 'latest'),
        ])


def _make_cli_runner():
    """Create a CliRunner with stderr separated from stdout."""
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


class TestInfoQuayCommand(unittest.TestCase):
    """Tests for the info command with quay:// URIs."""

    @mock.patch('occystrap.main._resolve_quay_images')
    @mock.patch('occystrap.main._build_info')
    @mock.patch('occystrap.main.input_registry.Image')
    def test_info_quay_text(self, mock_image_cls, mock_build_info,
                            mock_resolve):
        """info with quay:// shows multiple images separated by ---."""
        mock_resolve.return_value = [
            ('quay.io', 'kolla/nova-api', 'latest'),
            ('quay.io', 'kolla/keystone', 'latest'),
        ]
        mock_build_info.side_effect = [
            {'image': 'kolla/nova-api', 'tag': 'latest',
             'layer_count': 3},
            {'image': 'kolla/keystone', 'tag': 'latest',
             'layer_count': 2},
        ]

        runner = _make_cli_runner()
        result = runner.invoke(cli, ['info', 'quay://kolla/*:latest'])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('kolla/nova-api', result.output)
        self.assertIn('kolla/keystone', result.output)
        self.assertIn('---', result.output)

    @mock.patch('occystrap.main._resolve_quay_images')
    @mock.patch('occystrap.main._build_info')
    @mock.patch('occystrap.main.input_registry.Image')
    def test_info_quay_json(self, mock_image_cls, mock_build_info,
                            mock_resolve):
        """info -O json with quay:// outputs a JSON array."""
        mock_resolve.return_value = [
            ('quay.io', 'kolla/nova-api', 'latest'),
        ]
        mock_build_info.return_value = {
            'image': 'kolla/nova-api', 'tag': 'latest',
            'layer_count': 3,
        }

        runner = _make_cli_runner()
        result = runner.invoke(
            cli, ['-O', 'json', 'info', 'quay://kolla/*:latest'])

        self.assertEqual(result.exit_code, 0, result.output)
        # Extract JSON from output — progress lines and ANSI
        # codes may be mixed in when Click doesn't support
        # mix_stderr=False. Try stdout first (clean JSON),
        # then fall back to finding the JSON array in mixed
        # output.
        output = getattr(result, 'stdout', None) or result.output
        if not output.lstrip().startswith('['):
            json_start = output.index('\n[') + 1
            output = output[json_start:]
        data = json.loads(output)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['image'], 'kolla/nova-api')

    @mock.patch('occystrap.main._resolve_quay_images')
    def test_info_quay_no_matches(self, mock_resolve):
        """info with quay:// and no matches prints a message."""
        mock_resolve.return_value = []

        runner = _make_cli_runner()
        result = runner.invoke(cli, ['info', 'quay://kolla/*:latest'])

        self.assertEqual(result.exit_code, 0)
        # "No images found" goes to stderr
        stderr = getattr(result, 'stderr', '') or ''
        combined = result.output + stderr
        self.assertIn('No images found', combined)


class TestProcessQuayCommand(unittest.TestCase):
    """Tests for the process command with quay:// URIs."""

    @mock.patch('occystrap.main._resolve_quay_images')
    @mock.patch('occystrap.main._process_single')
    def test_process_quay_basic(self, mock_process_single, mock_resolve):
        """process with quay:// calls _process_single for each image."""
        mock_resolve.return_value = [
            ('quay.io', 'kolla/nova-api', 'latest'),
            ('quay.io', 'kolla/keystone', 'latest'),
        ]

        runner = _make_cli_runner()
        result = runner.invoke(
            cli, ['process', 'quay://kolla/*:latest',
                  'dir:///tmp/out?unique_names=true'])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(mock_process_single.call_count, 2)
        # Verify registry:// URIs were constructed
        first_call_source = mock_process_single.call_args_list[0][0][1]
        self.assertEqual(first_call_source,
                         'registry://quay.io/kolla/nova-api:latest')

    @mock.patch('occystrap.main._resolve_quay_images')
    def test_process_quay_tar_error(self, mock_resolve):
        """process with quay:// and tar:// output produces an error."""
        mock_resolve.return_value = [
            ('quay.io', 'kolla/nova-api', 'latest'),
        ]

        runner = _make_cli_runner()
        result = runner.invoke(
            cli, ['process', 'quay://kolla/*:latest',
                  'tar:///tmp/out.tar'])

        self.assertNotEqual(result.exit_code, 0)
        stderr = getattr(result, 'stderr', '') or ''
        combined = result.output + stderr
        self.assertIn('tar://', combined)

    @mock.patch('occystrap.main._resolve_quay_images')
    @mock.patch('occystrap.main._process_single')
    def test_process_quay_no_matches(self, mock_process_single, mock_resolve):
        """process with quay:// and no matches does not call _process_single."""
        mock_resolve.return_value = []

        runner = _make_cli_runner()
        result = runner.invoke(
            cli, ['process', 'quay://kolla/*:latest',
                  'dir:///tmp/out?unique_names=true'])

        self.assertEqual(result.exit_code, 0)
        mock_process_single.assert_not_called()
