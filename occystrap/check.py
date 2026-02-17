"""Image validation checks for the check command.

This module implements the check logic for validating
container images. Checks are organized by category:

- Structural integrity (errors)
- History consistency (errors)
- Compression and compatibility (warnings)
- Filesystem integrity (errors)
- Informational warnings
"""

import hashlib
import os
import tarfile as tarfile_mod

from occystrap import compression
from occystrap import constants
from occystrap.inputs.base import always_fetch
from occystrap.util import format_size
from shakenfist_utilities import logs


LOG = logs.setup_console(__name__)


# Severity levels
CHECK_ERROR = 'error'
CHECK_WARNING = 'warning'
CHECK_INFO = 'info'

# Large layer threshold (1 GB compressed)
LARGE_LAYER_THRESHOLD = 1024 * 1024 * 1024


class CheckResults:
    """Accumulator for check results."""

    def __init__(self):
        self.results = []

    def error(self, check_id, message):
        """Record an error (image is invalid)."""
        self.results.append({
            'severity': CHECK_ERROR,
            'check': check_id,
            'message': message,
        })

    def warning(self, check_id, message):
        """Record a warning (potential issue)."""
        self.results.append({
            'severity': CHECK_WARNING,
            'check': check_id,
            'message': message,
        })

    def info(self, check_id, message):
        """Record an informational message."""
        self.results.append({
            'severity': CHECK_INFO,
            'check': check_id,
            'message': message,
        })

    @property
    def has_errors(self):
        """Return True if any errors were recorded."""
        return any(
            r['severity'] == CHECK_ERROR
            for r in self.results)

    @property
    def error_count(self):
        """Return number of errors."""
        return sum(
            1 for r in self.results
            if r['severity'] == CHECK_ERROR)

    @property
    def warning_count(self):
        """Return number of warnings."""
        return sum(
            1 for r in self.results
            if r['severity'] == CHECK_WARNING)


