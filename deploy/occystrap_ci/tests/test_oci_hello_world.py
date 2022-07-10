import logging
from oslo_concurrency import processutils
import sys
import tempfile
import testtools


from occystrap import docker_registry
from occystrap import output_ocibundle


logging.basicConfig(level=logging.INFO, format='%(message)s')
LOG = logging.getLogger()


def _exec(cmd, cwd=None):
    sys.stderr.write('\n----- Exec: %s -----\n' % cmd)
    out, err = processutils.execute(cmd, cwd=cwd, shell=True)
    for line in out.split('\n'):
        sys.stderr.write('out: %s\n' % line)
    sys.stderr.write('\n')
    for line in err.split('\n'):
        sys.stderr.write('err: %s\n' % line)
    sys.stderr.write('\n----- End: %s -----\n' % cmd)
    return out, err


# Make sure we haven't broken sudo entirely in the test suite
class SudoTestCase(testtools.TestCase):
    def test_sudo(self):
        out, err = _exec('sudo echo $(( 4 + 6 ))')
        self.assertEqual('', err)
        self.assertTrue('10' in out)


class OCIHelloWorldTestCase(testtools.TestCase):
    def test_hello_world(self):
        image = 'library/hello-world'
        tag = 'latest'

        with tempfile.TemporaryDirectory() as tempdir:
            oci = output_ocibundle.OCIBundleWriter(image, tag, tempdir)
            img = docker_registry.Image(
                'registry-1.docker.io', image, tag, 'linux', 'amd64', '')
            for image_element in img.fetch(fetch_callback=oci.fetch_callback):
                oci.process_image_element(*image_element)
            oci.finalize()
            oci.write_bundle()

            out, err = _exec('sudo runc run OCIHellWorld', cwd=tempdir)
            self.assertEqual('', err)
            self.assertTrue('Hello from Docker!' in out)
