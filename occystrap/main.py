import json

import click
import logging
import os
from prettytable import PrettyTable
from shakenfist_utilities import logs
import sys

from occystrap import check
from occystrap import compression
from occystrap.progress import redirect_logging
from occystrap.inputs.base import ImageInputError
from occystrap.inputs import docker as input_docker
from occystrap.inputs import registry as input_registry
from occystrap.inputs import tarfile as input_tarfile
from occystrap.outputs import directory as output_directory
from occystrap.outputs import mounts as output_mounts
from occystrap.outputs import ocibundle as output_ocibundle
from occystrap.outputs import tarfile as output_tarfile
from occystrap.filters import SearchFilter, TimestampNormalizer
from occystrap.pipeline import PipelineBuilder, PipelineError
from occystrap import proxy as proxy_module
from occystrap import uri
from occystrap.util import format_size


LOG = logs.setup_console(__name__)


@click.group()
@click.option('--verbose', is_flag=True,
              help='Enable debug logging for occystrap')
@click.option('--debug', is_flag=True,
              help='Enable debug logging for all modules'
              ' (includes library output)')
@click.option('--os', default='linux')
@click.option('--architecture', default='amd64')
@click.option('--variant', default='')
@click.option('--username', default=None, envvar='OCCYSTRAP_USERNAME',
              help='Username for registry authentication')
@click.option('--password', default=None, envvar='OCCYSTRAP_PASSWORD',
              help='Password for registry authentication')
@click.option('--insecure', is_flag=True, default=False,
              help='Use HTTP instead of HTTPS for registry connections')
@click.option('--compression', default=None, envvar='OCCYSTRAP_COMPRESSION',
              type=click.Choice(['gzip', 'zstd']),
              help='Compression for registry output (default: gzip)')
@click.option('--parallel', '-j', default=4, type=int,
              envvar='OCCYSTRAP_PARALLEL',
              help='Number of parallel download/upload threads (default: 4)')
@click.option('--temp-dir', default=None, envvar='OCCYSTRAP_TEMP_DIR',
              help='Directory for temporary files (default: system temp)')
@click.option('--layer-cache', default=None, envvar='OCCYSTRAP_LAYER_CACHE',
              help='JSON file for cross-invocation layer caching')
@click.option('--output-format', '-O', default='text',
              type=click.Choice(['text', 'json']),
              help='Output format for info/check commands (default: text)')
@click.pass_context
def cli(ctx, verbose=None, debug=None, os=None,
        architecture=None, variant=None,
        username=None, password=None, insecure=None,
        compression=None, parallel=None, temp_dir=None,
        layer_cache=None, output_format=None):
    if debug:
        # Enable debug for all loggers (occystrap +
        # libraries like requests, urllib3, etc.)
        # Setting root level + handler levels is
        # sufficient since child loggers propagate.
        logging.root.setLevel(logging.DEBUG)
        for handler in logging.root.handlers:
            handler.setLevel(logging.DEBUG)
        LOG.setLevel(logging.DEBUG)
    elif verbose:
        # Enable debug for occystrap loggers only
        for name, obj in \
                logging.Logger.manager.loggerDict.items():
            if name.startswith('occystrap') \
                    and isinstance(obj, logging.Logger):
                obj.setLevel(logging.DEBUG)
                for handler in obj.handlers:
                    handler.setLevel(logging.DEBUG)

    if not ctx.obj:
        ctx.obj = {}
    ctx.obj['OS'] = os
    ctx.obj['ARCHITECTURE'] = architecture
    ctx.obj['VARIANT'] = variant
    ctx.obj['USERNAME'] = username
    ctx.obj['PASSWORD'] = password
    ctx.obj['INSECURE'] = insecure
    ctx.obj['COMPRESSION'] = compression
    ctx.obj['MAX_WORKERS'] = parallel
    ctx.obj['TEMP_DIR'] = temp_dir
    ctx.obj['LAYER_CACHE'] = layer_cache
    ctx.obj['OUTPUT_FORMAT'] = output_format


