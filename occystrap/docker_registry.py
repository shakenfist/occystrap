# A simple implementation of a docker registry client. Fetches an image to a tarball.
# With a big nod to https://github.com/NotGlop/docker-drag/blob/master/docker_pull.py

import hashlib
import io
import json
import logging
import os
import re
import sys
import tarfile
import tempfile
import zlib

from occystrap import constants
from occystrap import util

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DELETED_FILE_RE = re.compile('.*/\.wh\.(.*)$')


def always_fetch():
    return True


class Image(object):
    def __init__(self, registry, image, tag):
        self.registry = registry
        self.image = image
        self.tag = tag
        self._cached_auth = None

    def request_url(self, method, url, headers=None, data=None, stream=False):
        if not headers:
            headers = {}

        if self._cached_auth:
            headers.update({'Authorization': 'Bearer %s' % self._cached_auth})

        try:
            return util.request_url(method, url, headers=headers, data=data,
                                    stream=stream)
        except util.UnauthorizedException as e:
            auth_re = re.compile('Bearer realm="([^"]*)",service="([^"]*)"')
            m = auth_re.match(e.args[5].get('Www-Authenticate'))
            if m:
                auth_url = ('%s?service=%s&scope=repository:%s:pull'
                            % (m.group(1), m.group(2), self.image))
                r = util.request_url('GET', auth_url)
                token = r.json().get('token')
                headers.update({'Authorization': 'Bearer %s' % token})
                self._cached_auth = token

            return util.request_url(
                method, url, headers=headers, data=data, stream=stream)

    def fetch(self, fetch_callback=always_fetch):
        LOG.info('Fetching manifest')
        r = self.request_url(
            'GET',
            'https://%(registry)s/v2/%(image)s/manifests/%(tag)s'
            % {
                'registry': self.registry,
                'image': self.image,
                'tag': self.tag
            },
            headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
        manifest = r.json()

        LOG.info('Fetching config file')
        r = self.request_url(
            'GET',
            'https://%(registry)s/v2/%(image)s/blobs/%(config)s'
            % {
                'registry': self.registry,
                'image': self.image,
                'config': manifest['config']['digest']
            })
        config = r.content
        h = hashlib.sha256()
        h.update(config)
        if h.hexdigest() != manifest['config']['digest'].split(':')[1]:
            LOG.error('Hash verification failed for image config blob (%s vs %s)'
                      % (manifest['config']['digest'].split(':')[1], h.hexdigest()))
            sys.exit(1)

        config_filename = ('%s.json'
                           % manifest['config']['digest'].split(':')[1])
        yield (constants.CONFIG_FILE, config_filename,
               io.BytesIO(config))

        LOG.info('There are %d image layers' % len(manifest['layers']))
        for layer in manifest['layers']:
            layer_filename = layer['digest'].split(':')[1]
            if not fetch_callback(layer_filename):
                LOG.info('Fetch callback says skip layer %s' % layer['digest'])
                yield(constants.IMAGE_LAYER, layer_filename, None)
                continue

            LOG.info('Fetching layer %s (%d bytes)'
                     % (layer['digest'], layer['size']))
            r = self.request_url(
                'GET',
                'https://%(registry)s/v2/%(image)s/blobs/%(layer)s'
                % {
                    'registry': self.registry,
                    'image': self.image,
                    'layer': layer['digest']
                },
                stream=True)

            # We can use zlib for streaming decompression, but we need to tell it
            # to ignore the gzip header which it doesn't understand. Unfortunately
            # tarfile doesn't do streaming writes (and we need to know the
            # decompressed size before we can write to the tarfile), so we stream
            # to a temporary file on disk.
            try:
                h = hashlib.sha256()
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)

                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    LOG.info('Temporary file for layer is %s' % tf.name)
                    for chunk in r.iter_content(8192):
                        tf.write(d.decompress(chunk))
                        h.update(chunk)

                if h.hexdigest() != layer_filename:
                    LOG.error('Hash verification failed for layer (%s vs %s)'
                              % (name, h.hexdigest()))
                    sys.exit(1)

                with tarfile.open(tf.name) as layer:
                    for mem in layer.getmembers():
                        m = DELETED_FILE_RE.match(mem.name)
                        if m:
                            LOG.info('Layer tarball contains deleted file: %s'
                                     % mem.name)

                with open(tf.name, 'rb') as f:
                    yield(constants.IMAGE_LAYER, layer_filename, f)

            finally:
                os.unlink(tf.name)

        LOG.info('Done')
