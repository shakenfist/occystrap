# Fetch images from the local Docker or Podman daemon via the
# Docker Engine API. This communicates over a Unix domain socket
# (default: /var/run/docker.sock).
#
# Docker Engine API documentation:
# https://docs.docker.com/engine/api/
#
# Podman compatibility:
# Podman provides a Docker-compatible API via podman.socket.
# Use the --socket option to point to the Podman socket:
# - Rootful: /run/podman/podman.sock
# - Rootless: /run/user/<uid>/podman/podman.sock
# See: https://docs.podman.io/en/latest/markdown/
#      podman-system-service.1.html
#
# The API returns images in the same format as 'docker save',
# which is the same format that inputs/tarfile.py reads.
#
# API Limitation: Unlike the registry API (inputs/registry.py)
# which can fetch individual layer blobs via GET
# /v2/<name>/blobs/<digest>, the Docker Engine API only
# provides /images/{name}/get which returns a complete tarball.
# There is no endpoint to fetch individual image components
# (config, layers) separately. This is a fundamental limitation
# of the Docker Engine API.
# See: https://github.com/moby/moby/issues/24851
#
# Pre-Computed Manifest:
# Both Docker and Podman place manifest.json near the END of
# the tarball stream (Docker due to lexical filepath.WalkDir
# ordering, Podman explicitly in Close()). This means we must
# buffer all data until the manifest arrives to know the config
# and layer identities. To avoid this, we call the inspect API
# (GET /images/{name}/json) BEFORE streaming and extract:
#   - Image ID (SHA256 of config JSON) -> config filename
#   - RootFS.Layers (DiffIDs) -> layer paths for Docker 25+
#
# For Docker 25+ (OCI format), DiffIDs directly correspond to
# blob paths (blobs/sha256/<diffid>), so we can pre-compute
# the full manifest and process entries immediately.
#
# For Docker 1.10-24.x (legacy format), we can identify the
# config file early but layer directory names use v1-compat IDs
# that we cannot predict. Layers are still ordered by
# manifest.json when it arrives.
#
# Hybrid Streaming:
# We use a hybrid approach to minimize disk usage:
# - Stream the tarball sequentially (mode='r|')
# - With pre-computed manifest (OCI): process layers
#   immediately as they arrive in the stream
# - Without manifest (legacy): yield config early, buffer
#   layers until manifest.json arrives
# - In-order layers are yielded directly; out-of-order
#   layers are buffered to temp files for later
#
# For Docker 25+ with OCI format, this eliminates the need to
# buffer data while waiting for manifest.json. For legacy
# format, it still yields the config early and falls back to
# manifest.json for layer ordering.

import io
import json
import logging
import os
import shutil
import tarfile
import tempfile

import requests_unixsocket

from occystrap import constants
from occystrap.inputs.base import ImageInput

COPY_BUFSIZE = 1024 * 1024  # 1MB chunks for streaming copies


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

DEFAULT_SOCKET_PATH = '/var/run/docker.sock'


def always_fetch(digest):
    return True