def _fetch(img, output):
    ordered = output.requires_ordered_layers
    with redirect_logging():
        for element in img.fetch(
                fetch_callback=output.fetch_callback,
                ordered=ordered):
            output.process_image_element(element)
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
      dockerpush://IMAGE:TAG       - Local Docker via push (fast)
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
      inspect:file=PATH            - Append layer metadata (JSONL)
      exclude:pattern=GLOB         - Exclude files from layers

    \b
    Examples:
      occystrap process registry://docker.io/library/busybox:latest tar://busybox.tar
      occystrap process docker://myimage:v1 dir://./extracted -f normalize-timestamps
      occystrap process tar://image.tar dir://out -f "search:pattern=*.conf"
      occystrap process docker://img:v1 registry://host/img:v1 -f "inspect:file=layers.jsonl"
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
      dockerpush://IMAGE:TAG       - Local Docker via push (fast)
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


def _build_info(input_source):
    """Build an info dict from an image input source.

    Collects metadata from the manifest (if available)
    and config (if available) without downloading layer
    blobs.

    Returns:
        Dict with image metadata fields.
    """
    manifest = input_source.get_manifest()
    config = input_source.get_config()

    info = {
        'image': input_source.image,
        'tag': input_source.tag,
    }

    # Manifest-derived fields
    if manifest:
        info['schema_version'] = manifest.get(
            'schemaVersion')
        info['media_type'] = manifest.get('mediaType')

        config_desc = manifest.get('config', {})
        info['config_digest'] = config_desc.get(
            'digest')
        info['config_size'] = config_desc.get('size')

        layers = manifest.get('layers', [])
        info['layer_count'] = len(layers)
        info['total_compressed_size'] = sum(
            ly.get('size', 0) for ly in layers)

        info['layers'] = []
        for i, layer in enumerate(layers):
            media_type = layer.get('mediaType', '')
            comp = \
                compression \
                .detect_compression_from_media_type(
                    media_type)
            layer_info = {
                'index': i,
                'digest': layer.get('digest'),
                'size': layer.get('size'),
                'media_type': media_type,
                'compression': comp,
            }
            info['layers'].append(layer_info)

    # Config-derived fields
    if config:
        info['architecture'] = config.get(
            'architecture', '')
        info['os'] = config.get('os', '')
        info['variant'] = config.get('variant', '')
        info['created'] = config.get('created', '')

        rootfs = config.get('rootfs', {})
        diff_ids = rootfs.get('diff_ids', [])
        info['diff_ids'] = diff_ids

        # Merge diff_ids into layer info
        if 'layers' in info:
            for i, layer in enumerate(info['layers']):
                if i < len(diff_ids):
                    layer['diff_id'] = diff_ids[i]
        else:
            # No manifest available; build layers
            # from diff_ids only
            info['layer_count'] = len(diff_ids)
            info['layers'] = [
                {'index': i, 'diff_id': d}
                for i, d in enumerate(diff_ids)
            ]

        history = config.get('history', [])
        info['history_count'] = len(history)
        info['empty_layer_count'] = sum(
            1 for h in history
            if h.get('empty_layer'))

        # Merge history created_by into layer info
        non_empty_idx = 0
        for h in history:
            if not h.get('empty_layer'):
                if non_empty_idx < len(
                        info.get('layers', [])):
                    info['layers'][
                        non_empty_idx][
                        'created_by'] = h.get(
                        'created_by', '')
                non_empty_idx += 1

        img_config = config.get('config') or {}
        info['labels'] = img_config.get(
            'Labels') or {}
        info['env'] = img_config.get('Env') or []
        info['entrypoint'] = img_config.get(
            'Entrypoint')
        info['cmd'] = img_config.get('Cmd')
        info['working_dir'] = img_config.get(
            'WorkingDir', '')
        info['exposed_ports'] = list(
            (img_config.get('ExposedPorts')
             or {}).keys())
        info['volumes'] = list(
            (img_config.get('Volumes')
             or {}).keys())

    return info


