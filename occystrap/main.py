import click
import logging
import os
from shakenfist_utilities import logs
import sys

from occystrap import docker_registry
from occystrap import output_directory
from occystrap import output_mounts
from occystrap import output_ocibundle
from occystrap import output_tarfile


LOG = logs.setup_console(__name__)


@click.group()
@click.option('--verbose', is_flag=True)
@click.option('--os', default='linux')
@click.option('--architecture', default='amd64')
@click.option('--variant', default='')
@click.pass_context
def cli(ctx, verbose=None, os=None, architecture=None, variant=None):
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
        LOG.setLevel(logging.DEBUG)

    if not ctx.obj:
        ctx.obj = {}
    ctx.obj['OS'] = os
    ctx.obj['ARCHITECTURE'] = architecture
    ctx.obj['VARIANT'] = variant


def _fetch(registry, image, tag, output, os, architecture, variant, secure=True):
    img = docker_registry.Image(
        registry, image, tag, os, architecture, variant, secure=secure)
    for image_element in img.fetch(fetch_callback=output.fetch_callback):
        output.process_image_element(*image_element)
    output.finalize()


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.option('--use-unique-names', is_flag=True)
@click.option('--expand', is_flag=True)
@click.option('--insecure', is_flag=True, default=False)
@click.pass_context
def fetch_to_extracted(ctx, registry, image, tag, path, use_unique_names,
                       expand, insecure):
    d = output_directory.DirWriter(
        image, tag, path, unique_names=use_unique_names, expand=expand)
    _fetch(registry, image, tag, d, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
           ctx.obj['VARIANT'], secure=(not insecure))

    if expand:
        d.write_bundle()


cli.add_command(fetch_to_extracted)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.option('--insecure', is_flag=True, default=False)
@click.pass_context
def fetch_to_oci(ctx, registry, image, tag, path, insecure):
    d = output_ocibundle.OCIBundleWriter(image, tag, path)
    _fetch(registry, image, tag, d, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
           ctx.obj['VARIANT'], secure=(not insecure))
    d.write_bundle()


cli.add_command(fetch_to_oci)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.option('--insecure', is_flag=True, default=False)
@click.pass_context
def fetch_to_tarfile(ctx, registry, image, tag, tarfile, insecure):
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    _fetch(registry, image, tag, tar, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
           ctx.obj['VARIANT'], secure=(not insecure))


cli.add_command(fetch_to_tarfile)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.option('--insecure', is_flag=True, default=False)
@click.pass_context
def fetch_to_mounts(ctx, registry, image, tag, path, insecure):
    if not hasattr(os, 'setxattr'):
        print('Sorry, your OS module implementation lacks setxattr')
        sys.exit(1)
    if not hasattr(os, 'mknod'):
        print('Sorry, your OS module implementation lacks mknod')
        sys.exit(1)

    d = output_mounts.MountWriter(image, tag, path)
    _fetch(registry, image, tag, d, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
           ctx.obj['VARIANT'], secure=(not insecure))
    d.write_bundle()


cli.add_command(fetch_to_mounts)


@click.command()
@click.argument('path')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.pass_context
def recreate_image(ctx, path, image, tag, tarfile):
    d = output_directory.DirReader(path, image, tag)
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    for image_element in d.fetch():
        tar.process_image_element(*image_element)
    tar.finalize()


cli.add_command(recreate_image)
