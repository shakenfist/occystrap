# Agents Guide

This document provides guidance for AI agents working on the occystrap codebase.

## Project Overview

Occystrap is a Docker/OCI container image processing tool that follows an
input -> filter -> output pipeline pattern. It can fetch images from registries,
local Docker daemons, or tarballs, process them through filters, and write to
various output formats.

## Key Patterns

### Adding a New Filter

1. Create a new file in `occystrap/filters/` (e.g., `myfilter.py`)
2. Subclass `ImageFilter` from `occystrap.filters.base`
3. Implement `process_image_element(element_type, name, data)`
4. Export from `occystrap/filters/__init__.py`
5. Register in `PipelineBuilder.build_filter()` in `occystrap/pipeline.py`

Template for a filter that modifies layers:

```python
from occystrap import constants
from occystrap.filters.base import ImageFilter

class MyFilter(ImageFilter):
    def __init__(self, wrapped_output, option=None):
        super().__init__(wrapped_output)
        self.option = option

    def process_image_element(self, element_type, name, data):
        if element_type == constants.IMAGE_LAYER and data is not None:
            # Process the layer, return modified data and new name
            new_data, new_name = self._process_layer(data)
            try:
                self._wrapped.process_image_element(
                    element_type, new_name, new_data)
            finally:
                # Clean up temporary files
                pass
        else:
            self._wrapped.process_image_element(element_type, name, data)
```

### Adding a New Input Source

1. Create a new file in `occystrap/inputs/`
2. Subclass `ImageInput` from `occystrap.inputs.base`
3. Implement `image`, `tag` properties and `fetch()` method
4. Register in `PipelineBuilder.build_input()` in `occystrap/pipeline.py`

### Adding a New Output Writer

1. Create a new file in `occystrap/outputs/`
2. Subclass `ImageOutput` from `occystrap.outputs.base`
3. Implement `fetch_callback()`, `process_image_element()`, `finalize()`
4. Register in `PipelineBuilder.build_output()` in `occystrap/pipeline.py`
5. Add the scheme to `OUTPUT_SCHEMES` in `occystrap/uri.py`

## Build System

The project uses `pyproject.toml` with `setuptools` and `setuptools_scm`
for building and versioning. Versions are derived from git tags. There is
no `setup.py` or `setup.cfg`. Dependencies are declared in `pyproject.toml`
under `[project.dependencies]` and `[project.optional-dependencies.test]`.

## Testing

- **Unit tests**: Located in `occystrap/tests/`. Run with `tox -epy3`.
- **Functional tests**: Located in `deploy/occystrap_ci/tests/`. Run in CI.

### Pre-commit Hooks

The project uses pre-commit hooks for `tox -eflake8` (linting) and `tox -epy3`
(unit tests). Install with `pre-commit install`.

## Common Tasks

- **Search for files in layers**: Use `SearchFilter` as reference
- **Modify layer contents**: Use `TimestampNormalizer` or `ExcludeFilter` as
  reference (they rewrite tarballs)
- **Record layer metadata**: Use `InspectFilter` as reference (accumulates
  state across elements and writes output in `finalize()`)
- **Passthrough filter**: Check element type, process if needed, always call
  `self._wrapped.process_image_element()` to pass data through
- **Write to Docker daemon**: Use `DockerWriter` as reference (builds tarball
  in memory and posts via API)
- **Push to registry**: Use `RegistryWriter` as reference (uploads blobs and
  manifest via Docker Registry HTTP API V2)
- **Handle layer compression**: Use `compression.py` module for detecting and
  handling gzip/zstd compressed layers. Media type constants are in `constants.py`.
- **Add new compression format**: Extend `compression.py` with detection magic,
  `StreamingDecompressor`/`StreamingCompressor` classes, and media type mapping

## CI/CD Automation Tools

The `tools/` directory contains scripts for automated PR workflows:

- **review-pr-with-claude.sh**: Generates structured JSON code reviews using
  Claude Code
- **render-review.py**: Converts review JSON to formatted markdown
- **create-review-issues.py**: Creates GitHub issues for actionable review items
- **address-comments-with-claude.sh**: Processes review items and creates
  commits for fixes

These scripts are used by GitHub Actions workflows in `.github/workflows/`:

- `pr-retest.yml` - Re-run tests via `@shakenfist-bot please retest`
- `pr-fix-tests.yml` - Fix test failures via `@shakenfist-bot please attempt to fix`
- `pr-re-review.yml` - Re-review PR via `@shakenfist-bot please re-review`
- `pr-address-comments.yml` - Address review comments via
  `@shakenfist-bot please address comments`
