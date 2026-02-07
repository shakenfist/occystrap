CONFIG_FILE = 'config_file'
IMAGE_LAYER = 'image_layer'

# Compression type constants
COMPRESSION_GZIP = 'gzip'
COMPRESSION_ZSTD = 'zstd'
COMPRESSION_NONE = 'none'
COMPRESSION_UNKNOWN = 'unknown'

# Docker manifest media types
MEDIA_TYPE_DOCKER_MANIFEST_V2 = \
    'application/vnd.docker.distribution.manifest.v2+json'
MEDIA_TYPE_DOCKER_MANIFEST_LIST_V2 = \
    'application/vnd.docker.distribution.manifest.list.v2+json'
MEDIA_TYPE_DOCKER_CONFIG = 'application/vnd.docker.container.image.v1+json'

# Docker layer media types
MEDIA_TYPE_DOCKER_LAYER_GZIP = \
    'application/vnd.docker.image.rootfs.diff.tar.gzip'
MEDIA_TYPE_DOCKER_LAYER_ZSTD = \
    'application/vnd.docker.image.rootfs.diff.tar.zstd'

# OCI manifest media types
MEDIA_TYPE_OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
MEDIA_TYPE_OCI_INDEX = 'application/vnd.oci.image.index.v1+json'

# OCI layer media types
MEDIA_TYPE_OCI_LAYER_GZIP = 'application/vnd.oci.image.layer.v1.tar+gzip'
MEDIA_TYPE_OCI_LAYER_ZSTD = 'application/vnd.oci.image.layer.v1.tar+zstd'
MEDIA_TYPE_OCI_LAYER_UNCOMPRESSED = 'application/vnd.oci.image.layer.v1.tar'

RUNC_SPEC_TEMPLATE = """{
    "ociVersion": "1.0.2-dev",
    "process": {
        "terminal": false,
        "user": {
            "uid": 0,
            "gid": 0
        },
        "args": [
            "sh"
        ],
        "env": [
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM=xterm"
        ],
        "cwd": "/",
        "capabilities": {
            "bounding": [
                "CAP_AUDIT_WRITE",
                "CAP_KILL",
                "CAP_NET_BIND_SERVICE"
            ],
            "effective": [
                "CAP_AUDIT_WRITE",
                "CAP_KILL",
                "CAP_NET_BIND_SERVICE"
            ],
            "inheritable": [
                "CAP_AUDIT_WRITE",
                "CAP_KILL",
                "CAP_NET_BIND_SERVICE"
            ],
            "permitted": [
                "CAP_AUDIT_WRITE",
                "CAP_KILL",
                "CAP_NET_BIND_SERVICE"
            ],
            "ambient": [
                "CAP_AUDIT_WRITE",
                "CAP_KILL",
                "CAP_NET_BIND_SERVICE"
            ]
        },
        "rlimits": [
            {
                "type": "RLIMIT_NOFILE",
                "hard": 1024,
                "soft": 1024
            }
        ],
        "noNewPrivileges": true
    },
    "root": {
        "path": "rootfs",
        "readonly": true
    },
    "hostname": "runc",
    "mounts": [
        {
            "destination": "/proc",
            "type": "proc",
            "source": "proc"
        },
        {
            "destination": "/dev",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": [
                "nosuid",
                "strictatime",
                "mode=755",
                "size=65536k"
            ]
        },
        {
            "destination": "/dev/pts",
            "type": "devpts",
            "source": "devpts",
            "options": [
                "nosuid",
                "noexec",
                "newinstance",
                "ptmxmode=0666",
                "mode=0620",
                "gid=5"
            ]
        },
        {
            "destination": "/dev/shm",
            "type": "tmpfs",
            "source": "shm",
            "options": [
                "nosuid",
                "noexec",
                "nodev",
                "mode=1777",
                "size=65536k"
            ]
        },
        {
            "destination": "/dev/mqueue",
            "type": "mqueue",
            "source": "mqueue",
            "options": [
                "nosuid",
                "noexec",
                "nodev"
            ]
        },
        {
            "destination": "/sys",
            "type": "sysfs",
            "source": "sysfs",
            "options": [
                "nosuid",
                "noexec",
                "nodev",
                "ro"
            ]
        },
        {
            "destination": "/sys/fs/cgroup",
            "type": "cgroup",
            "source": "cgroup",
            "options": [
                "nosuid",
                "noexec",
                "nodev",
                "relatime",
                "ro"
            ]
        }
    ],
    "linux": {
        "resources": {
            "devices": [
                {
                    "allow": false,
                    "access": "rwm"
                }
            ]
        },
        "namespaces": [
            {
                "type": "pid"
            },
            {
                "type": "network"
            },
            {
                "type": "ipc"
            },
            {
                "type": "uts"
            },
            {
                "type": "mount"
            },
            {
                "type": "cgroup"
            }
        ],
        "maskedPaths": [
            "/proc/acpi",
            "/proc/asound",
            "/proc/kcore",
            "/proc/keys",
            "/proc/latency_stats",
            "/proc/timer_list",
            "/proc/timer_stats",
            "/proc/sched_debug",
            "/sys/firmware",
            "/proc/scsi"
        ],
        "readonlyPaths": [
            "/proc/bus",
            "/proc/fs",
            "/proc/irq",
            "/proc/sys",
            "/proc/sysrq-trigger"
        ]
    }
}"""
