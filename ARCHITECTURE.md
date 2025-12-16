# Architecture

Occystrap follows an input -> filter -> output pipeline pattern for processing
container images.

## Directory Structure

```
occystrap/
    __init__.py
    main.py              # CLI entry point (Click-based)
    constants.py         # Element type constants (CONFIG_FILE, IMAGE_LAYER)
    common.py            # Shared utilities
    util.py              # Additional utilities
    uri.py               # URI parsing for pipeline specification
    pipeline.py          # Pipeline builder from URIs
    docker_extract.py    # Layer extraction utilities
    inputs/              # Input source modules
        __init__.py
        base.py          # ImageInput abstract base class
        docker.py        # Fetches images from local Docker daemon
        registry.py      # Fetches images from Docker/OCI registries
        tarfile.py       # Reads from docker-save tarballs
    filters/             # Filter modules (transform/inspect pipeline)
        __init__.py
        base.py          # ImageFilter abstract base class
        normalize_timestamps.py  # Timestamp normalization for reproducible builds
        search.py        # Search for files matching patterns
    outputs/             # Output writer modules
        __init__.py
        base.py          # ImageOutput abstract base class
        tarfile.py       # Creates docker-loadable tarballs
        directory.py     # Extracts to directory with deduplication
        ocibundle.py     # Creates OCI runtime bundles
        mounts.py        # Creates overlay mount-based extraction
```

## Pipeline Pattern

The pipeline follows a decorator pattern where filters wrap outputs:

```
Input Source -> Filter Chain -> Output Writer -> Files
     |              |                |
   fetch()    process_image_element()  finalize()
```

### Input Sources

All input sources inherit from the `ImageInput` abstract base class defined in
`inputs/base.py`. This ABC defines the interface:

- `image` (property) - Returns the image name
- `tag` (property) - Returns the image tag
- `fetch(fetch_callback)` - Yields image elements (config files and layers)

Input source implementations:
- `inputs/docker.py` - Fetches images from local Docker daemon via Unix socket
- `inputs/registry.py` - Fetches images from Docker/OCI registries via HTTP API
- `inputs/tarfile.py` - Reads from existing docker-save tarballs

### Filters

Filters inherit from `ImageFilter` (in `filters/base.py`) which itself inherits
from `ImageOutput`. This allows filters to be chained together using the
decorator pattern. Each filter wraps another output (or filter) and can:

- Transform element data (e.g., normalize timestamps)
- Transform element names (e.g., recalculate hashes)
- Inspect elements without modification (e.g., search)
- Skip elements entirely
- Accumulate state across elements

Filter implementations:
- `filters/normalize_timestamps.py` - Normalizes layer timestamps for
  reproducible builds, recalculating layer SHAs
- `filters/search.py` - Searches layers for files matching glob or regex
  patterns, can operate standalone or as passthrough

### Output Writers

All output writers inherit from the `ImageOutput` abstract base class defined in
`outputs/base.py`. This ABC defines the interface:

- `fetch_callback(digest)` - Returns whether a layer should be fetched
- `process_image_element(type, name, data)` - Handles CONFIG_FILE or IMAGE_LAYER
- `finalize()` - Writes manifest and completes output

Output writer implementations:
- `outputs/tarfile.py` - Creates docker-loadable tarballs (v1.2 format)
- `outputs/directory.py` - Extracts to directory with optional layer deduplication
- `outputs/ocibundle.py` - Creates OCI runtime bundles for runc (inherits from
  DirWriter)
- `outputs/mounts.py` - Creates overlay mount-based extraction

### Element Types

Defined in `constants.py`:
- `CONFIG_FILE` - Image configuration JSON
- `IMAGE_LAYER` - Tarball containing filesystem layer

## URI-Style Command Line

The new `process` command uses URI-style arguments:

```
occystrap process SOURCE DESTINATION [-f FILTER]...
```

### Input URIs

```
registry://[user:pass@]host/image:tag[?arch=X&os=Y&variant=Z]
docker://image:tag[?socket=/path/to/socket]
tar:///path/to/file.tar
```

### Output URIs

```
tar:///path/to/output.tar
dir:///path/to/directory[?unique_names=true&expand=true]
oci:///path/to/bundle
mounts:///path/to/directory
```

### Filter Specifications

```
filter-name
filter-name:option=value
filter-name:opt1=val1,opt2=val2
```

Available filters:
- `normalize-timestamps` - Normalize layer timestamps (option: `ts=TIMESTAMP`)
- `search` - Search for files (options: `pattern=GLOB`, `regex=true`,
  `script_friendly=true`)

## Key Concepts

### Whiteout Files

OCI layers use special files to mark deletions:
- `.wh.<filename>` - Marks a file as deleted
- `.wh..wh..opq` - Marks directory as opaque (contents replaced)

Processed in `outputs/directory.py` when `expand` option is used.

### Unique Names Mode

`unique_names=true` enables storing multiple images in one directory by
prefixing manifest files with image/tag names. A `catalog.json` tracks which
layers belong to which images.

### Timestamp Normalization

The `normalize-timestamps` filter rewrites layer tar mtimes for reproducible
builds, recalculating layer SHAs.

## Data Flow

```
                                    +-----------------+
                                    |                 |
Input URI  -->  Input Source  -->  | Filter Chain    |  -->  Output Writer  -->  Files
                     |              | (optional)      |            |
                   fetch()          +-----------------+        finalize()
                     |                     |
                     +---------------------+
                       process_image_element()
                              |
                        fetch_callback
                       (skip/include)
```
