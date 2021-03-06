#!/usr/bin/python3

# Call me like this:
#  docker-image-extract tarfile.tar extracted

import tarfile
import json
import os
import sys

image_path = sys.argv[1]
extracted_path = sys.argv[2]

with tarfile.open(image_path) as image:
    manifest = json.loads(image.extractfile('manifest.json').read())
    print('Manifest: %s' % manifest)

    config = json.loads(image.extractfile(manifest[0]['Config']).read())
    print('Config: %s' % config)

    for layer in manifest[0]['Layers']:
        print('Found layer: %s' % layer)
        layer_tar = tarfile.open(fileobj=image.extractfile(layer))

        for tarinfo in layer_tar:
            print('  ... %s' % tarinfo.name)
            if tarinfo.isdev():
                print('  --> skip device files')
                continue

            dest = os.path.join(extracted_path, tarinfo.name)
            if not tarinfo.isdir() and os.path.exists(dest):
                print('  --> remove old version of file')
                os.unlink(dest)

            layer_tar.extract(tarinfo, path=extracted_path)
