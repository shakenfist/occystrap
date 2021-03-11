# A simple implementation of a docker registry client. Fetches an image to a tarball.
# With a big nod to https://github.com/NotGlop/docker-drag/blob/master/docker_pull.py

import gzip
import hashlib
import io
import json
import logging
from pbr.version import VersionInfo
import re
import requests
import sys
import tarfile


logging.basicConfig(level=logging.INFO)

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


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
    except:
        version = '0.0.0'
    return 'Mozilla/5.0 (Ubuntu; Linux x86_64) Occy Strap/%s' % version


def actual_request_url(method, url, headers=None, data=None):
    if not headers:
        headers = {}
    headers.update({'User-Agent': get_user_agent()})
    if data:
        headers['Content-Type'] = 'application/json'
    r = requests.request(method, url,
                         data=json.dumps(data),
                         headers=headers)

    LOG.debug('-------------------------------------------------------')
    LOG.debug('API client requested: %s %s' % (method, url))
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
    if r.text:
        try:
            LOG.debug('Data:\n    %s'
                      % ('\n    '.join(json.dumps(json.loads(r.text),
                                                  indent=4,
                                                  sort_keys=True).split('\n'))))
        except Exception:
            LOG.debug('Text:\n    %s'
                      % ('\n    '.join(r.text.split('\n'))))
    LOG.debug('-------------------------------------------------------')

    if r.status_code in STATUS_CODES_TO_ERRORS:
        raise STATUS_CODES_TO_ERRORS[r.status_code](
            'API request failed', method, url, r.status_code, r.text, r.headers)

    if r.status_code != 200:
        raise APIException(
            'API request failed', method, url, r.status_code, r.text, r.headers)
    return r


CACHED_AUTH = None


def request_url(method, url, headers=None, data=None):
    global CACHED_AUTH

    if not headers:
        headers = {}

    if CACHED_AUTH:
        headers.update({'Authorization': 'Bearer %s' % CACHED_AUTH})

    try:
        return actual_request_url(method, url, headers=headers, data=data)
    except UnauthorizedException as e:
        auth_re = re.compile('Bearer realm="([^"]*)",service="([^"]*)"')
        m = auth_re.match(e.args[5].get('Www-Authenticate'))
        if m:
            auth_url = ('%s?service=%s&scope=repository:library/busybox:pull'
                        % (m.group(1), m.group(2)))
            r = actual_request_url('GET', auth_url)
            token = r.json().get('token')
            headers.update({'Authorization': 'Bearer %s' % token})
            CACHED_AUTH = token

        return actual_request_url(
            method, url, headers=headers, data=data)


DELETED_FILE_RE = re.compile('.*/\.wh\.(.*)$')
image_path = sys.argv[1]

LOG.info('Fetching manifest')
r = request_url(
    'GET', 'https://registry-1.docker.io/v2/library/busybox/manifests/latest',
    headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
manifest = r.json()
LOG.info('Manifest says: %s' % manifest)

LOG.info('Fetching config file')
r = request_url(
    'GET', ('https://registry-1.docker.io/v2/library/busybox/blobs/%s'
            % manifest['config']['digest']))
config = r.content
h = hashlib.sha256()
h.update(config)
if h.hexdigest() != manifest['config']['digest'].split(':')[1]:
    LOG.error('Hash verification failed for image config blob (%s vs %s)'
              % (manifest['config']['digest'].split(':')[1], h.hexdigest()))
    sys.exit(1)

tar_manifest = [{
    'Layers': [],
    'RepoTags': ['busybox:latest']
}]

with tarfile.open(image_path, 'w') as image:
    LOG.info('Writing config file to tarball')
    config_filename = manifest['config']['digest'].split(':')[1]
    ti = tarfile.TarInfo('%s.json' % config_filename)
    ti.size = len(config)
    image.addfile(ti, io.BytesIO(config))
    tar_manifest[0]['Config'] = config_filename

    for layer in manifest['layers']:
        LOG.info('Fetching layer %s (%d bytes)'
                 % (layer['digest'], layer['size']))
        r = request_url(
            'GET', ('https://registry-1.docker.io/v2/library/busybox/blobs/%s'
                    % layer['digest']))

        LOG.info('Writing layer to tarball')
        layer_filename = layer['digest'].split(':')[1]
        compressed_layer = r.content
        expanded_layer = gzip.GzipFile(
            fileobj=io.BytesIO(compressed_layer), mode='rb').read()
        h = hashlib.sha256()
        h.update(compressed_layer)
        if h.hexdigest() != layer_filename:
            LOG.error('Hash verification failed for layer (%s vs %s)'
                      % (layer_filename, h.hexdigest()))
            sys.exit(1)

        ti = tarfile.TarInfo('%s/layer.tar' % layer_filename)
        ti.size = len(expanded_layer)
        image.addfile(ti, io.BytesIO(expanded_layer))
        tar_manifest[0]['Layers'].append(layer_filename)

        with tarfile.open(layer_filename, fileobj=io.BytesIO(expanded_layer)) as layer:
            for mem in layer.getmembers():
                m = DELETED_FILE_RE.match(mem.name)
                if m:
                    LOG.info('Layer tarball contains deleted file: %s'
                             % mem.name)

    LOG.info('Writing manifest file to tarball')
    encoded_manifest = json.dumps(tar_manifest).encode('utf-8')
    ti = tarfile.TarInfo('manifest.json')
    ti.size = len(encoded_manifest)
    image.addfile(ti, io.BytesIO(encoded_manifest))

LOG.info('Done')