def check_metadata(manifest, config, results):
    """Run metadata-only checks (--fast mode).

    These checks use only the manifest and config blob,
    without downloading layer data.

    Args:
        manifest: Distribution manifest dict, or None.
        config: OCI image config dict, or None.
        results: CheckResults instance.
    """
    if manifest is None and config is None:
        results.warning(
            'metadata',
            'No manifest or config available; '
            'metadata checks skipped')
        return

    # --- Structural integrity ---

    if manifest:
        schema = manifest.get('schemaVersion')
        if schema != 2:
            results.error(
                'schema-version',
                'Manifest schemaVersion is %s, '
                'expected 2' % schema)
        else:
            results.info(
                'schema-version',
                'Manifest schemaVersion is 2')

    if config:
        rootfs = config.get('rootfs', {})
        rootfs_type = rootfs.get('type')
        if rootfs_type != 'layers':
            results.error(
                'rootfs-type',
                'Config rootfs.type is %r, '
                'expected "layers"' % rootfs_type)
        else:
            results.info(
                'rootfs-type',
                'Config rootfs.type is "layers"')

    # Layer count vs diff_id count
    if manifest and config:
        manifest_layers = manifest.get('layers', [])
        diff_ids = config.get('rootfs', {}).get(
            'diff_ids', [])
        if len(manifest_layers) != len(diff_ids):
            results.error(
                'layer-count',
                'Manifest has %d layers but config '
                'has %d diff_ids'
                % (len(manifest_layers),
                   len(diff_ids)))
        else:
            results.info(
                'layer-count',
                'Layer count matches: %d layers, '
                '%d diff_ids'
                % (len(manifest_layers),
                   len(diff_ids)))

    # Config descriptor
    if manifest:
        config_desc = manifest.get('config', {})
        config_digest = config_desc.get('digest')
        config_size = config_desc.get('size')
        if config_digest:
            results.info(
                'config-descriptor',
                'Config descriptor present '
                '(digest: %s, size: %s)'
                % (config_digest,
                   config_size if config_size
                   is not None else 'N/A'))
        else:
            results.error(
                'config-descriptor',
                'Manifest has no config descriptor')

    # --- History consistency ---

    if config:
        history = config.get('history', [])
        non_empty_count = sum(
            1 for h in history
            if not h.get('empty_layer'))

        if manifest:
            layer_count = len(
                manifest.get('layers', []))
        else:
            layer_count = len(
                config.get('rootfs', {}).get(
                    'diff_ids', []))

        if history:
            if non_empty_count != layer_count:
                results.error(
                    'history-count',
                    'History has %d non-empty entries '
                    'but image has %d layers'
                    % (non_empty_count, layer_count))
            else:
                results.info(
                    'history-count',
                    'History entries match: %d '
                    'non-empty entries for %d layers'
                    % (non_empty_count, layer_count))
        else:
            # Docker daemon inputs have empty history;
            # not an error
            results.info(
                'history-count',
                'No history entries present')

    # --- Compression and compatibility ---

    if manifest:
        layers = manifest.get('layers', [])

        # zstd compatibility
        zstd_layers = [
            ly for ly in layers
            if compression
            .detect_compression_from_media_type(
                ly.get('mediaType'))
            == constants.COMPRESSION_ZSTD]
        if zstd_layers:
            results.warning(
                'zstd-compat',
                '%d layer(s) use zstd compression; '
                'Docker Engine < 20.10 and '
                'containerd < 1.5 cannot pull this '
                'image' % len(zstd_layers))

        # OCI media types
        manifest_media = manifest.get(
            'mediaType', '')
        if manifest_media in (
                constants.MEDIA_TYPE_OCI_MANIFEST,
                constants.MEDIA_TYPE_OCI_INDEX):
            results.info(
                'oci-media-type',
                'Image uses OCI media types; older '
                'Docker versions may not handle '
                'this correctly')

        # Docker v2 media types
        if (manifest_media
                == constants.MEDIA_TYPE_DOCKER_MANIFEST_V2):
            results.info(
                'docker-media-type',
                'Image uses Docker v2 media types; '
                'some OCI-only tooling may not '
                'handle this')

        # Large layers
        for i, layer in enumerate(layers):
            size = layer.get('size', 0)
            if size > LARGE_LAYER_THRESHOLD:
                results.warning(
                    'large-layer',
                    'Layer %d is very large: %s '
                    'compressed'
                    % (i, format_size(size)))

    # ArgsEscaped deprecation
    if config:
        img_config = config.get('config') or {}
        if img_config.get('ArgsEscaped'):
            results.warning(
                'args-escaped',
                'config.ArgsEscaped is set '
                '(Docker-specific, deprecated '
                'in OCI spec)')


