import click
import logging

from occystrap import docker_registry

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
    img.fetch(tarfile)


cli.add_command(fetch)
