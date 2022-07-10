import logging
import os
import tempfile
import testtools


from occystrap import docker_registry
from occystrap import output_directory


logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class DirDeepImageTestCase(testtools.TestCase):
    def test_deep_image(self):
        image = 'library/ubuntu'
        tag = 'latest'

        with tempfile.TemporaryDirectory() as tempdir:
            oci = output_directory.DirWriter(
                image, tag, tempdir, expand=True)
            img = docker_registry.Image(
                'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
            for image_element in img.fetch(fetch_callback=oci.fetch_callback):
                oci.process_image_element(*image_element)
            oci.finalize()
            oci.write_bundle()

            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'manifest')))
            self.assertTrue(
                os.path.exists(os.path.join(tempdir, 'manifest/usr/bin/dash')))
