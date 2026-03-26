import io
import json
import os
import tarfile

from occystrap import constants
from occystrap.outputs.base import ImageOutput
from shakenfist_utilities import logs


LOG = logs.setup_console(__name__)


# This code creates v1.2 format image tarballs.
# v1.2 is documented at https://github.com/moby/docker-image-spec/blob/v1.2.0/v1.2.md
# v2 is documented at https://github.com/opencontainers/image-spec/blob/main/

class TarWriter(ImageOutput):
    """Creates docker-loadable tarballs in v1.2 format.

    To normalize timestamps for reproducible builds, use
    the TimestampNormalizer filter before this output.

    Uses USTAR format for the outer tarball which
    contains only short paths (SHA256 hashes and small
    filenames), avoiding PAX extended headers.
    """

    def __init__(self, image, tag, image_path):
        super().__init__()

        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.image_tar = tarfile.open(
            image_path, 'w', format=tarfile.USTAR_FORMAT)

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': [
                '%s:%s' % (self.image.split('/')[-1],
                           self.tag)]
        }]

        # For out-of-order layer delivery
        self._indexed_layers = []

    @property
    def requires_ordered_layers(self):
        return False

    def fetch_callback(self, digest):
        return True

    def process_image_element(self, element):
        if element.element_type == constants.CONFIG_FILE:
            LOG.debug('Writing config file to tarball')

            ti = tarfile.TarInfo(element.name)
            ti.size = len(element.data.read())
            element.data.seek(0)
            self.image_tar.addfile(ti, element.data)
            self.tar_manifest[0]['Config'] = element.name
            self._track_element(
                element.element_type, ti.size)

        elif element.element_type == constants.IMAGE_LAYER:
            LOG.debug('Writing layer to tarball')

            layer_name = element.name + '/layer.tar'
            ti = tarfile.TarInfo(layer_name)
            element.data.seek(0, os.SEEK_END)
            ti.size = element.data.tell()
            element.data.seek(0)
            self.image_tar.addfile(ti, element.data)
            self._track_element(
                element.element_type, ti.size)

            if element.layer_index is not None:
                self._indexed_layers.append(
                    (element.layer_index, layer_name))
            else:
                self.tar_manifest[0][
                    'Layers'].append(layer_name)

    def finalize(self):
        # Reconstruct layer order from indices
        if self._indexed_layers:
            self._indexed_layers.sort(
                key=lambda x: x[0])
            self.tar_manifest[0]['Layers'] = [
                name for _, name
                in self._indexed_layers]

        LOG.debug('Writing manifest file to tarball')
        encoded_manifest = json.dumps(
            self.tar_manifest).encode('utf-8')
        ti = tarfile.TarInfo('manifest.json')
        ti.size = len(encoded_manifest)
        self.image_tar.addfile(
            ti, io.BytesIO(encoded_manifest))
        self.image_tar.close()
        self._log_summary()

    def verify(self, full=False):
        """Verify the tarball output is complete.

        Re-opens the tarball read-only and checks that
        manifest.json, the config file, and all layer
        entries are present. In full mode, additionally
        validates each layer entry is a valid tarball.
        """
        from occystrap.check import CheckResults
        results = CheckResults()

        # Check tarball exists
        if not os.path.exists(self.image_path):
            results.error(
                'verify.tarball',
                'Tarball missing: %s'
                % self.image_path)
            return results

        try:
            tf = tarfile.open(self.image_path, 'r')
        except tarfile.TarError as e:
            results.error(
                'verify.tarball',
                'Cannot open tarball: %s' % e)
            return results

        try:
            members = {m.name for m in tf.getmembers()}

            # Check manifest.json
            if 'manifest.json' not in members:
                results.error(
                    'verify.manifest',
                    'manifest.json missing from tarball')
                return results

            try:
                mf = tf.extractfile('manifest.json')
                manifest = json.loads(mf.read())
                if not isinstance(manifest, list) \
                        or not manifest:
                    results.error(
                        'verify.manifest',
                        'manifest.json is not a'
                        ' non-empty list')
                    return results
                if 'Layers' not in manifest[0]:
                    results.error(
                        'verify.manifest',
                        'Manifest missing Layers key')
                if 'Config' not in manifest[0]:
                    results.error(
                        'verify.manifest',
                        'Manifest missing Config key')
            except (json.JSONDecodeError, OSError) as e:
                results.error(
                    'verify.manifest',
                    'Cannot read manifest.json: %s' % e)
                return results

            # Check config entry
            config_name = manifest[0].get('Config')
            if config_name and config_name not in members:
                results.error(
                    'verify.config',
                    'Config entry missing from tarball:'
                    ' %s' % config_name)

            # Check layer entries
            expected_layers = \
                self.tar_manifest[0].get('Layers', [])
            for layer_name in expected_layers:
                if layer_name not in members:
                    results.error(
                        'verify.layer',
                        'Layer entry missing from'
                        ' tarball: %s' % layer_name)
                elif full:
                    try:
                        layer_fobj = tf.extractfile(
                            layer_name)
                        with tarfile.open(
                                fileobj=layer_fobj) \
                                as inner:
                            inner.getmembers()
                    except (tarfile.TarError,
                            OSError) as e:
                        results.error(
                            'verify.layer',
                            'Layer is not a valid tar:'
                            ' %s (%s)'
                            % (layer_name, e))

            if not results.has_errors:
                results.info(
                    'verify.ok',
                    'All %d layers verified in tarball'
                    % len(expected_layers))
        finally:
            tf.close()

        return results