class Image(ImageInput):
    def __init__(self, image, tag='latest',
                 socket_path=DEFAULT_SOCKET_PATH,
                 temp_dir=None):
        self._image = image
        self._tag = tag
        self.socket_path = socket_path
        self.temp_dir = temp_dir
        self._session = None

    @property
    def image(self):
        """Return the image name."""
        return self._image

    @property
    def tag(self):
        """Return the image tag."""
        return self._tag

    def _get_session(self):
        if self._session is None:
            self._session = requests_unixsocket.Session()
        return self._session

    def _socket_url(self, path):
        # requests_unixsocket uses http+unix:// with
        # URL-encoded socket path
        encoded_socket = self.socket_path.replace(
            '/', '%2F')
        return 'http+unix://%s%s' % (encoded_socket, path)

    def _request(self, method, path, stream=False):
        session = self._get_session()
        url = self._socket_url(path)
        LOG.debug(
            'Docker API request: %s %s' % (method, path))
        r = session.request(method, url, stream=stream)
        if r.status_code == 404:
            raise Exception(
                'Image not found: %s:%s'
                % (self.image, self.tag))
        if r.status_code != 200:
            raise Exception(
                'Docker API error %d: %s'
                % (r.status_code, r.text))
        return r

    def _get_image_reference(self):
        return '%s:%s' % (self.image, self.tag)

    def inspect(self):
        """Get image metadata from the Docker daemon."""
        ref = self._get_image_reference()
        r = self._request(
            'GET', '/images/%s/json' % ref)
        return r.json()

    def _extract_inspect_ids(self, inspect_data):
        """Extract config hash and DiffIDs from inspect.

        The Docker Engine inspect API (GET /images/{name}/json)
        returns the image ID (SHA256 of the config JSON) and
        RootFS.Layers (the DiffIDs of each layer). These are
        used to pre-compute the tarball manifest for Docker 25+
        (OCI format) or identify the config file early for
        older formats.

        Args:
            inspect_data: JSON response from inspect API.

        Returns:
            Tuple of (config_hex, diff_id_hexes) where:
            - config_hex: hex SHA256 of image config, or
              None if unavailable.
            - diff_id_hexes: list of hex DiffIDs for each
              layer, or None if unavailable.
        """
        image_id = inspect_data.get('Id', '')
        if not image_id.startswith('sha256:'):
            return None, None

        config_hex = image_id[7:]

        rootfs = inspect_data.get('RootFS', {})
        layers = rootfs.get('Layers', [])
        diff_ids = []
        for d in layers:
            if d.startswith('sha256:'):
                diff_ids.append(d[7:])
            else:
                diff_ids.append(d)

        return config_hex, diff_ids if diff_ids else None

    def _digest_from_path(self, layer_path):
        """Extract layer digest from a tarball entry path.

        Handles both Docker tarball formats:
        - Legacy (1.10-24.x): <digest>/layer.tar -> digest
        - OCI (25+): blobs/sha256/<digest> -> digest
        """
        if layer_path.startswith('blobs/'):
            return os.path.basename(layer_path)
        return os.path.dirname(layer_path)

    def _buffer_to_tempfile(self, fileobj, name):
        """Buffer file content to a temporary file.

        Copies data in chunks to avoid loading entire layers
        into RAM. Returns the path to the temp file.
        """
        tf = tempfile.NamedTemporaryFile(
            delete=False, dir=self.temp_dir)
        shutil.copyfileobj(fileobj, tf, length=COPY_BUFSIZE)
        tf.close()
        size = os.path.getsize(tf.name)
        LOG.info('Buffered %s to %s (%d bytes)'
                 % (name, tf.name, size))
        return tf.name

    def _open_buffered(self, buffered, filename):
        """Open a buffered temp file for reading.

        Returns (file_handle, path). Caller must close the
        handle and delete the file when done.
        """
        path = buffered.pop(filename)
        fh = open(path, 'rb')
        return fh, path

    def _cleanup_file(self, fh, path):
        """Close a file handle and delete its backing file."""
        fh.close()
        try:
            os.unlink(path)
        except OSError:
            pass

    def fetch(self, fetch_callback=always_fetch):
        """Fetch image layers from the local Docker daemon.

        Uses the inspect API to pre-compute the manifest for
        Docker 25+ (OCI format), eliminating the need to wait
        for manifest.json. For older formats (1.10-24.x), the
        config file is identified early from inspect data.

        Uses hybrid streaming: processes layers directly when
        they arrive in expected order, buffers out-of-order
        layers to temp files. This minimizes disk usage.
        """
        ref = self._get_image_reference()
        LOG.info('Fetching image %s from Docker daemon at %s'
                 % (ref, self.socket_path))

        # Pre-compute manifest data from inspect API to
        # avoid blocking on manifest.json (which arrives
        # last in the tarball stream).
        inspect_data = self.inspect()
        config_hex, diff_ids = \
            self._extract_inspect_ids(inspect_data)
        if config_hex and diff_ids:
            LOG.info(
                'Pre-computed from inspect:'
                ' config=%s..., %d layers'
                % (config_hex[:12], len(diff_ids)))
        elif config_hex:
            LOG.info(
                'Config hash from inspect: %s...'
                % config_hex[:12])

        # Stream the image tarball from Docker
        LOG.info(
            'Requesting image tarball from'
            ' Docker daemon...')
        r = self._request(
            'GET', '/images/%s/get' % ref, stream=True)
        LOG.info('Docker API responded, stream ready')

        # State tracking
        manifest = None
        config_filename = None
        expected_layers = None
        format_detected = False
        next_layer_idx = 0
        config_yielded = False

        # Stats for summary
        layers_streamed = 0
        layers_buffered = 0

        # Buffered files: filename -> temp file path
        buffered = {}

        def _detect_format(name):
            """Detect tarball format from entry name.

            Sets manifest, config_filename, and
            expected_layers for OCI format. Sets just
            config_filename for legacy format.
            """
            nonlocal format_detected, manifest
            nonlocal config_filename, expected_layers

            if format_detected:
                return
            format_detected = True

            if name.startswith('blobs/') \
                    and config_hex and diff_ids:
                # OCI format (Docker 25+): pre-compute
                # full manifest from inspect data
                config_filename = \
                    'blobs/sha256/%s' % config_hex
                expected_layers = [
                    'blobs/sha256/%s' % d
                    for d in diff_ids
                ]
                manifest = [{
                    'Config': config_filename,
                    'Layers': expected_layers
                }]
                LOG.info(
                    'OCI format detected, pre-computed'
                    ' manifest: config=%s, %d layers'
                    % (config_filename,
                       len(expected_layers)))
            elif config_hex:
                # Legacy format (1.10-24.x): we know
                # the config filename but not layer paths
                config_filename = \
                    '%s.json' % config_hex
                LOG.info(
                    'Legacy format detected,'
                    ' config=%s' % config_filename)

        def _layer_progress():
            """Return a progress string like '[3/10]'."""
            if expected_layers:
                return '[%d/%d]' % (
                    next_layer_idx + 1,
                    len(expected_layers))
            return ''

        def _yield_layer(layer_path, from_buffer=False):
            """Yield a layer from a buffered temp file."""
            layer_digest = self._digest_from_path(
                layer_path)
            fh, path = self._open_buffered(
                buffered, layer_path)
            size = os.path.getsize(path)
            source = 'buffer' if from_buffer else 'temp'
            LOG.info(
                '%s Yielding layer %s from %s'
                ' (%d bytes)'
                % (_layer_progress(), layer_digest,
                   source, size))
            try:
                yield (constants.IMAGE_LAYER,
                       layer_digest, fh)
            finally:
                self._cleanup_file(fh, path)

        def flush_ready_layers():
            """Yield buffered layers that are next."""
            nonlocal next_layer_idx, layers_buffered
            while (expected_layers
                   and next_layer_idx
                   < len(expected_layers)
                   and expected_layers[next_layer_idx]
                   in buffered):
                layer_path = \
                    expected_layers[next_layer_idx]
                layer_digest = self._digest_from_path(
                    layer_path)

                if not fetch_callback(layer_digest):
                    LOG.info(
                        '%s Skipping layer %s'
                        ' (fetch callback)'
                        % (_layer_progress(),
                           layer_digest))
                    yield (constants.IMAGE_LAYER,
                           layer_digest, None)
                else:
                    for elem in _yield_layer(
                            layer_path,
                            from_buffer=True):
                        yield elem
                    layers_buffered += 1
                next_layer_idx += 1

        try:
            LOG.info(
                'Opening tarball stream (this may take'
                ' a while for large images)...')
            tar = tarfile.open(
                fileobj=r.raw, mode='r|')
            LOG.info('Tarball stream opened,'
                     ' reading entries...')

            for member in tar:
                f = tar.extractfile(member)
                if f is None:
                    _detect_format(member.name)
                    LOG.info(
                        'Skipping directory entry: %s'
                        % member.name)
                    continue

                _detect_format(member.name)

                # --- Handle manifest.json ---
                if member.name == 'manifest.json':
                    if manifest is not None:
                        # OCI: we already pre-computed
                        # the manifest. Verify it.
                        real = json.loads(
                            f.read().decode('utf-8'))
                        real_layers = \
                            real[0].get('Layers', [])
                        if real_layers \
                                != expected_layers:
                            LOG.warning(
                                'Pre-computed manifest'
                                ' differs from actual!'
                                ' Falling back to'
                                ' actual manifest.')
                            config_filename = \
                                real[0]['Config']
                            expected_layers = \
                                real[0]['Layers']
                            manifest = real
                        else:
                            LOG.info(
                                'Pre-computed manifest'
                                ' verified against'
                                ' actual')
                        continue

                    # Legacy: read manifest normally
                    manifest = json.loads(
                        f.read().decode('utf-8'))
                    real_config = manifest[0]['Config']
                    expected_layers = \
                        manifest[0]['Layers']

                    # Correct config filename if
                    # pre-computed was wrong
                    if config_filename \
                            and real_config \
                            != config_filename:
                        LOG.info(
                            'Config filename corrected'
                            ': %s -> %s'
                            % (config_filename,
                               real_config))
                    config_filename = real_config

                    LOG.info(
                        'Found manifest: config=%s,'
                        ' %d layers'
                        % (config_filename,
                           len(expected_layers)))
                    if buffered:
                        LOG.info(
                            '%d file(s) were buffered'
                            ' before manifest'
                            % len(buffered))

                    # Yield config from buffer if it
                    # was buffered before manifest
                    if not config_yielded \
                            and config_filename \
                            in buffered:
                        LOG.info(
                            'Config was buffered'
                            ' before manifest,'
                            ' yielding from buffer')
                        fh, path = \
                            self._open_buffered(
                                buffered,
                                config_filename)
                        config_data = fh.read()
                        self._cleanup_file(fh, path)
                        yield (
                            constants.CONFIG_FILE,
                            config_filename,
                            io.BytesIO(config_data))
                        config_yielded = True

                        for elem in \
                                flush_ready_layers():
                            yield elem
                    continue

                # --- Before manifest: smart handling ---
                if manifest is None:
                    # Yield config early if we can
                    # identify it from inspect data
                    if config_filename \
                            and member.name \
                            == config_filename \
                            and not config_yielded:
                        config_data = f.read()
                        LOG.info(
                            'Config identified early'
                            ' from inspect: %s'
                            ' (%d bytes)'
                            % (config_filename,
                               len(config_data)))
                        yield (
                            constants.CONFIG_FILE,
                            config_filename,
                            io.BytesIO(config_data))
                        config_yielded = True
                        continue

                    # Buffer everything else until
                    # manifest arrives
                    LOG.info(
                        'Manifest not yet seen,'
                        ' buffering %s'
                        % member.name)
                    buffered[member.name] = \
                        self._buffer_to_tempfile(
                            f, member.name)
                    continue

                # --- Handle config file ---
                if member.name == config_filename \
                        and not config_yielded:
                    config_data = f.read()
                    LOG.info(
                        'Found config file %s'
                        ' (%d bytes)'
                        % (config_filename,
                           len(config_data)))
                    yield (constants.CONFIG_FILE,
                           config_filename,
                           io.BytesIO(config_data))
                    config_yielded = True

                    for elem in flush_ready_layers():
                        yield elem
                    continue

                # --- Next expected layer ---
                if (config_yielded
                        and next_layer_idx
                        < len(expected_layers)
                        and member.name
                        == expected_layers[
                            next_layer_idx]):
                    layer_path = member.name
                    layer_digest = \
                        self._digest_from_path(
                            layer_path)

                    if not fetch_callback(
                            layer_digest):
                        LOG.info(
                            '%s Skipping layer %s'
                            ' (fetch callback)'
                            % (_layer_progress(),
                               layer_digest))
                        yield (constants.IMAGE_LAYER,
                               layer_digest, None)
                    else:
                        # Buffer to temp file using
                        # chunked I/O, then yield
                        # seekable file handle
                        temp_path = \
                            self._buffer_to_tempfile(
                                f, member.name)
                        fh = open(temp_path, 'rb')
                        size = os.path.getsize(
                            temp_path)
                        LOG.info(
                            '%s Streaming layer %s'
                            ' via temp (%d bytes)'
                            % (_layer_progress(),
                               layer_digest, size))
                        try:
                            yield (
                                constants.IMAGE_LAYER,
                                layer_digest, fh)
                        finally:
                            self._cleanup_file(
                                fh, temp_path)
                        layers_streamed += 1
                    next_layer_idx += 1

                    for elem in flush_ready_layers():
                        yield elem
                    continue

                # --- Out-of-order or unknown file ---
                if member.isfile():
                    LOG.info(
                        'Out-of-order file %s,'
                        ' buffering to temp'
                        % member.name)
                    buffered[member.name] = \
                        self._buffer_to_tempfile(
                            f, member.name)

            tar.close()
            LOG.info('Tarball stream complete')

            # Yield any remaining buffered items
            if not config_yielded \
                    and config_filename \
                    and config_filename in buffered:
                LOG.info(
                    'Yielding config from buffer'
                    ' (arrived after layers)')
                fh, path = self._open_buffered(
                    buffered, config_filename)
                config_data = fh.read()
                self._cleanup_file(fh, path)
                yield (constants.CONFIG_FILE,
                       config_filename,
                       io.BytesIO(config_data))
                config_yielded = True

            # Yield remaining buffered layers in order
            if expected_layers \
                    and next_layer_idx \
                    < len(expected_layers):
                remaining = (
                    len(expected_layers)
                    - next_layer_idx)
                LOG.info(
                    '%d layer(s) remaining in buffer,'
                    ' yielding in order' % remaining)

            while expected_layers \
                    and next_layer_idx \
                    < len(expected_layers):
                layer_path = \
                    expected_layers[next_layer_idx]
                layer_digest = \
                    self._digest_from_path(layer_path)

                if layer_path not in buffered:
                    raise Exception(
                        'Layer %s not found in'
                        ' tarball' % layer_path)

                if not fetch_callback(layer_digest):
                    LOG.info(
                        '%s Skipping layer %s'
                        ' (fetch callback)'
                        % (_layer_progress(),
                           layer_digest))
                    yield (constants.IMAGE_LAYER,
                           layer_digest, None)
                    buffered.pop(layer_path)
                else:
                    for elem in _yield_layer(
                            layer_path,
                            from_buffer=True):
                        yield elem
                    layers_buffered += 1
                next_layer_idx += 1

        finally:
            # Clean up any remaining buffered files
            for path in buffered.values():
                if os.path.exists(path):
                    os.unlink(path)

        total = layers_streamed + layers_buffered
        LOG.info(
            'Done: %d layer(s) streamed directly,'
            ' %d from buffer (%d total)'
            % (layers_streamed, layers_buffered, total))
