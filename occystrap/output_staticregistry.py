import gzip
import hashlib
import json
import logging
import os
from shakenfist_utilities import random as sf_random
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
    def __init__(self, image, tag, image_path, catalog_file='catalog.json'):
        self.image = image
        self.tag = tag
        self.image_path = image_path
        self.catalog_file = catalog_file

        self.index = {
            'manifests': [
                {

                }
            ],
            'mediaType': 'application/vnd.oci.image.index.v1+json',
            'schemaVersion': 2
        }

        self.manifest = {
            'config': {},
            'layers': [],
            'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'schemaVersion': 2
        }

        self.index_filename = \
            '%s/v2/%s/manifests/%s' % (self.image_path, self.image, self.tag)
        self.manifest_dir = os.path.dirname(self.index_filename)
        os.makedirs(self.manifest_dir, exist_ok=True)
        os.makedirs(
            '%s/v2/%s/blobs' % (self.image_path, self.image), exist_ok=True)

        self.layer_dir = os.path.join(self.image_path, 'v2/blobs')
        os.makedirs(self.layer_dir, exist_ok=True)

        LOG.info(f'Index path will be {self.index_filename}')
        LOG.info(f'Shared layers will be at {self.layer_dir}')

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
        LOG.info(f'Processing image element {element_type}')
        if element_type == constants.INDEX_ENTRY:
            print('*******************************')
            print('index entry')
            print(name)
            print()
            print(data)
            print('*******************************')
            index_file = os.path.join(self.image_path, self.image, name)
            with open(index_file, 'wb') as f:
                d = json.loads(data.read())
                f.write(json.dumps(d, indent=4, sort_keys=True).encode('ascii'))

            htaccess_path = os.path.join(
                self.image_path, self.image, '.htaccess')
            with open(htaccess_path, 'a') as f:
                f.write(f"""
DirectoryIndex {name}
<FilesMatch "^{name}$">
    ForceType application/vnd.oci.image.index.v1+json
</FilesMatch>""")

        elif element_type == constants.CONFIG_FILE:
            d = json.dumps(json.loads(data.read()), indent=4,
                           sort_keys=True).encode('ascii')

            hasher = hashlib.sha256()
            hasher.update(d)
            hash = hasher.hexdigest()

            config_file = os.path.join(
                self.image_path, 'v2/blobs/sha256:%s' % hash)
            config_dir = os.path.dirname(config_file)
            os.makedirs(config_dir, exist_ok=True)
            with open(config_file, 'wb') as f:
                f.write(d)

            image_blob_path = ('%s/v2/%s/blobs/sha256:%s'
                               % (self.image_path, self.image, hash))
            if not os.path.exists(image_blob_path):
                os.symlink(config_file, image_blob_path)

            self.manifest['config']['digest'] = 'sha256:%s' % hash
            self.manifest['config']['mediaType'] = \
                'application/vnd.oci.image.index.v1+json'
            self.manifest['config']['size'] = len(d)

        elif element_type == constants.IMAGE_LAYER:
            size = 0
            hasher = hashlib.sha256()
            layer_file_in_dir = os.path.join(
                self.layer_dir, '.%s' % sf_random.random_id())
            with gzip.open(layer_file_in_dir, 'wb') as f:
                while d := data.read(102400):
                    f.write(d)
                    size += len(d)
                    hasher.update(d)

            hash = hasher.hexdigest()
            layer_file_final_location = os.path.join(
                self.layer_dir, 'sha256:%s' % hash)
            if not os.path.exists(layer_file_final_location):
                os.rename(layer_file_in_dir, layer_file_final_location)
            else:
                os.unlink(layer_file_in_dir)

            image_blob_path = ('%s/v2/%s/blobs/sha256:%s'
                               % (self.image_path, self.image, hash))
            if not os.path.exists(image_blob_path):
                os.symlink(layer_file_final_location, image_blob_path)

                htaccess_path = os.path.join(
                    os.path.dirname(image_blob_path), '.htaccess')
                htaccess_justfile = os.path.basename(image_blob_path)
                with open(htaccess_path, 'a') as f:
                    f.write("""
<FilesMatch "^%s$">
    ForceType application/vnd.oci.image.layer.v1.tar+gzip
</FilesMatch>""" % htaccess_justfile)

            # Digest and size are _before_ gzip compression
            self.manifest['layers'].append({
                'digest': 'sha256:%s' % hash,
                'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip',
                'size': size
            })

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

    def _set_htaccess_mime_type(self, file, mime_type):
        dir = os.path.dirname(file)
        just_file = os.path.basename(file)

        htaccess_path = os.path.join(dir, '.htaccess')
        with open(htaccess_path, 'a') as f:
            f.write("""
<FilesMatch "^%s$">
    ForceType %s
</FilesMatch>""" % (just_file, mime_type))

        LOG.info(f'Set mime type for {file} to {mime_type} via .htaccess')

    def finalize(self):
        # There is an index file at image:tag which lists available manifests
        # by hash
        with open(self.index_filename, 'wb') as f:
            f.write(json.dumps(
                self.index, indent=4, sort_keys=True).encode('ascii')
            )
        LOG.info(f'Wrote index to {self.index_filename}')
        self._set_htaccess_mime_type(
            self.index_filename, 'application/vnd.oci.image.index.v1+json')

        # And then there's the manifest at that hash
        manifest_content = json.dumps(
            self.manifest, indent=4, sort_keys=True).encode('ascii')

        hasher = hashlib.sha256()
        hasher.update(manifest_content)
        hash = f'sha256:{hasher.hexdigest()}'

        manifest_path = os.path.join(self.manifest_dir, hash)
        with open(manifest_path, 'wb') as f:
            f.write(manifest_content)
        LOG.info(f'Wrote manifest to {manifest_path}')
        self._set_htaccess_mime_type(
            manifest_path, 'application/vnd.oci.image.manifest.v1+json')

        c = {}
        catalog_path = os.path.join(self.image_path, self.catalog_file)
        if os.path.exists(catalog_path):
            with open(catalog_path, 'r') as f:
                c = json.loads(f.read())
        LOG.info(f'Updated catalog at {catalog_path}')

        c.setdefault(self.image, {})
        c[self.image][self.tag] = self.index_filename
        with open(catalog_path, 'w') as f:
            f.write(json.dumps(c, indent=4, sort_keys=True))
