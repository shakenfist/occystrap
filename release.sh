#!/bin/bash -e

# Release a specified version of Occy Strap

echo "--- Determine verison number ---"
PREVIOUS=`git tag | egrep "^v" | sort -n | tail -1 | sed 's/^v//'`

echo
echo -n "What is the version number (previous was $PREVIOUS)? "
read VERSION

echo
echo "--- Setup ---"
set -x
pip install --upgrade readme-renderer
pip install --upgrade twine
rm -rf build dist *.egg-info
git pull || true
set +x

echo
echo "--- Setup ---"
echo "Do you want to apply a git tag for this release (yes to tag)?"
read TAG
set -x

if [ "%$TAG%" == "%yes%" ]
then
  git tag -s "v$VERSION" -m "Release v$VERSION"
  git push origin "v$VERSION"
fi

python3 setup.py sdist bdist_wheel
twine check dist/*
set +x

echo
echo "--- Uploading ---"
echo "This is the point where we push files to pypi. Hit ctrl-c to abort."
read DUMMY

set -x
twine upload dist/*
set +x