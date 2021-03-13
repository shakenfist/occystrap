import click
import logging

from occystrap import docker_registry
from occystrap import output_directory
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


def _fetch(registry, image, tag, output):
    img = docker_registry.Image(registry, image, tag)
    for image_element in img.fetch():
        output.process_image_element(*image_element)
    output.finalize()


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.pass_context
def fetch_to_extracted(ctx, registry, image, tag, path):
    d = output_directory.DirWriter(image, tag, path)
    _fetch(registry, image, tag, d)


cli.add_command(fetch_to_extracted)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.pass_context
def fetch_to_tarfile(ctx, registry, image, tag, tarfile):
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    _fetch(registry, image, tag, tar)


cli.add_command(fetch_to_tarfile)
