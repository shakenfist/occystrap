import click
import logging
import os
from shakenfist_utilities import logs
import sys

from occystrap.inputs import docker as input_docker
from occystrap.inputs import registry as input_registry
from occystrap.inputs import tarfile as input_tarfile
from occystrap.outputs import directory as output_directory
from occystrap.outputs import mounts as output_mounts
from occystrap.outputs import ocibundle as output_ocibundle
from occystrap.outputs import tarfile as output_tarfile
from occystrap.filters import SearchFilter, TimestampNormalizer
from occystrap.pipeline import PipelineBuilder, PipelineError
from occystrap import uri


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


# =============================================================================
# New URI-style commands
# =============================================================================

@click.command('process')
@click.argument('source')
@click.argument('destination')
@click.option('--filter', '-f', 'filters', multiple=True,
              help='Apply filter (can be specified multiple times)')
@click.pass_context
def process_cmd(ctx, source, destination, filters):
    """Process container images through a pipeline.

    SOURCE and DESTINATION are URIs specifying where to read from
    and write to.

    \b
    Input URI schemes:
      registry://HOST/IMAGE:TAG    - Docker/OCI registry
      docker://IMAGE:TAG           - Local Docker daemon
      tar://PATH                   - Docker-save tarball

    \b
    Output URI schemes:
      tar://PATH                   - Create tarball
      dir://PATH                   - Extract to directory
      oci://PATH                   - Create OCI bundle
      mounts://PATH                - Create overlay mounts

    \b
    Filters (use -f, can chain multiple):
      normalize-timestamps         - Normalize layer timestamps
      normalize-timestamps:ts=N    - Use specific timestamp
      search:pattern=GLOB          - Search for files
      search:pattern=RE,regex=true - Search with regex

    \b
    Examples:
      occystrap process registry://docker.io/library/busybox:latest tar://busybox.tar
      occystrap process docker://myimage:v1 dir://./extracted -f normalize-timestamps
      occystrap process tar://image.tar dir://out -f "search:pattern=*.conf"
    """
    try:
        builder = PipelineBuilder(ctx)
        input_source, output = builder.build_pipeline(
            source, destination, list(filters))
        _fetch(input_source, output)

        # Handle post-processing for certain outputs
        if hasattr(output, 'write_bundle'):
            # Check if this is an OCI or mounts output that needs write_bundle
            dest_spec = uri.parse_uri(destination)
            if dest_spec.scheme in ('oci', 'mounts'):
                output.write_bundle()
            elif dest_spec.scheme == 'dir' and dest_spec.options.get('expand'):
                output.write_bundle()

    except (PipelineError, uri.URIParseError) as e:
        click.echo('Error: %s' % e, err=True)
        sys.exit(1)


cli.add_command(process_cmd)


@click.command('search')
@click.argument('source')
@click.argument('pattern')
@click.option('--regex', is_flag=True, default=False,
              help='Use regex pattern instead of glob pattern')
@click.option('--script-friendly', is_flag=True, default=False,
              help='Output in script-friendly format: image:tag:layer:path')
@click.pass_context
def search_cmd(ctx, source, pattern, regex, script_friendly):
    """Search for files in container image layers.

    SOURCE is a URI specifying where to read the image from:
      registry://HOST/IMAGE:TAG    - Docker/OCI registry
      docker://IMAGE:TAG           - Local Docker daemon
      tar://PATH                   - Docker-save tarball

    PATTERN is a glob pattern (or regex with --regex).

    \b
    Examples:
      occystrap search registry://docker.io/library/busybox:latest "bin/*sh"
      occystrap search docker://myimage:v1 "*.conf"
      occystrap search --regex tar://image.tar ".*\\.py$"
    """
    try:
        builder = PipelineBuilder(ctx)
        input_source, searcher = builder.build_search_pipeline(
            source, pattern, use_regex=regex, script_friendly=script_friendly)
        _fetch(input_source, searcher)

    except (PipelineError, uri.URIParseError) as e:
        click.echo('Error: %s' % e, err=True)
        sys.exit(1)


cli.add_command(search_cmd)


# =============================================================================
# Legacy commands (kept for backwards compatibility)
# =============================================================================

@click.command(deprecated=True)
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.option('--use-unique-names', is_flag=True)
@click.option('--expand', is_flag=True)
@click.pass_context
def fetch_to_extracted(ctx, registry, image, tag, path, use_unique_names,
                       expand):
    """[DEPRECATED] Use: occystrap process registry://... dir://..."""
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


@click.command(deprecated=True)
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.pass_context
def fetch_to_oci(ctx, registry, image, tag, path):
    """[DEPRECATED] Use: occystrap process registry://... oci://..."""
    d = output_ocibundle.OCIBundleWriter(image, tag, path)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, d)
    d.write_bundle()


cli.add_command(fetch_to_oci)


@click.command(deprecated=True)
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.option('--normalize-timestamps', is_flag=True, default=False)
@click.option('--timestamp', default=0, type=int)
@click.pass_context
def fetch_to_tarfile(ctx, registry, image, tag, tarfile,
                     normalize_timestamps, timestamp):
    """[DEPRECATED] Use: occystrap process registry://... tar://..."""
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    if normalize_timestamps:
        tar = TimestampNormalizer(tar, timestamp=timestamp)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, tar)


cli.add_command(fetch_to_tarfile)


@click.command(deprecated=True)
@click.argument('registry')
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.pass_context
def fetch_to_mounts(ctx, registry, image, tag, path):
    """[DEPRECATED] Use: occystrap process registry://... mounts://..."""
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


