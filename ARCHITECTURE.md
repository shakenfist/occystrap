# Architecture

Occystrap follows an input/output pipeline pattern for processing container
images.

## Directory Structure

```
occystrap/
    __init__.py
    main.py              # CLI entry point (Click-based)
    constants.py         # Element type constants (CONFIG_FILE, IMAGE_LAYER)
    common.py            # Shared utilities
    util.py              # Additional utilities
    docker_extract.py    # Layer extraction utilities
    search.py            # Layer file search functionality
    inputs/              # Input source modules
        __init__.py
        registry.py      # Fetches images from Docker/OCI registries
        tarfile.py       # Reads from docker-save tarballs
    output_tarfile.py    # Creates docker-loadable tarballs
    output_directory.py  # Extracts to directory with deduplication
    output_ocibundle.py  # Creates OCI runtime bundles
    output_mounts.py     # Creates overlay mount-based extraction
```

## Pipeline Pattern

### Input Sources

Input sources are responsible for yielding image elements (config files and
layers) from various sources:

- `inputs/registry.py` - Fetches images from Docker/OCI registries via HTTP API
- `inputs/tarfile.py` - Reads from existing docker-save tarballs

### Output Writers

All output writers implement a common interface:

- `fetch_callback(digest)` - Returns whether a layer should be fetched
- `process_image_element(type, name, data)` - Handles CONFIG_FILE or IMAGE_LAYER
- `finalize()` - Writes manifest and completes output

Output writers:
- `output_tarfile.py` - Creates docker-loadable tarballs (v1.2 format)
- `output_directory.py` - Extracts to directory with optional layer deduplication
- `output_ocibundle.py` - Creates OCI runtime bundles for runc
- `output_mounts.py` - Creates overlay mount-based extraction

### Element Types

Defined in `constants.py`:
- `CONFIG_FILE` - Image configuration JSON
- `IMAGE_LAYER` - Tarball containing filesystem layer

## Key Concepts

### Whiteout Files

OCI layers use special files to mark deletions:
- `.wh.<filename>` - Marks a file as deleted
- `.wh..wh..opq` - Marks directory as opaque (contents replaced)

Processed in `output_directory.py` when `--expand` is used.

### Unique Names Mode

`--use-unique-names` enables storing multiple images in one directory by
prefixing manifest files with image/tag names. A `catalog.json` tracks which
layers belong to which images.

### Timestamp Normalization

`--normalize-timestamps` rewrites layer tar mtimes for reproducible builds,
recalculating layer SHAs.

## Data Flow

```
Registry/Tarball -> Input Source -> [element generator] -> Output Writer -> Files
                                         |
                                    fetch_callback
                                   (skip/include)
```
