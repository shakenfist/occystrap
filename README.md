# Occy Strap

Occy Strap is a simple set of Docker and OCI container tools, which can be used either for container forensics or for implementing an OCI orchestrator, depending on your needs. This is a very early implementation, so be braced for impact.

## Downloading an image from a repository and storing as a tarball

Let's say we want to download an image from a repository and store it as a local tarball. This is a common thing to want to do in airgapped environments for example. You could do this with docker with a `docker pull; docker save`. The Occy Strap equivalent is:

```
occystrap fetch-to-tarfile registry-1.docker.io library/busybox latest busybox.tar
```

In this example we're pulling from the Docker Hub (registry-1.docker.io), and are downloading busybox's latest version into a tarball named `busybox-occy.tar`. This tarball can be loaded with `docker load -i busybox.tar` on an airgapped Docker environment.

## Downloading an image from a repository and storing as an extracted tarball

The format of the tarball in the previous example is two JSON configuration files and a series of image layers as tarballs inside the main tarball. You can write these elements to a directory instead of to a tarball if you'd like to inspect them. For example:

```
occystrap fetch-to-extracted registry-1.docker.io library/centos 7 centos7
```

This example will pull from the Docker Hub the Centos image with the label "7", and write the content to a directory in the current working directory called "centos7". If you tarred centos7 like this, you'd end up with a tarball equivalent to what `fetch-to-tarfile` produces, which could therefore be loaded with `docker load`:

```
cd centos7; tar -cf ../centos7.tar *
```

## Downloading an image from a repository and storing it in a merged directory

In scenarios where image layers are likely to be reused between images (for example many images which share a common base layer), you can save disk space by downloading images to a directory which contains more than one image. To make this work, you need to instruct Occy Strap to use unique names for the JSON elements within the image file:

```
occystrap fetch-to-extracted --use-unique-names registry-1.docker.io \
    homeassistant/home-assistant latest merged_images
occystrap fetch-to-extracted --use-unique-names registry-1.docker.io \
    homeassistant/home-assistant stable merged_images
occystrap fetch-to-extracted --use-unique-names registry-1.docker.io \
    homeassistant/home-assistant 2021.3.0.dev20210219 merged_images
```

Each of these images include 21 layers, but the merged_images directory at the time of writing this there are 25 unique layers in the directory. You end up with a layout like this:

```
0465ae924726adc52c0216e78eda5ce2a68c42bf688da3f540b16f541fd3018c
10556f40181a651a72148d6c643ac9b176501d4947190a8732ec48f2bf1ac4fb
...
catalog.json
cd8d37c8075e8a0195ae12f1b5c96fe4e8fe378664fc8943f2748336a7d2f2f3
d1862a2c28ec9e23d88c8703096d106e0fe89bc01eae4c461acde9519d97b062
d1ac3982d662e038e06cc7e1136c6a84c295465c9f5fd382112a6d199c364d20.json
...
d81f69adf6d8aeddbaa1421cff10ba47869b19cdc721a2ebe16ede57679850f0.json
...
manifest-homeassistant_home-assistant-2021.3.0.dev20210219.json
manifest-homeassistant_home-assistant-latest.json
manifest-homeassistant_home-assistant-stable.json
```

`catalog.json` is an Occy Strap specific artefact which maps which layers are used by which image. Each of the manifest files for the various images have been converted to have a unique name instead of `manifest.json` as well.

To extract a single image from such a shared directory, use the `recreate-image` command:

```
occystrap recreate-image merged_images homeassistant/home-assistant latest ha-latest.tar
```

## Exploring the contents of layers and overwritten files

Similarly, if you'd like the layers to be expanded from their tarballs to the filesystem, you can pass the `--expand` argument to `fetch-to-extracted` to have them extracted. This will also create a filesystem at the name of the manifest which is the final state of the image (the layers applied sequential). For example:

```
occystrap fetch-to-extracted --expand quay.io \
    ukhomeofficedigital/centos-base latest ukhomeoffice-centos
```

Note that layers delete files from previous layers with files named ".wh.$previousfilename". These files are _not_ processed in the expanded layers, so that they are visible to the user. They are however processed in the merged layer named for the manifest file.

## Generating an OCI runtime bundle

This isn't fully supported yet, but you can extract an image to an OCI image bundle
with the following command:

```
occystrap fetch-to-oci registry-1.docker.io library/hello-world latest bar
```

You should then be able to run that container by doing something like:

```
cd bar
sudo apt-get install runc
sudo runc run id-0001
```

## Supporting non-default architectures

Docker image repositories can store multiple versions of a single image, with each image corresponding to a different (operating system, cpu architecture, cpu variant) tuple. Occy Strap supports letting you specify which to use with global command line flags. Occy Strap defaults to linux amd64 if you don't specify something different. For example, to fetch the linux arm64 v8 image for busybox, you would run:

```
occystrap --os linux --architecture arm64 --variant v8 \
    fetch-to-extracted registry-1.docker.io library/busybox \
    latest busybox
```