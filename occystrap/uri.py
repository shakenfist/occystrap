"""URI parsing for occystrap pipeline specification.

This module provides URI-style parsing for input sources, output destinations,
and filter specifications.

URI formats:
    Input:
        registry://[user:pass@]host/image:tag[?arch=X&os=Y&variant=Z]
        docker://image:tag[?socket=/path/to/socket]
        tar:///path/to/file.tar
        file:///path/to/file.tar  (alias for tar)

    Output:
        tar:///path/to/output.tar
        dir:///path/to/directory[?unique_names=true&expand=true]
        directory:///path/...  (alias for dir)
        oci:///path/to/bundle
        mounts:///path/to/directory

    Filter specs:
        filter-name
        filter-name:option=value
        filter-name:opt1=val1,opt2=val2
"""

from collections import namedtuple
from urllib.parse import urlparse, parse_qs, unquote


# Named tuples for parsed specifications
URISpec = namedtuple('URISpec', ['scheme', 'host', 'path', 'options'])
FilterSpec = namedtuple('FilterSpec', ['name', 'options'])


# Scheme classifications
INPUT_SCHEMES = {'registry', 'docker', 'tar', 'file'}
OUTPUT_SCHEMES = {'tar', 'dir', 'directory', 'oci', 'mounts'}

# Scheme aliases
SCHEME_ALIASES = {
    'file': 'tar',
    'directory': 'dir',
}


class URIParseError(Exception):
    """Raised when a URI cannot be parsed."""
    pass


def parse_uri(uri_string):
    """Parse a URI string into components.

    Args:
        uri_string: A URI like 'registry://docker.io/library/busybox:latest'

    Returns:
        URISpec(scheme, host, path, options)

    Raises:
        URIParseError: If the URI is malformed.
    """
    # Handle URIs without :// (e.g., 'tar:foo.tar' -> 'tar://foo.tar')
    if '://' not in uri_string and ':' in uri_string:
        scheme, rest = uri_string.split(':', 1)
        if not rest.startswith('//'):
            uri_string = '%s://%s' % (scheme, rest)

    parsed = urlparse(uri_string)

    if not parsed.scheme:
        raise URIParseError('Missing scheme in URI: %s' % uri_string)

    scheme = parsed.scheme.lower()
    scheme = SCHEME_ALIASES.get(scheme, scheme)

    # Parse query string into options dict
    options = {}
    if parsed.query:
        qs = parse_qs(parsed.query)
        for key, values in qs.items():
            # Convert single-value lists to scalars
            if len(values) == 1:
                value = values[0]
                # Convert string booleans
                if value.lower() in ('true', 'yes', '1'):
                    options[key] = True
                elif value.lower() in ('false', 'no', '0'):
                    options[key] = False
                else:
                    # Try to convert to int
                    try:
                        options[key] = int(value)
                    except ValueError:
                        options[key] = value
            else:
                options[key] = values

    # Build the path, handling different schemes
    host = parsed.netloc
    path = unquote(parsed.path)

    # For file-based schemes, the host might be part of the path
    if scheme in ('tar', 'dir', 'oci', 'mounts'):
        if host and not path:
            # tar://foo.tar -> host='foo.tar', path=''
            path = host
            host = ''
        elif host:
            # tar://localhost/path/to/file -> path='/path/to/file'
            # But we want to preserve absolute paths
            if host != 'localhost':
                path = host + path
            host = ''

    return URISpec(scheme=scheme, host=host, path=path, options=options)


def parse_filter(filter_string):
    """Parse a filter specification string.

    Args:
        filter_string: A filter spec like 'normalize-timestamps:timestamp=0'

    Returns:
        FilterSpec(name, options)

    Raises:
        URIParseError: If the filter spec is malformed.
    """
    if not filter_string:
        raise URIParseError('Empty filter specification')

    # Split on first colon
    if ':' in filter_string:
        name, opts_string = filter_string.split(':', 1)
        options = {}

        # Parse comma-separated key=value pairs
        for pair in opts_string.split(','):
            pair = pair.strip()
            if not pair:
                continue
            if '=' not in pair:
                raise URIParseError(
                    'Invalid filter option (missing =): %s' % pair)
            key, value = pair.split('=', 1)
            key = key.strip()
            value = value.strip()

            # Convert string booleans
            if value.lower() in ('true', 'yes', '1'):
                options[key] = True
            elif value.lower() in ('false', 'no', '0'):
                options[key] = False
            else:
                # Try to convert to int
                try:
                    options[key] = int(value)
                except ValueError:
                    options[key] = value
    else:
        name = filter_string
        options = {}

    return FilterSpec(name=name.strip(), options=options)


def parse_registry_uri(uri_spec):
    """Parse registry URI into (registry, image, tag) tuple.

    Handles formats like:
        registry://docker.io/library/busybox:latest
        registry://ghcr.io/owner/repo:v1.0

    Returns:
        Tuple of (registry_host, image_path, tag)
    """
    if uri_spec.scheme != 'registry':
        raise URIParseError('Expected registry:// URI, got %s' % uri_spec.scheme)

    host = uri_spec.host
    path = uri_spec.path.lstrip('/')

    # Split off tag
    if ':' in path:
        # Find the last colon that's part of the tag (not in the image name)
        # e.g., 'library/busybox:latest' or 'my-image:v1.0'
        last_colon = path.rfind(':')
        image = path[:last_colon]
        tag = path[last_colon + 1:]
    else:
        image = path
        tag = 'latest'

    return (host, image, tag)


def parse_docker_uri(uri_spec):
    """Parse docker URI into (image, tag, socket) tuple.

    Handles formats like:
        docker://busybox:latest
        docker://busybox:latest?socket=/run/podman/podman.sock
        docker://library/busybox:v1

    Returns:
        Tuple of (image, tag, socket_path)
    """
    if uri_spec.scheme != 'docker':
        raise URIParseError('Expected docker:// URI, got %s' % uri_spec.scheme)

    # The image:tag is in the host+path
    image_tag = uri_spec.host
    if uri_spec.path:
        image_tag += uri_spec.path

    # Split off tag
    if ':' in image_tag:
        last_colon = image_tag.rfind(':')
        image = image_tag[:last_colon]
        tag = image_tag[last_colon + 1:]
    else:
        image = image_tag
        tag = 'latest'

    socket = uri_spec.options.get('socket', '/var/run/docker.sock')

    return (image, tag, socket)
