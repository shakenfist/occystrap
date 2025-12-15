import click
import logging
import os
from shakenfist_utilities import logs
import sys

from occystrap.inputs import registry as input_registry
from occystrap.inputs import tarfile as input_tarfile
from occystrap import output_directory
from occystrap import output_mounts
from occystrap import output_ocibundle
from occystrap import output_tarfile
from occystrap import search


LOG = logs.setup_console(__name__)


@click.group()
@click.option('--verbose', is_flag=True)
@click.option('--os', default='linux')
@click.option('--architecture', default='amd64')
@click.option('--variant', default='')
@click.option('--username', default=None, envvar='OCCYSTRAP_USERNAME',
              help='Username for registry authentication')
@click.option('--password', default=None, envvar='OCCYSTRAP_PASSWORD',
              help='Password for registry authentication')
@click.option('--insecure', is_flag=True, default=False,
              help='Use HTTP instead of HTTPS for registry connections')
@click.pass_context
def cli(ctx, verbose=None, os=None, architecture=None, variant=None,
        username=None, password=None, insecure=None):
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
        LOG.setLevel(logging.DEBUG)

    if not ctx.obj:
        ctx.obj = {}
    ctx.obj['OS'] = os
    ctx.obj['ARCHITECTURE'] = architecture
    ctx.obj['VARIANT'] = variant
    ctx.obj['USERNAME'] = username
    ctx.obj['PASSWORD'] = password
    ctx.obj['INSECURE'] = insecure


def _fetch(img, output):
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
@click.pass_context
def fetch_to_extracted(ctx, registry, image, tag, path, use_unique_names,
                       expand):
    d = output_directory.DirWriter(
        image, tag, path, unique_names=use_unique_names, expand=expand)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, d)

    if expand:
        d.write_bundle()


cli.add_command(fetch_to_extracted)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.pass_context
def fetch_to_oci(ctx, registry, image, tag, path):
    d = output_ocibundle.OCIBundleWriter(image, tag, path)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, d)
    d.write_bundle()


cli.add_command(fetch_to_oci)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.option('--normalize-timestamps', is_flag=True, default=False)
@click.option('--timestamp', default=0, type=int)
@click.pass_context
def fetch_to_tarfile(ctx, registry, image, tag, tarfile,
                     normalize_timestamps, timestamp):
    tar = output_tarfile.TarWriter(
        image, tag, tarfile,
        normalize_timestamps=normalize_timestamps,
        timestamp=timestamp)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, tar)


cli.add_command(fetch_to_tarfile)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.pass_context
def fetch_to_mounts(ctx, registry, image, tag, path):
    if not hasattr(os, 'setxattr'):
        print('Sorry, your OS module implementation lacks setxattr')
        sys.exit(1)
    if not hasattr(os, 'mknod'):
        print('Sorry, your OS module implementation lacks mknod')
        sys.exit(1)

    d = output_mounts.MountWriter(image, tag, path)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, d)
    d.write_bundle()


cli.add_command(fetch_to_mounts)


@click.command()
@click.argument('path')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.option('--normalize-timestamps', is_flag=True, default=False)
@click.option('--timestamp', default=0, type=int)
@click.pass_context
def recreate_image(ctx, path, image, tag, tarfile, normalize_timestamps,
                   timestamp):
    d = output_directory.DirReader(path, image, tag)
    tar = output_tarfile.TarWriter(
        image, tag, tarfile,
        normalize_timestamps=normalize_timestamps,
        timestamp=timestamp)
    for image_element in d.fetch():
        tar.process_image_element(*image_element)
    tar.finalize()


cli.add_command(recreate_image)


@click.command()
@click.argument('tarfile')
@click.argument('path')
@click.option('--use-unique-names', is_flag=True)
@click.option('--expand', is_flag=True)
@click.pass_context
def tarfile_to_extracted(ctx, tarfile, path, use_unique_names, expand):
    img = input_tarfile.Image(tarfile)
    d = output_directory.DirWriter(
        img.image, img.tag, path, unique_names=use_unique_names, expand=expand)
    _fetch(img, d)

    if expand:
        d.write_bundle()


cli.add_command(tarfile_to_extracted)


@click.command()
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('pattern')
@click.option('--regex', is_flag=True, default=False,
              help='Use regex pattern instead of glob pattern')
@click.option('--script-friendly', is_flag=True, default=False,
              help='Output in script-friendly format: image:tag:layer:path')
@click.pass_context
def search_layers(ctx, registry, image, tag, pattern, regex, script_friendly):
    """Search for files matching PATTERN in image layers from a registry."""
    searcher = search.LayerSearcher(
        pattern, use_regex=regex, image=image, tag=tag,
        script_friendly=script_friendly)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, searcher)


cli.add_command(search_layers)


@click.command()
@click.argument('tarfile')
@click.argument('pattern')
@click.option('--regex', is_flag=True, default=False,
              help='Use regex pattern instead of glob pattern')
@click.option('--script-friendly', is_flag=True, default=False,
              help='Output in script-friendly format: image:tag:layer:path')
@click.pass_context
def search_layers_tarfile(ctx, tarfile, pattern, regex, script_friendly):
    """Search for files matching PATTERN in image layers from a tarball."""
    img = input_tarfile.Image(tarfile)
    searcher = search.LayerSearcher(
        pattern, use_regex=regex, image=img.image, tag=img.tag,
        script_friendly=script_friendly)
    _fetch(img, searcher)


cli.add_command(search_layers_tarfile)