def _print_info_text(info):
    """Print image info in human-readable text format."""
    click.echo('Image:         %s:%s'
               % (info['image'], info['tag']))

    if 'media_type' in info:
        click.echo('Media type:    %s'
                   % info['media_type'])
    if 'schema_version' in info:
        click.echo('Schema:        v%s'
                   % info['schema_version'])
    if 'architecture' in info:
        arch = info['architecture']
        if info.get('variant'):
            arch += '/%s' % info['variant']
        click.echo('Platform:      %s/%s'
                   % (info.get('os', ''), arch))
    if 'created' in info:
        click.echo('Created:       %s'
                   % info['created'])
    if 'config_digest' in info:
        click.echo('Config digest: %s'
                   % info['config_digest'])
    if 'config_size' in info:
        click.echo('Config size:   %s'
                   % format_size(
                       info['config_size']))

    click.echo('')
    click.echo('Layers: %d' % info.get(
        'layer_count', 0))
    if 'total_compressed_size' in info:
        click.echo(
            'Total compressed size: %s'
            % format_size(
                info['total_compressed_size']))

    # Layer table
    layers = info.get('layers', [])
    if layers:
        click.echo('')
        table = PrettyTable()

        # Build columns based on available data
        has_digest = any(
            'digest' in ly for ly in layers)
        has_size = any(
            'size' in ly for ly in layers)
        has_diff_id = any(
            'diff_id' in ly for ly in layers)
        has_compression = any(
            'compression' in ly for ly in layers)
        has_created_by = any(
            'created_by' in ly for ly in layers)

        fields = ['#']
        if has_digest:
            fields.append('Compressed Digest')
        if has_diff_id:
            fields.append('DiffID')
        if has_size:
            fields.append('Size')
        if has_compression:
            fields.append('Compression')
        if has_created_by:
            fields.append('Created By')
        table.field_names = fields

        if has_created_by:
            table.max_width['Created By'] = 50

        table.align = 'l'

        for layer in layers:
            row = [layer['index']]
            if has_digest:
                d = layer.get('digest', '')
                # Truncate for display
                if len(d) > 25:
                    d = d[:25] + '...'
                row.append(d)
            if has_diff_id:
                d = layer.get('diff_id', '')
                if len(d) > 25:
                    d = d[:25] + '...'
                row.append(d)
            if has_size:
                row.append(format_size(
                    layer.get('size')))
            if has_compression:
                row.append(
                    layer.get('compression', ''))
            if has_created_by:
                cb = layer.get('created_by', '')
                row.append(cb)
            table.add_row(row)

        click.echo(table)

    # History summary
    if 'history_count' in info:
        click.echo('')
        click.echo(
            'History: %d entries (%d empty)'
            % (info['history_count'],
               info.get('empty_layer_count', 0)))

    # Container config
    has_config = any(
        k in info for k in [
            'entrypoint', 'cmd', 'working_dir',
            'env', 'labels', 'exposed_ports',
            'volumes'])
    if has_config:
        click.echo('')
        click.echo('Container Config:')
        if info.get('entrypoint'):
            click.echo('  Entrypoint: %s'
                       % info['entrypoint'])
        if info.get('cmd'):
            click.echo('  Cmd:        %s'
                       % info['cmd'])
        if info.get('working_dir'):
            click.echo('  WorkingDir: %s'
                       % info['working_dir'])
        if info.get('exposed_ports'):
            click.echo('  Ports:      %s'
                       % ', '.join(
                           info['exposed_ports']))
        if info.get('volumes'):
            click.echo('  Volumes:    %s'
                       % ', '.join(
                           info['volumes']))
        if info.get('env'):
            click.echo('  Environment:')
            for env in info['env']:
                click.echo('    %s' % env)
        if info.get('labels'):
            click.echo('  Labels:')
            for k, v in info['labels'].items():
                click.echo('    %s=%s' % (k, v))


