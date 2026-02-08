"""Pipeline builder for occystrap.

This module provides a PipelineBuilder class that constructs input -> filter
chain -> output pipelines from URI specifications.
"""

import os

from occystrap.inputs import docker as input_docker
from occystrap.inputs import registry as input_registry
from occystrap.inputs import tarfile as input_tarfile
from occystrap.outputs import directory as output_directory
from occystrap.outputs import docker as output_docker
from occystrap.outputs import mounts as output_mounts
from occystrap.outputs import ocibundle as output_ocibundle
from occystrap.outputs import registry as output_registry
from occystrap.outputs import tarfile as output_tarfile
from occystrap.filters import (
    ExcludeFilter, InspectFilter, TimestampNormalizer, SearchFilter
)
from occystrap import uri


class PipelineError(Exception):
    """Raised when a pipeline cannot be built."""
    pass


class PipelineBuilder:
    """Builds input -> filter chain -> output pipelines from URIs."""

    def __init__(self, ctx=None):
        """Initialize the pipeline builder.

        Args:
            ctx: Click context object containing global options like
                OS, ARCHITECTURE, VARIANT, USERNAME, PASSWORD, INSECURE.
                Can be None for defaults.
        """
        self.ctx = ctx
        self._ctx_obj = ctx.obj if ctx and ctx.obj else {}

    def _get_ctx(self, key, default=None):
        """Get a value from the context object."""
        return self._ctx_obj.get(key, default)

    def build_input(self, uri_spec):
        """Create an ImageInput from a URI spec.

        Args:
            uri_spec: A URISpec from uri.parse_uri()

        Returns:
            An ImageInput instance.

        Raises:
            PipelineError: If the input cannot be created.
        """
        if uri_spec.scheme == 'registry':
            host, image, tag = uri.parse_registry_uri(uri_spec)

            # Get options from URI or context
            os_name = uri_spec.options.get('os', self._get_ctx('OS', 'linux'))
            arch = uri_spec.options.get(
                'arch', uri_spec.options.get(
                    'architecture', self._get_ctx('ARCHITECTURE', 'amd64')))
            variant = uri_spec.options.get(
                'variant', self._get_ctx('VARIANT', ''))
            username = uri_spec.options.get(
                'username', self._get_ctx('USERNAME'))
            password = uri_spec.options.get(
                'password', self._get_ctx('PASSWORD'))
            insecure = uri_spec.options.get(
                'insecure', self._get_ctx('INSECURE', False))

            max_workers = uri_spec.options.get(
                'max_workers', self._get_ctx('MAX_WORKERS', 4))
            temp_dir = self._get_ctx('TEMP_DIR')

            return input_registry.Image(
                host, image, tag,
                os=os_name,
                architecture=arch,
                variant=variant,
                secure=(not insecure),
                username=username,
                password=password,
                max_workers=max_workers,
                temp_dir=temp_dir)

        elif uri_spec.scheme == 'docker':
            image, tag, socket = uri.parse_docker_uri(uri_spec)
            temp_dir = self._get_ctx('TEMP_DIR')
            return input_docker.Image(
                image, tag, socket_path=socket, temp_dir=temp_dir)

        elif uri_spec.scheme == 'tar':
            path = uri_spec.path
            if not path:
                raise PipelineError('tar:// URI requires a path')
            return input_tarfile.Image(path)

        else:
            raise PipelineError('Unknown input scheme: %s' % uri_spec.scheme)

    def build_output(self, uri_spec, image, tag):
        """Create an ImageOutput from a URI spec.

        Args:
            uri_spec: A URISpec from uri.parse_uri()
            image: Image name (from input source)
            tag: Image tag (from input source)

        Returns:
            An ImageOutput instance.

        Raises:
            PipelineError: If the output cannot be created.
        """
        if uri_spec.scheme == 'tar':
            path = uri_spec.path
            if not path:
                raise PipelineError('tar:// output URI requires a path')
            return output_tarfile.TarWriter(image, tag, path)

        elif uri_spec.scheme == 'dir':
            path = uri_spec.path
            if not path:
                raise PipelineError('dir:// URI requires a path')
            unique_names = uri_spec.options.get('unique_names', False)
            expand = uri_spec.options.get('expand', False)
            return output_directory.DirWriter(
                image, tag, path,
                unique_names=unique_names,
                expand=expand)

        elif uri_spec.scheme == 'oci':
            path = uri_spec.path
            if not path:
                raise PipelineError('oci:// URI requires a path')
            return output_ocibundle.OCIBundleWriter(image, tag, path)

        elif uri_spec.scheme == 'mounts':
            path = uri_spec.path
            if not path:
                raise PipelineError('mounts:// URI requires a path')

            # Check for required OS features
            if not hasattr(os, 'setxattr'):
                raise PipelineError(
                    'mounts:// output requires setxattr support')
            if not hasattr(os, 'mknod'):
                raise PipelineError(
                    'mounts:// output requires mknod support')

            return output_mounts.MountWriter(image, tag, path)

        elif uri_spec.scheme == 'docker':
            _, _, socket = uri.parse_docker_uri(uri_spec)
            temp_dir = self._get_ctx('TEMP_DIR')
            return output_docker.DockerWriter(
                image, tag, socket_path=socket,
                temp_dir=temp_dir)

        elif uri_spec.scheme == 'registry':
            host, dest_image, dest_tag = uri.parse_registry_uri(uri_spec)
            username = uri_spec.options.get(
                'username', self._get_ctx('USERNAME'))
            password = uri_spec.options.get(
                'password', self._get_ctx('PASSWORD'))
            insecure = uri_spec.options.get(
                'insecure', self._get_ctx('INSECURE', False))
            compression_type = uri_spec.options.get(
                'compression', self._get_ctx('COMPRESSION'))
            max_workers = uri_spec.options.get(
                'max_workers', self._get_ctx('MAX_WORKERS', 4))
            return output_registry.RegistryWriter(
                host, dest_image, dest_tag,
                secure=(not insecure),
                username=username,
                password=password,
                compression_type=compression_type,
                max_workers=max_workers)

        else:
            raise PipelineError('Unknown output scheme: %s' % uri_spec.scheme)

    def build_filter(self, filter_spec, wrapped_output, image=None, tag=None):
        """Wrap an output with a filter.

        Args:
            filter_spec: A FilterSpec from uri.parse_filter()
            wrapped_output: The ImageOutput to wrap
            image: Image name (for search filter output)
            tag: Image tag (for search filter output)

        Returns:
            An ImageFilter wrapping the output.

        Raises:
            PipelineError: If the filter cannot be created.
        """
        name = filter_spec.name.lower().replace('_', '-')

        temp_dir = self._get_ctx('TEMP_DIR')

        if name == 'normalize-timestamps':
            timestamp = filter_spec.options.get(
                'timestamp', filter_spec.options.get('ts', 0))
            return TimestampNormalizer(
                wrapped_output, timestamp=timestamp,
                temp_dir=temp_dir)

        elif name == 'search':
            pattern = filter_spec.options.get('pattern')
            if not pattern:
                raise PipelineError(
                    'search filter requires pattern option')
            use_regex = filter_spec.options.get('regex', False)
            script_friendly = filter_spec.options.get('script_friendly',
                                                      filter_spec.options.get(
                                                          'script-friendly',
                                                          False))
            return SearchFilter(
                wrapped_output,
                pattern=pattern,
                use_regex=use_regex,
                image=image,
                tag=tag,
                script_friendly=script_friendly)

        elif name == 'exclude':
            pattern_str = filter_spec.options.get('pattern')
            if not pattern_str:
                raise PipelineError(
                    'exclude filter requires pattern option')
            patterns = [p.strip() for p in pattern_str.split(',')]
            return ExcludeFilter(
                wrapped_output, patterns=patterns,
                temp_dir=temp_dir)

        elif name == 'inspect':
            output_file = filter_spec.options.get('file')
            if not output_file:
                raise PipelineError(
                    'inspect filter requires file option')
            return InspectFilter(
                wrapped_output,
                output_file=output_file,
                image=image,
                tag=tag)

        else:
            raise PipelineError('Unknown filter: %s' % filter_spec.name)

    def build_pipeline(self, source_uri_str, dest_uri_str, filter_strs=None):
        """Build complete pipeline from URI strings.

        Args:
            source_uri_str: Input URI string
            dest_uri_str: Output URI string
            filter_strs: List of filter specification strings

        Returns:
            Tuple of (input_source, output_chain)

        Raises:
            PipelineError: If the pipeline cannot be built.
            uri.URIParseError: If a URI cannot be parsed.
        """
        if filter_strs is None:
            filter_strs = []

        # Parse URIs
        source_spec = uri.parse_uri(source_uri_str)
        dest_spec = uri.parse_uri(dest_uri_str)
        filter_specs = [uri.parse_filter(f) for f in filter_strs]

        # Build input
        input_source = self.build_input(source_spec)

        # Build output
        output = self.build_output(
            dest_spec, input_source.image, input_source.tag)

        # Wrap with filters (in reverse order so first filter is outermost)
        for filter_spec in reversed(filter_specs):
            output = self.build_filter(
                filter_spec, output,
                image=input_source.image,
                tag=input_source.tag)

        return input_source, output

    def build_search_pipeline(self, source_uri_str, pattern, use_regex=False,
                              script_friendly=False):
        """Build a search-only pipeline (no output destination).

        Args:
            source_uri_str: Input URI string
            pattern: Search pattern
            use_regex: If True, treat pattern as regex
            script_friendly: If True, output in machine-parseable format

        Returns:
            Tuple of (input_source, search_filter)
        """
        source_spec = uri.parse_uri(source_uri_str)
        input_source = self.build_input(source_spec)

        # Create search filter with no wrapped output
        searcher = SearchFilter(
            None,  # No wrapped output
            pattern=pattern,
            use_regex=use_regex,
            image=input_source.image,
            tag=input_source.tag,
            script_friendly=script_friendly)

        return input_source, searcher
