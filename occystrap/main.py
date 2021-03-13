import click
import logging

from occystrap import docker_registry
from occystrap import output_tarfile

logging.basicConfig(level=logging.INFO)

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


@click.group()
@click.option('--verbose/--no-verbose', default=False)
@click.pass_context
def cli(ctx, verbose=None):
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
        LOG.setLevel(logging.DEBUG)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.pass_context
def fetch(ctx, registry, image, tag, tarfile):
    img = docker_registry.Image(registry, image, tag)
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    for image_element in img.fetch():
        tar.process_image_element(*image_element)
    tar.finalize()


cli.add_command(fetch)
