# Occy Strap

Occy Strap is a simple set of Docker and OCI container tools, which can be used either for container forensics or for implementing an OCI orchestrator, depending on your needs. This is a very early implementation, so be braced for impact.

## Downloading an image from a repository

Let's say we want to download an image from a repository and store it as a local tarball. This is a common thing to want to do in airgapped environments for example. You could do this with docker with a `docker pull; docker save`. The Occy Strap equivalent is:

`occystrap --verbose fetch registry-1.docker.io library/busybox latest busybox-occy.tar`

In this example we're pulling from the Docker Hub (registry-1.docker.io), and are downloading busybox's latest version into a tarball named `busybox-occy.tar`. This tarball can be loaded with `docker load -i busybox-occy.tar` on an airgapped Docker environment.