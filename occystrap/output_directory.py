import json
import logging
import os
import tarfile

from occystrap import constants


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


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


class DirWriter(object):
    def __init__(self, image, tag, image_path, unique_names=False, expand=False):
        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.unique_names = unique_names
        self.expand = expand

        self.tar_manifest = [{
            'Layers': [],
            'RepoTags': ['%s:%s' % (self.image.split('/')[-1], self.tag)]
        }]
        if self.unique_names:
            self.tar_manifest[0]['ImageName'] = self.image

        self.bundle = {}

        if not os.path.exists(self.image_path):
            os.makedirs(self.image_path)

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
        layer_file_in_dir = os.path.join(self.image_path, digest, 'layer.tar')
        LOG.info('Layer file is %s' % layer_file_in_dir)
        return not os.path.exists(layer_file_in_dir)

    def process_image_element(self, element_type, name, data):
        if element_type == constants.CONFIG_FILE:
            with open(os.path.join(self.image_path, name), 'wb') as f:
                d = json.loads(data.read())
                f.write(json.dumps(d, indent=4, sort_keys=True).encode('ascii'))
            self.tar_manifest[0]['Config'] = name

        elif element_type == constants.IMAGE_LAYER:
            layer_dir = os.path.join(self.image_path, name)
            if not os.path.exists(layer_dir):
                os.makedirs(layer_dir)

            layer_file = os.path.join(name, 'layer.tar')
            self.tar_manifest[0]['Layers'].append(layer_file)

            layer_file_in_dir = os.path.join(self.image_path, layer_file)
            if os.path.exists(layer_file_in_dir):
                LOG.info('Skipping layer already in output directory')
            else:
                with open(layer_file_in_dir, 'wb') as f:
                    d = data.read(102400)
                    while d:
                        f.write(d)
                        d = data.read(102400)

            if self.expand:
                # Build a in-memory map of the layout of the final image bundle
                with tarfile.open(layer_file_in_dir) as layer:
                    for mem in layer.getmembers():
                        path = mem.name
                        dirname, filename = os.path.split(mem.name)

                        # Some light reading on how this works...
                        # https://github.com/opencontainers/image-spec/blob/main/layer.md#opaque-whiteout
                        if filename == '.wh..wh..opq':
                            # A deleted directory, but only for layers below
                            # this one.
                            for ent in self.bundle:
                                if (ent.startswith(dirname) and
                                        self.bundle[ent][-1].tarpath != layer_file):
                                    self.bundle[ent].append(
                                        BundleDeletedFile(ent, layer_file, mem))
                            continue

                        elif filename.startswith('.wh.'):
                            # A single deleted element, which might not be a
                            # file.
                            path = os.path.join(dirname, filename[4:])
                            if type(self.bundle[path][-1]) is BundleDirectory:
                                for ent in self.bundle:
                                    if ent.startswith(path):
                                        self.bundle[ent].append(
                                            BundleDeletedFile(ent, layer_file, mem))

                            serialized = BundleDeletedFile(
                                path, layer_file, mem)
                        else:
                            serialized = TARFILE_TYPE_MAP[mem.type](
                                mem.name, layer_file, mem)

                        self.bundle.setdefault(path, [])
                        self.bundle[path].append(serialized)

    def _log_bundle(self):
        savings = 0

        for path in self.bundle:
            versions = len(self.bundle[path])
            if versions > 1:
                path_savings = 0
                LOG.info('Bundle path "%s" has %d versions'
                         % (path, versions))
                for ver in self.bundle[path][:-1]:
                    path_savings += ver.size
                if type(self.bundle[path][-1]) is BundleDeletedFile:
                    LOG.info('Bundle path "%s" final version is a deleted file, '
                             'which wasted %d bytes.' % (path, path_savings))
                savings += path_savings

        LOG.info('Flattening image would save %d bytes' % savings)

    def finalize(self):
        if self.expand:
            self._log_bundle()

        manifest_filename = self._manifest_filename() + '.json'
        manifest_path = os.path.join(self.image_path, manifest_filename)
        with open(manifest_path, 'wb') as f:
            f.write(json.dumps(self.tar_manifest, indent=4,
                               sort_keys=True).encode('ascii'))

        c = {}
        catalog_path = os.path.join(self.image_path, 'catalog.json')
        if os.path.exists(catalog_path):
            with open(catalog_path, 'r') as f:
                c = json.loads(f.read())

        c.setdefault(self.image, {})
        c[self.image][self.tag] = manifest_filename
        with open(catalog_path, 'w') as f:
            f.write(json.dumps(c, indent=4, sort_keys=True))

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
            with tarfile.open(os.path.join(self.image_path, tarpath)) as layer:
                for ent in entities_by_layer[tarpath]:
                    layer.extract(ent.name, path=rootfs_path)

        for tarpath in deferred_by_layer:
            with tarfile.open(os.path.join(self.image_path, tarpath)) as layer:
                for ent in deferred_by_layer[tarpath]:
                    layer.extract(ent.name, path=rootfs_path)

    def write_bundle(self):
        manifest_filename = self._manifest_filename()
        manifest_path = os.path.join(self.image_path, manifest_filename)
        if not os.path.exists(manifest_path):
            os.makedirs(manifest_path)
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
        with open(os.path.join(self.path, self.manifest_filename)) as f:
            manifest = json.loads(f.read())

        config_filename = manifest[0]['Config']
        with open(os.path.join(self.path, config_filename), 'rb') as f:
            yield (constants.CONFIG_FILE, config_filename, f)

        for layer in manifest[0]['Layers']:
            with open(os.path.join(self.path, layer), 'rb') as f:
                yield (constants.IMAGE_LAYER, layer, f)
