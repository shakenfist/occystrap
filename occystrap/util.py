import json
import logging
from oslo_concurrency import processutils
from pbr.version import VersionInfo
import requests


LOG = logging.getLogger(__name__)


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


def request_url(method, url, headers=None, data=None, stream=False):
    if not headers:
        headers = {}
    headers.update({'User-Agent': get_user_agent()})
    if data:
        headers['Content-Type'] = 'application/json'
    r = requests.request(method, url,
                         data=json.dumps(data),
                         headers=headers,
                         stream=stream)

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
                          % ('\n    '.join(json.dumps(json.loads(r.text),
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
            'API request failed', method, url, r.status_code, r.text, r.headers)

    if r.status_code != 200:
        raise APIException(
            'API request failed', method, url, r.status_code, r.text, r.headers)
    return r


def execute(command, check_exit_code=[0], env_variables=None,
            cwd=None):
    return processutils.execute(
        command, check_exit_code=check_exit_code,
        env_variables=env_variables, shell=True, cwd=cwd)
