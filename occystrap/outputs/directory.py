import json
import os
import shutil
import tarfile
import threading

from occystrap import constants
from occystrap.check import (
    CheckResults, validate_manifest_structure)
from occystrap.outputs.base import ImageOutput
from occystrap.util import safe_path_join
from shakenfist_utilities import logs


LOG = logs.setup_console(__name__)

# Lock for catalog.json updates in finalize(). Multiple
# DirWriter instances (from concurrent image processing)
# may write to the same catalog.json file, so we serialize
# the read-modify-write to prevent data loss.
_catalog_lock = threading.Lock()


# Python supports the following tarfile object types: REGTYPE, AREGTYPE,
# LNKTYPE, SYMTYPE, DIRTYPE, FIFOTYPE, CONTTYPE, CHRTYPE, BLKTYPE,
# GNUTYPE_SPARSE. It also supports GNUTYPE_LONGNAME, GNUTYPE_LONGLINK but
# they are not mentioned in the documentation just to keep things fun.
class BundleObject(object):
    def __init__(self, name, tarpath, ti):
        self.name = name
        self.tarpath = tarpath
        self.size = 0


class BundleFile(BundleObject):
    def __init__(self, name, tarpath, ti):
        super(BundleFile, self).__init__(name, tarpath, ti)
        self.size = ti.size
        self.mtime = ti.mtime
        self.mode = ti.mode
        self.uid = ti.uid
        self.gid = ti.gid
        self.uname = ti.uname
        self.gname = ti.gname


class BundleDeletedFile(BundleObject):
    pass


class BundleLink(BundleObject):
    def __init__(self, name, tarpath, ti):
        super(BundleLink, self).__init__(name, tarpath, ti)
        self.linkname = ti.linkname
        self.mtime = ti.mtime
        self.mode = ti.mode
        self.uid = ti.uid
        self.gid = ti.gid
        self.uname = ti.uname
        self.gname = ti.gname


class BundleHardLink(BundleLink):
    pass


class BundleSymLink(BundleLink):
    pass


class BundleDirectory(BundleObject):
    def __init__(self, name, tarpath, ti):
        super(BundleDirectory, self).__init__(name, tarpath, ti)
        self.mtime = ti.mtime
        self.mode = ti.mode
        self.uid = ti.uid
        self.gid = ti.gid
        self.uname = ti.uname
        self.gname = ti.gname


class BundleFIFO(BundleObject):
    def __init__(self, name, tarpath, ti):
        super(BundleFIFO, self).__init__(name, tarpath, ti)
        self.mtime = ti.mtime
        self.mode = ti.mode
        self.uid = ti.uid
        self.gid = ti.gid
        self.uname = ti.uname
        self.gname = ti.gname


TARFILE_TYPE_MAP = {
    tarfile.REGTYPE: BundleFile,
    tarfile.AREGTYPE: BundleFile,
    'deleted': BundleDeletedFile,
    tarfile.LNKTYPE: BundleHardLink,
    tarfile.SYMTYPE: BundleSymLink,
    tarfile.DIRTYPE: BundleDirectory,
    tarfile.FIFOTYPE: BundleFIFO,
    tarfile.CONTTYPE: BundleFile,
    tarfile.CHRTYPE: BundleFile,
    tarfile.BLKTYPE: BundleFile,
    tarfile.GNUTYPE_SPARSE: BundleFile,
}