@click.command('info')
@click.argument('source')
@click.pass_context
def info_cmd(ctx, source):
    """Display information about a container image.

    SOURCE is a URI specifying where to read the image
    from:

    \b
      registry://HOST/IMAGE:TAG  - Docker/OCI registry
      docker://IMAGE:TAG         - Local Docker daemon
      tar://PATH                 - Docker-save tarball

    \b
    Examples:
      occystrap info registry://docker.io/library/busybox:latest
      occystrap info docker://myimage:v1
      occystrap info tar://image.tar
      occystrap -O json info registry://ghcr.io/org/app:v2
    """
    try:
        builder = PipelineBuilder(ctx)
        source_spec = uri.parse_uri(source)
        input_source = builder.build_input(source_spec)

        info = _build_info(input_source)

        output_format = ctx.obj.get(
            'OUTPUT_FORMAT', 'text')
        if output_format == 'json':
            click.echo(json.dumps(
                info, indent=2))
        else:
            _print_info_text(info)

    except (PipelineError, uri.URIParseError,
            ImageInputError) as e:
        click.echo('Error: %s' % e, err=True)
        sys.exit(1)


cli.add_command(info_cmd)


def _print_check_text(results, fast=False):
    """Print check results in human-readable format.

    Groups results by severity: errors first, then
    warnings, then informational messages.
    """
    errors = [
        r for r in results.results
        if r['severity'] == check.CHECK_ERROR]
    warnings = [
        r for r in results.results
        if r['severity'] == check.CHECK_WARNING]
    infos = [
        r for r in results.results
        if r['severity'] == check.CHECK_INFO]

    if errors:
        click.echo('Errors:')
        for r in errors:
            click.echo(
                '  [%s] %s'
                % (r['check'], r['message']))
        click.echo('')

    if warnings:
        click.echo('Warnings:')
        for r in warnings:
            click.echo(
                '  [%s] %s'
                % (r['check'], r['message']))
        click.echo('')

    if infos:
        click.echo('Info:')
        for r in infos:
            click.echo(
                '  [%s] %s'
                % (r['check'], r['message']))
        click.echo('')

    # Summary
    mode = 'fast (metadata only)' if fast else 'full'
    click.echo(
        'Check complete (%s): %d error(s), '
        '%d warning(s)'
        % (mode, results.error_count,
           results.warning_count))


@click.command('check')
@click.argument('source')
@click.option('--fast', is_flag=True, default=False,
              help='Skip layer download, check '
              'metadata only')
@click.pass_context
def check_cmd(ctx, source, fast):
    """Check validity of a container image.

    Validates structural integrity, history
    consistency, compression compatibility, and
    filesystem correctness.

    SOURCE is a URI specifying where to read the
    image from:

    \b
      registry://HOST/IMAGE:TAG  - Docker/OCI registry
      docker://IMAGE:TAG         - Local Docker daemon
      tar://PATH                 - Docker-save tarball

    Use --fast to skip layer downloads and only check
    metadata consistency (manifest and config).

    Exit code is non-zero if any errors are found.

    \b
    Examples:
      occystrap check registry://docker.io/library/busybox:latest
      occystrap check --fast docker://myimage:v1
      occystrap check tar://image.tar
      occystrap -O json check registry://ghcr.io/org/app:v2
    """
    try:
        builder = PipelineBuilder(ctx)
        source_spec = uri.parse_uri(source)
        input_source = builder.build_input(
            source_spec)

        manifest = input_source.get_manifest()
        config = input_source.get_config()

        results = check.CheckResults()
        check.check_metadata(
            manifest, config, results)

        if not fast:
            check.check_layers(
                input_source, manifest,
                config, results)

        output_format = ctx.obj.get(
            'OUTPUT_FORMAT', 'text')
        if output_format == 'json':
            output = {
                'image': input_source.image,
                'tag': input_source.tag,
                'mode': 'fast' if fast else 'full',
                'errors': results.error_count,
                'warnings': results.warning_count,
                'results': results.results,
            }
            click.echo(json.dumps(
                output, indent=2))
        else:
            _print_check_text(results, fast=fast)

        if results.has_errors:
            sys.exit(1)

    except (PipelineError, uri.URIParseError,
            ImageInputError) as e:
        click.echo('Error: %s' % e, err=True)
        sys.exit(1)