def check_layers(input_source, manifest, config,
                 results):
    """Run full checks including layer download.

    Downloads and decompresses every layer to verify
    diff_ids and scan tar entries.

    Args:
        input_source: ImageInput instance.
        manifest: Distribution manifest dict, or None.
        config: OCI image config dict, or None.
        results: CheckResults instance.
    """
    diff_ids = []
    if config:
        diff_ids = config.get('rootfs', {}).get(
            'diff_ids', [])

    seen_files = {}  # path -> list of layer indices
    layer_idx = 0

    for element in input_source.fetch(
            fetch_callback=always_fetch):
        if element.element_type == constants.CONFIG_FILE:
            # Config element - verify digest if manifest
            if manifest and element.data:
                config_data = element.data.read()
                h = hashlib.sha256()
                h.update(config_data)
                actual = 'sha256:%s' % h.hexdigest()
                expected = manifest.get(
                    'config', {}).get('digest')

                # Config digest verification
                if expected and actual != expected:
                    results.error(
                        'config-digest',
                        'Config digest mismatch: '
                        'expected %s, got %s'
                        % (expected, actual))
                elif expected:
                    results.info(
                        'config-digest',
                        'Config digest verified: %s'
                        % actual)

                # Config size verification
                config_size = manifest.get(
                    'config', {}).get('size')
                if config_size is not None:
                    if len(config_data) != config_size:
                        results.error(
                            'config-size',
                            'Config size mismatch: '
                            'expected %d, got %d'
                            % (config_size,
                               len(config_data)))
                    else:
                        results.info(
                            'config-size',
                            'Config size verified: '
                            '%d bytes' % config_size)
            continue

        if (element.element_type
                != constants.IMAGE_LAYER):
            continue

        if element.data is None:
            # Layer was skipped
            layer_idx += 1
            continue

        # Diff_id verification: hash the decompressed
        # layer in chunks to keep memory usage low.
        # Layer data must be seekable (BytesIO or file)
        # since we read it twice: once for hashing and
        # once for tar entry scanning.
        h = hashlib.sha256()
        while True:
            chunk = element.data.read(65536)
            if not chunk:
                break
            h.update(chunk)
        actual_diffid = 'sha256:%s' % h.hexdigest()

        if layer_idx < len(diff_ids):
            expected_diffid = diff_ids[layer_idx]
            if actual_diffid != expected_diffid:
                results.error(
                    'diff-id',
                    'Layer %d diff_id mismatch: '
                    'expected %s, got %s'
                    % (layer_idx,
                       _truncate_digest(
                           expected_diffid),
                       _truncate_digest(
                           actual_diffid)))
            else:
                results.info(
                    'diff-id',
                    'Layer %d diff_id verified'
                    % layer_idx)

        # Scan tar entries for filesystem checks.
        try:
            element.data.seek(0)
        except (AttributeError, OSError) as e:
            results.error(
                'layer-seekable',
                'Layer %d data is not seekable: %s'
                % (layer_idx, e))
            layer_idx += 1
            continue
        try:
            with tarfile_mod.open(
                    fileobj=element.data) as tf:
                for entry in tf:
                    name = entry.name
                    basename = os.path.basename(name)

                    # Whiteout validation
                    if basename.startswith('.wh.'):
                        _check_whiteout(
                            name, basename, entry,
                            layer_idx, results)

                    # Tar entry validation
                    _check_tar_entry(
                        entry, layer_idx, results)

                    # Track files for duplicate
                    # detection
                    if entry.isreg():
                        if name not in seen_files:
                            seen_files[name] = []
                        seen_files[name].append(
                            layer_idx)
        except tarfile_mod.TarError as e:
            results.error(
                'tar-format',
                'Layer %d is not a valid tar: %s'
                % (layer_idx, e))

        layer_idx += 1

    # Report duplicate files across layers.
    # This is informational since multi-layer images
    # commonly overwrite files in later layers.
    dupes_reported = 0
    for path, layers in seen_files.items():
        if len(layers) > 1:
            dupes_reported += 1
            if dupes_reported <= 10:
                results.info(
                    'duplicate-file',
                    'File %r appears in layers %s'
                    % (path, layers))
    if dupes_reported > 10:
        results.info(
            'duplicate-file',
            '... and %d more duplicate files'
            % (dupes_reported - 10))


def _check_whiteout(name, basename, entry,
                    layer_idx, results):
    """Check whiteout file validity.

    Called only when basename starts with '.wh.'.
    """
    if basename == '.wh..wh..opq':
        # Opaque whiteout - valid
        return

    # Deletion whiteout - should have a target
    target = basename[4:]
    if not target:
        results.error(
            'whiteout',
            'Layer %d: empty whiteout target '
            'at %s' % (layer_idx, name))

    # OCI spec: whiteout files should be empty
    if entry.isreg() and entry.size > 0:
        results.warning(
            'whiteout-size',
            'Layer %d: whiteout %s has non-zero '
            'size (%d bytes)'
            % (layer_idx, name, entry.size))


def _check_tar_entry(entry, layer_idx, results):
    """Check tar entry header validity."""
    # Negative timestamps
    if entry.mtime < 0:
        results.error(
            'tar-timestamp',
            'Layer %d: negative timestamp '
            'on %s (mtime=%d)'
            % (layer_idx, entry.name,
               entry.mtime))

    # setuid/setgid + world-writable
    mode = entry.mode
    if mode is not None:
        if (mode & 0o6000) and (mode & 0o002):
            results.warning(
                'tar-permissions',
                'Layer %d: %s has setuid/setgid '
                'with world-writable '
                '(mode=%o)'
                % (layer_idx, entry.name, mode))


def _truncate_digest(digest):
    """Truncate a digest for display."""
    if len(digest) > 25:
        return digest[:25] + '...'
    return digest
