
import json
import logging
import os
from oslo_concurrency import processutils
import tempfile
import testtools


from occystrap import docker_registry
from occystrap import output_tarfile


logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


class TarFileTestCase(testtools.TestCase):
    def _list_docker_images(self):
        stdout, stderr = processutils.execute(
            'docker image list --format "{{json .}}"', shell=True)
        self.assertEqual(0, len(stderr))

        images = []
        for line in stdout.split('\n'):
            if line:
                images.append(json.loads(line))

        return images

    def _filter_image_list(self, images, image, tag):
        for entry in images:
            if entry['Repository'] == image and entry['Tag'] == tag:
                yield entry

    def assertImagePresent(self, image, tag):
        images = self._list_docker_images()
        found = list(self._filter_image_list(images, image, tag))
        if not found:
            pretty_all_images = ', '.join(
                f'{img["Repository"]}:{img["Tag"]}' for img in images
            )
            self.fail(f'{image}:{tag} not found in {pretty_all_images}')

    def assertImageNotPresent(self, image, tag):
        images = self._list_docker_images()
        found = list(self._filter_image_list(images, image, tag))
        if found:
            self.fail(f'{image}:{tag} present')

    def test_tarfile_loads(self):
        self.assertImageNotPresent('busybox', 'latest')

        with tempfile.TemporaryDirectory() as tempdir:
            # Fetch to a tar file
            tarfile = os.path.join(tempdir, 'busybox.tar')
            tar = output_tarfile.TarWriter(
                'library/busybox', 'latest', tarfile)
            img = docker_registry.Image(
                'registry-1.docker.io', 'library/busybox', 'latest', 'linux',
                'amd64', '', secure=True)
            for image_element in img.fetch(fetch_callback=tar.fetch_callback):
                tar.process_image_element(*image_element)
            tar.finalize()

            self.assertTrue(os.path.exists(tarfile))

            # Attempt to load that tar file into docker
            processutils.execute(f'docker load -i {tarfile}', shell=True)

            # Ensure that docker now has that image
            self.assertImagePresent('busybox', 'latest')

    def test_tarfile_loads_commandline(self):
        self.assertImageNotPresent('qemu-static', '9.2.0')

        with tempfile.TemporaryDirectory() as tempdir:
            tarfile = os.path.join(tempdir, 'qemu.tar')

            # Fetch to a tar file
            processutils.execute(
                'occystrap --verbose fetch-to-tarfile registry-1.docker.io '
                f'linuxserver/qemu-static 9.2.0 {tarfile}', shell=True)

            self.assertTrue(os.path.exists(tarfile))

            # Attempt to load that tar file into docker
            processutils.execute(f'docker load -i {tarfile}', shell=True)

            # Ensure that docker now has that image
            self.assertImagePresent('qemu-static', '9.2.0')