cli.add_command(check_cmd)


@click.command('proxy')
@click.option('--listen', '-l', default='127.0.0.1:5050',
              help='Address to listen on (default: 127.0.0.1:5050)')
@click.option('--downstream', '-d', required=True,
              help='Downstream registry host'
              ' (e.g., ghcr.io, registry.example.com)')
@click.option('--filter', '-f', 'filters', multiple=True,
              help='Apply filter (can be specified'
              ' multiple times)')
@click.option('--concurrency', '-c', default=4, type=int,
              help='Max concurrent image processing'
              ' (default: 4)')
@click.option('--upstream', '-u', default=None,
              help='Upstream registry for pull-through'
              ' (e.g., docker.io,'
              ' user:pass@registry.example.com)')
@click.option('--upstream-username', default=None,
              envvar='OCCYSTRAP_UPSTREAM_USERNAME',
              help='Username for upstream registry'
              ' (overrides user:pass@ in --upstream)')
@click.option('--upstream-password', default=None,
              envvar='OCCYSTRAP_UPSTREAM_PASSWORD',
              help='Password for upstream registry'
              ' (overrides user:pass@ in --upstream)')
@click.pass_context
def proxy_cmd(ctx, listen, downstream, filters,
              concurrency, upstream, upstream_username,
              upstream_password):
    """Run a filtering registry proxy.

    Starts a Docker Registry V2 server that receives
    pushes, applies filters, and forwards images to a
    downstream registry. Runs until interrupted.

    \b
    When --upstream is specified, the proxy also serves
    pull requests. On pull, it checks the downstream
    registry (cache). On cache miss, it fetches from
    upstream, applies filters, pushes to downstream,
    and serves the result.

    \b
    The proxy listens on localhost by default, which is
    trusted by Docker without TLS configuration.

    \b
    Filters (use -f, can chain multiple):
      normalize-timestamps         - Normalize timestamps
      normalize-timestamps:ts=N    - Use specific timestamp
      exclude:pattern=GLOB         - Exclude files

    \b
    Examples:
      occystrap proxy -d ghcr.io/myorg
      occystrap proxy -d ghcr.io/myorg -u docker.io
      occystrap proxy -l 127.0.0.1:5050 -d ghcr.io/myorg -f normalize-timestamps
      occystrap proxy -d registry.example.com -f "exclude:pattern=/tmp/*"

    \b
    Usage with kolla-build:
      occystrap proxy -d ghcr.io/myorg --layer-cache /tmp/cache.json -f normalize-timestamps &
      kolla-build --registry 127.0.0.1:5050 --push ...
    """
    # Parse listen address
    if ':' in listen:
        host, port_str = listen.rsplit(':', 1)
        try:
            port = int(port_str)
        except ValueError:
            click.echo(
                'Error: invalid port: %s' % port_str,
                err=True)
            sys.exit(1)
    else:
        host = listen
        port = 5050

    layer_cache_path = ctx.obj.get('LAYER_CACHE')

    # Parse upstream credentials if embedded
    upstream_host = upstream
    upstream_user = None
    upstream_pass = None
    if upstream and '@' in upstream:
        creds, upstream_host = upstream.rsplit('@', 1)
        if ':' in creds:
            upstream_user, upstream_pass = (
                creds.split(':', 1))

    # Explicit options override embedded credentials
    if upstream_username is not None:
        upstream_user = upstream_username
    if upstream_password is not None:
        upstream_pass = upstream_password

    try:
        proxy_module.run_proxy(
            host, port, downstream,
            filter_strs=list(filters),
            layer_cache_path=layer_cache_path,
            ctx=ctx,
            max_concurrent=concurrency,
            upstream_uri=upstream_host,
            upstream_username=upstream_user,
            upstream_password=upstream_pass)
    except Exception as e:
        click.echo('Error: %s' % e, err=True)
        sys.exit(1)


cli.add_command(proxy_cmd)


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
    for element in d.fetch():
        tar.process_image_element(element)
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