class DirWriter(ImageOutput):
    def __init__(self, image, tag, image_path,
                 unique_names=False, expand=False):
        super().__init__()

        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.unique_names = unique_names
        self.expand = expand

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': [
                '%s:%s' % (self.image.split('/')[-1],
                           self.tag)]
        }]
        if self.unique_names:
            self.tar_manifest[0]['ImageName'] = \
                self.image

        self.bundle = {}
        os.makedirs(self.image_path, exist_ok=True)

        # For out-of-order layer delivery
        self._indexed_layers = []

        # Verification expectations: recorded during
        # process_image_element(), checked in verify().
        self._expected_layers = {}
        self._expected_config_size = None

    @property
    def requires_ordered_layers(self):
        # expand=True needs ordered layers for
        # whiteout file handling
        return self.expand

    def _manifest_filename(self):
        if not self.unique_names:
            return 'manifest'
        else:
            return ('manifest-%s-%s' % (self.image.replace('/', '_'),
                                        self.tag.replace('/', '_')))

    def _create_bundle_path(self, path):
        d = self.bundle
        for elem in path.split('/'):
            if elem not in d:
                d[elem] = {}
            d = d[elem]
        return d

    def fetch_callback(self, digest):
        layer_file_in_dir = safe_path_join(
            self.image_path, digest, 'layer.tar')
        LOG.debug('Layer file is %s' % layer_file_in_dir)
        return not os.path.exists(layer_file_in_dir)

    def process_image_element(self, element):
        if element.element_type == constants.CONFIG_FILE:
            config_file = safe_path_join(
                self.image_path, element.name)
            config_dir = os.path.dirname(config_file)
            os.makedirs(config_dir, exist_ok=True)

            config_data = element.data.read()
            with open(config_file, 'wb') as f:
                d = json.loads(config_data)
                written = json.dumps(
                    d, indent=4,
                    sort_keys=True).encode('ascii')
                f.write(written)
            self.tar_manifest[0]['Config'] = element.name
            self._expected_config_size = len(written)
            self._track_element(
                element.element_type, len(config_data))

        elif element.element_type == constants.IMAGE_LAYER:
            layer_dir = safe_path_join(
                self.image_path, element.name)
            os.makedirs(layer_dir, exist_ok=True)

            layer_file = os.path.join(
                element.name, 'layer.tar')

            if element.layer_index is not None:
                self._indexed_layers.append(
                    (element.layer_index, layer_file))
            else:
                self.tar_manifest[0][
                    'Layers'].append(layer_file)

            layer_file_in_dir = safe_path_join(
                self.image_path, layer_file)
            layer_size = 0
            layer_preexisted = False
            if os.path.exists(layer_file_in_dir):
                LOG.debug(
                    'Skipping layer already in'
                    ' output directory')
                layer_size = os.path.getsize(
                    layer_file_in_dir)
                layer_preexisted = True
            elif element.temp_path:
                # Move the temp file directly instead
                # of copying data through a read loop.
                # This is O(1) on the same filesystem.
                try:
                    os.rename(
                        element.temp_path,
                        layer_file_in_dir)
                    LOG.debug(
                        'Renamed temp file to %s'
                        % layer_file_in_dir)
                except OSError:
                    # Cross-filesystem: fall back to
                    # shutil.move (copies then deletes)
                    shutil.move(
                        element.temp_path,
                        layer_file_in_dir)
                    LOG.debug(
                        'Moved temp file to %s'
                        ' (cross-filesystem)'
                        % layer_file_in_dir)
                layer_size = os.path.getsize(
                    layer_file_in_dir)
            else:
                with open(layer_file_in_dir, 'wb') as f:
                    d = element.data.read(102400)
                    while d:
                        layer_size += len(d)
                        f.write(d)
                        d = element.data.read(102400)
            # Don't record expected size for layers that
            # already existed on disk — concurrent images
            # sharing layers via deduplication can overwrite
            # each other, making the size unpredictable.
            # We still verify the file exists.
            if layer_preexisted:
                self._expected_layers[layer_file] = None
            else:
                self._expected_layers[layer_file] = \
                    layer_size
            self._track_element(
                element.element_type, layer_size)

            if self.expand:
                # Build an in-memory map of the layout
                # of the final image bundle
                with tarfile.open(
                        layer_file_in_dir) as layer:
                    for mem in layer.getmembers():
                        path = mem.name
                        dirname, filename = \
                            os.path.split(mem.name)

                        # Some light reading on how this
                        # works...
                        # https://github.com/opencontainers/image-spec/blob/main/layer.md#opaque-whiteout
                        if filename == '.wh..wh..opq':
                            # A deleted directory, but
                            # only for layers below this
                            # one.
                            for ent in self.bundle:
                                if (
                                    ent.startswith(dirname)
                                    and self.bundle[
                                        ent][-1].tarpath
                                    != layer_file
                                ):
                                    self.bundle[
                                        ent].append(
                                        BundleDeletedFile(
                                            ent,
                                            layer_file,
                                            mem))
                            continue

                        elif filename.startswith('.wh.'):
                            # A single deleted element,
                            # which might not be a file.
                            path = os.path.join(
                                dirname, filename[4:])
                            if type(
                                self.bundle[path][-1]
                            ) is BundleDirectory:
                                for ent in self.bundle:
                                    if ent.startswith(
                                            path):
                                        self.bundle[
                                            ent].append(
                                            BundleDeletedFile(
                                                ent,
                                                layer_file,
                                                mem))

                            serialized = \
                                BundleDeletedFile(
                                    path, layer_file,
                                    mem)
                        else:
                            serialized = \
                                TARFILE_TYPE_MAP[
                                    mem.type](
                                    mem.name,
                                    layer_file, mem)

                        self.bundle.setdefault(path, [])
                        self.bundle[path].append(
                            serialized)

    def _log_bundle(self):
        savings = 0

        for path in self.bundle:
            versions = len(self.bundle[path])
            if versions > 1:
                path_savings = 0
                LOG.debug('Bundle path "%s" has %d versions'
                          % (path, versions))
                for ver in self.bundle[path][:-1]:
                    path_savings += ver.size
                if type(self.bundle[path][-1]) is BundleDeletedFile:
                    LOG.debug('Bundle path "%s" final version is a deleted file, '
                              'which wasted %d bytes.' % (path, path_savings))
                savings += path_savings

        LOG.info('Flattening image would save %d bytes' % savings)

    def finalize(self):
        # Reconstruct layer order from indices
        if self._indexed_layers:
            self._indexed_layers.sort(
                key=lambda x: x[0])
            self.tar_manifest[0]['Layers'] = [
                name for _, name
                in self._indexed_layers]

        if self.expand:
            self._log_bundle()

        manifest_filename = self._manifest_filename() + '.json'
        manifest_path = safe_path_join(
            self.image_path, manifest_filename)
        with open(manifest_path, 'wb') as f:
            f.write(json.dumps(self.tar_manifest, indent=4,
                               sort_keys=True).encode('ascii'))

        catalog_path = os.path.join(
            self.image_path, 'catalog.json')
        with _catalog_lock:
            c = {}
            if os.path.exists(catalog_path):
                with open(catalog_path, 'r') as f:
                    c = json.loads(f.read())

            c.setdefault(self.image, {})
            c[self.image][self.tag] = manifest_filename
            with open(catalog_path, 'w') as f:
                f.write(json.dumps(
                    c, indent=4, sort_keys=True))

        self._log_summary()

    def verify(self, full=False):
        """Verify the directory output is complete.

        Checks that the manifest, config, and all layer
        files exist with the expected sizes. In full
        mode, additionally validates each layer is a
        valid tarball.
        """
        results = CheckResults()

        # Check manifest file
        manifest_filename = self._manifest_filename()
        manifest_path = safe_path_join(
            self.image_path,
            '%s.json' % manifest_filename)
        if not os.path.exists(manifest_path):
            results.error(
                'verify.manifest',
                'Manifest file missing: %s'
                % manifest_path)
            return results

        try:
            with open(manifest_path, 'r') as f:
                manifest = json.loads(f.read())
            if not validate_manifest_structure(
                    manifest, results):
                return results
        except (json.JSONDecodeError, OSError) as e:
            results.error(
                'verify.manifest',
                'Cannot read manifest: %s' % e)
            return results

        # Check config file
        config_name = self.tar_manifest[0].get('Config')
        if config_name:
            config_path = safe_path_join(
                self.image_path, config_name)
            if not os.path.exists(config_path):
                results.error(
                    'verify.config',
                    'Config file missing: %s'
                    % config_name)
            elif self._expected_config_size is not None:
                actual = os.path.getsize(config_path)
                if actual != self._expected_config_size:
                    results.error(
                        'verify.config',
                        'Config size mismatch: expected'
                        ' %d, got %d'
                        % (self._expected_config_size,
                           actual))

        # Check layer files
        layers = self.tar_manifest[0].get('Layers', [])
        for layer_path in layers:
            layer_file = safe_path_join(
                self.image_path, layer_path)
            if not os.path.exists(layer_file):
                results.error(
                    'verify.layer',
                    'Layer file missing: %s'
                    % layer_path)
                continue

            expected_size = self._expected_layers.get(
                layer_path)
            if expected_size is not None:
                actual = os.path.getsize(layer_file)
                if actual != expected_size:
                    results.error(
                        'verify.layer',
                        'Layer size mismatch for %s:'
                        ' expected %d, got %d'
                        % (layer_path, expected_size,
                           actual))

            if full:
                try:
                    with tarfile.open(layer_file) as tf:
                        tf.getmembers()
                except (tarfile.TarError, OSError) as e:
                    results.error(
                        'verify.layer',
                        'Layer is not a valid tar: %s'
                        ' (%s)' % (layer_path, e))

        if not results.has_errors:
            results.info(
                'verify.ok',
                'All %d layers verified'
                % len(layers))

        return results

    def _extract_rootfs(self, rootfs_path):
        # Reading tarfiles is expensive, as tarfile needs to scan the
        # entire file to find the right entry. It builds a cache while
        # doing this however, so performance improves if you access a
        # bunch of files from the same archive. We therefore group
        # entities by layer to improve performance.
        entities_by_layer = {}

        # We defer changing the permissions of directories until later
        # so that permissions don't affect the writing of files.
        deferred_by_layer = {}

        # Find all the entities
        for path in self.bundle:
            ent = self.bundle[path][-1]

            if type(ent) is BundleDirectory:
                deferred_by_layer.setdefault(ent.tarpath, [])
                deferred_by_layer[ent.tarpath].append(ent)
                continue

            if type(ent) is BundleDeletedFile:
                continue

            entities_by_layer.setdefault(ent.tarpath, [])
            entities_by_layer[ent.tarpath].append(ent)

        for tarpath in entities_by_layer:
            with tarfile.open(safe_path_join(self.image_path, tarpath)) as layer:
                for ent in entities_by_layer[tarpath]:
                    layer.extract(ent.name, path=rootfs_path)

        for tarpath in deferred_by_layer:
            with tarfile.open(safe_path_join(self.image_path, tarpath)) as layer:
                for ent in deferred_by_layer[tarpath]:
                    layer.extract(ent.name, path=rootfs_path)

    def write_bundle(self):
        manifest_filename = self._manifest_filename()
        manifest_path = safe_path_join(
            self.image_path, manifest_filename)
        os.makedirs(manifest_path, exist_ok=True)
        LOG.info('Writing image bundle to %s' % manifest_path)
        self._extract_rootfs(manifest_path)


class NoSuchImageException(Exception):
    pass


class DirReader(object):
    def __init__(self, path, image, tag):
        self.path = path
        self.image = image
        self.tag = tag

        c = {}
        catalog_path = os.path.join(self.path, 'catalog.json')
        if os.path.exists(catalog_path):
            with open(catalog_path, 'r') as f:
                c = json.loads(f.read())

        if self.image not in c:
            raise NoSuchImageException(self.image)
        if self.tag not in c[self.image]:
            raise NoSuchImageException(self.image)

        self.manifest_filename = c[self.image][self.tag]

    def fetch(self):
        with open(safe_path_join(
                self.path,
                self.manifest_filename)) as f:
            manifest = json.loads(f.read())

        config_filename = manifest[0]['Config']
        with open(safe_path_join(
                self.path, config_filename), 'rb') as f:
            yield constants.ImageElement(
                constants.CONFIG_FILE,
                config_filename, f)

        for layer in manifest[0]['Layers']:
            with open(safe_path_join(
                    self.path, layer), 'rb') as f:
                yield constants.ImageElement(
                    constants.IMAGE_LAYER, layer, f)
