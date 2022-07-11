
import logging
import os
from oslo_concurrency import processutils
import tempfile
import testtools


from occystrap import docker_registry
from occystrap import output_ocibundle
from occystrap import output_mounts


logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class WhiteoutsTestCase(testtools.TestCase):
    def test_whiteouts_ocibundle(self):
        image = 'occystrap_deletion_layers'
        tag = 'latest'

        with tempfile.TemporaryDirectory() as tempdir:
            oci = output_ocibundle.OCIBundleWriter(image, tag, tempdir)
            img = docker_registry.Image(
                'localhost:5000', image, tag, 'linux', 'amd64', '',
                secure=False)
            for image_element in img.fetch(fetch_callback=oci.fetch_callback):
                oci.process_image_element(*image_element)
            oci.finalize()
            oci.write_bundle()

            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'rootfs')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir, 'rootfs/file')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir, 'rootfs/.wh.file')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir, 'rootfs/directory')))
            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'rootfs/anotherfile')))
            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'rootfs/anotherdirectory')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir,
                                            'rootfs/anotherdirectory/.wh..wh..opq')))

    def test_whiteouts_mounts(self):
        image = 'occystrap_deletion_layers'
        tag = 'latest'

        with tempfile.TemporaryDirectory() as tempdir:
            oci = output_mounts.MountWriter(image, tag, tempdir)
            img = docker_registry.Image(
                'localhost:5000', image, tag, 'linux', 'amd64', '',
                secure=False)
            for image_element in img.fetch(fetch_callback=oci.fetch_callback):
                oci.process_image_element(*image_element)
            oci.finalize()
            oci.write_bundle()

            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'rootfs')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir, 'rootfs/file')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir, 'rootfs/.wh.file')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir, 'rootfs/directory')))
            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'rootfs/anotherfile')))
            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'rootfs/anotherdirectory')))
            self.assertFalse(
                os.path.exists(os.path.join(tempdir,
                                            'rootfs/anotherdirectory/.wh..wh..opq')))

            processutils.execute('umount %s' % os.path.join(tempdir, 'rootfs'),
                                 shell=True)
