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

## Testing

Tests are located in `deploy/occystrap_ci/tests/`. Run with pytest.

## Common Tasks

- **Search for files in layers**: Use `SearchFilter` as reference
- **Modify layer contents**: Use `TimestampNormalizer` or `ExcludeFilter` as
  reference (they rewrite tarballs)
- **Passthrough filter**: Check element type, process if needed, always call
  `self._wrapped.process_image_element()` to pass data through