@click.command(deprecated=True)
@click.argument('path')
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.option('--normalize-timestamps', is_flag=True, default=False)
@click.option('--timestamp', default=0, type=int)
@click.pass_context
def recreate_image(ctx, path, image, tag, tarfile, normalize_timestamps,
                   timestamp):
    """[DEPRECATED] Recreate image from shared directory."""
    d = output_directory.DirReader(path, image, tag)
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    if normalize_timestamps:
        tar = TimestampNormalizer(tar, timestamp=timestamp)
    for image_element in d.fetch():
        tar.process_image_element(*image_element)
    tar.finalize()


cli.add_command(recreate_image)


@click.command(deprecated=True)
@click.argument('tarfile')
@click.argument('path')
@click.option('--use-unique-names', is_flag=True)
@click.option('--expand', is_flag=True)
@click.pass_context
def tarfile_to_extracted(ctx, tarfile, path, use_unique_names, expand):
    """[DEPRECATED] Use: occystrap process tar://... dir://..."""
    img = input_tarfile.Image(tarfile)
    d = output_directory.DirWriter(
        img.image, img.tag, path, unique_names=use_unique_names, expand=expand)
    _fetch(img, d)

    if expand:
        d.write_bundle()


cli.add_command(tarfile_to_extracted)


@click.command(deprecated=True)
@click.argument('image')
@click.argument('tag')
@click.argument('tarfile')
@click.option('--socket', default='/var/run/docker.sock',
              help='Path to Docker socket')
@click.option('--normalize-timestamps', is_flag=True, default=False)
@click.option('--timestamp', default=0, type=int)
@click.pass_context
def docker_to_tarfile(ctx, image, tag, tarfile, socket, normalize_timestamps,
                      timestamp):
    """[DEPRECATED] Use: occystrap process docker://... tar://..."""
    tar = output_tarfile.TarWriter(image, tag, tarfile)
    if normalize_timestamps:
        tar = TimestampNormalizer(tar, timestamp=timestamp)
    img = input_docker.Image(image, tag, socket_path=socket)
    _fetch(img, tar)


cli.add_command(docker_to_tarfile)


@click.command(deprecated=True)
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.option('--socket', default='/var/run/docker.sock',
              help='Path to Docker socket')
@click.option('--use-unique-names', is_flag=True)
@click.option('--expand', is_flag=True)
@click.pass_context
def docker_to_extracted(ctx, image, tag, path, socket, use_unique_names,
                        expand):
    """[DEPRECATED] Use: occystrap process docker://... dir://..."""
    d = output_directory.DirWriter(
        image, tag, path, unique_names=use_unique_names, expand=expand)
    img = input_docker.Image(image, tag, socket_path=socket)
    _fetch(img, d)

    if expand:
        d.write_bundle()


cli.add_command(docker_to_extracted)


@click.command(deprecated=True)
@click.argument('image')
@click.argument('tag')
@click.argument('path')
@click.option('--socket', default='/var/run/docker.sock',
              help='Path to Docker socket')
@click.pass_context
def docker_to_oci(ctx, image, tag, path, socket):
    """[DEPRECATED] Use: occystrap process docker://... oci://..."""
    d = output_ocibundle.OCIBundleWriter(image, tag, path)
    img = input_docker.Image(image, tag, socket_path=socket)
    _fetch(img, d)
    d.write_bundle()


cli.add_command(docker_to_oci)


@click.command(deprecated=True)
@click.argument('image')
@click.argument('tag')
@click.argument('pattern')
@click.option('--socket', default='/var/run/docker.sock',
              help='Path to Docker socket')
@click.option('--regex', is_flag=True, default=False,
              help='Use regex pattern instead of glob pattern')
@click.option('--script-friendly', is_flag=True, default=False,
              help='Output in script-friendly format: image:tag:layer:path')
@click.pass_context
def search_layers_docker(ctx, image, tag, pattern, socket, regex,
                         script_friendly):
    """[DEPRECATED] Use: occystrap search docker://IMAGE:TAG PATTERN"""
    searcher = SearchFilter(
        None, pattern, use_regex=regex, image=image, tag=tag,
        script_friendly=script_friendly)
    img = input_docker.Image(image, tag, socket_path=socket)
    _fetch(img, searcher)


cli.add_command(search_layers_docker)


@click.command(deprecated=True)
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
    """[DEPRECATED] Use: occystrap search registry://HOST/IMAGE:TAG PATTERN"""
    searcher = SearchFilter(
        None, pattern, use_regex=regex, image=image, tag=tag,
        script_friendly=script_friendly)
    img = input_registry.Image(
        registry, image, tag, ctx.obj['OS'], ctx.obj['ARCHITECTURE'],
        ctx.obj['VARIANT'], secure=(not ctx.obj['INSECURE']),
        username=ctx.obj['USERNAME'], password=ctx.obj['PASSWORD'])
    _fetch(img, searcher)


cli.add_command(search_layers)


@click.command(deprecated=True)
@click.argument('tarfile')
@click.argument('pattern')
@click.option('--regex', is_flag=True, default=False,
              help='Use regex pattern instead of glob pattern')
@click.option('--script-friendly', is_flag=True, default=False,
              help='Output in script-friendly format: image:tag:layer:path')
@click.pass_context
def search_layers_tarfile(ctx, tarfile, pattern, regex, script_friendly):
    """[DEPRECATED] Use: occystrap search tar://PATH PATTERN"""
    img = input_tarfile.Image(tarfile)
    searcher = SearchFilter(
        None, pattern, use_regex=regex, image=img.image, tag=img.tag,
        script_friendly=script_friendly)
    _fetch(img, searcher)


cli.add_command(search_layers_tarfile)
