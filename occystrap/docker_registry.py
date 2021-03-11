# A simple implementation of a docker registry client. Fetches an image to a tarball.
# With a big nod to https://github.com/NotGlop/docker-drag/blob/master/docker_pull.py

import gzip
import hashlib
import io
import json
import logging
import re
import sys
import tarfile

from occystrap import util

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


DELETED_FILE_RE = re.compile('.*/\.wh\.(.*)$')


class Image(object):
    def __init__(self, registry, image, tag):
        self.registry = registry
        self.image = image
        self.tag = tag
        self._cached_auth = None

    def request_url(self, method, url, headers=None, data=None):
        if not headers:
            headers = {}

        if self._cached_auth:
            headers.update({'Authorization': 'Bearer %s' % self._cached_auth})

        try:
            return util.request_url(method, url, headers=headers, data=data)
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
                method, url, headers=headers, data=data)

    def fetch(self, image_path):
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
        LOG.info('Manifest says: %s' % manifest)

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

        tar_manifest = [{
            'Layers': [],
            'RepoTags': ['busybox:latest']
        }]

        with tarfile.open(image_path, 'w') as image_tar:
            LOG.info('Writing config file to tarball')
            config_filename = manifest['config']['digest'].split(':')[1]
            ti = tarfile.TarInfo('%s.json' % config_filename)
            ti.size = len(config)
            image_tar.addfile(ti, io.BytesIO(config))
            tar_manifest[0]['Config'] = config_filename

            LOG.info('There are %d image layers' % len(manifest['layers']))
            for layer in manifest['layers']:
                LOG.info('Fetching layer %s (%d bytes)'
                         % (layer['digest'], layer['size']))
                r = self.request_url(
                    'GET',
                    'https://%(registry)s/v2/%(image)s/blobs/%(layer)s'
                    % {
                        'registry': self.registry,
                        'image': self.image,
                        'layer': layer['digest']
                    })

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
                image_tar.addfile(ti, io.BytesIO(expanded_layer))
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
            image_tar.addfile(ti, io.BytesIO(encoded_manifest))

        LOG.info('Done')
