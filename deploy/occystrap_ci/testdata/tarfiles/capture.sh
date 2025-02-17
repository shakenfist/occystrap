#!/bin/bash -ex

# $1 is the docker version

DATA_DIR="/tmp/data"
DOCKER_BIN="${DATA_DIR}/docker-${1}"

mkdir -p "${DATA_DIR}"

# Setup environment
apt-get update
apt-get dist-upgrade -y
apt-get install -y build-essential make zip
apt-get remove -y apparmor

# Setup a fake apparmor profile loader
cat - > /lib/init/apparmor-profile-load << EOF
#!/bin/bash
exit 0
EOF
chmod ugo+rx /lib/init/apparmor-profile-load

# Fetch docker version
wget https://get.docker.io/builds/Linux/x86_64/docker-${1} -O ${DOCKER_BIN}
chmod u+rx ${DOCKER_BIN}

# Start daemon
${DOCKER_BIN} -d -D &
sleep 20
chmod ugo+rw /var/run/docker.sock

# Create a "scratch" image
tar cv --files-from /dev/null | \
    ${DOCKER_BIN} import - scratch
${DOCKER_BIN} images
${DOCKER_BIN} save scratch > "${DATA_DIR}/scratch.tar"
mkdir -p "${DATA_DIR}/scratch"
tar xf "${DATA_DIR}/scratch.tar" -C "${DATA_DIR}/scratch"

# Compile a simple rm implementation
cd /home/ubuntu/tarfiles
gcc rm.c -o rm -Wall -static

# Create a "mkdir" tarball
mkdir -p mydir
tar cf mydir.tar mydir

# Create a tarfile which is a little more complicated
${DOCKER_BIN} build -t test-1:latest .
${DOCKER_BIN} images
cd ${DOCKER_BIN}
${DOCKER_BIN} save test-1 > "${DATA_DIR}/test-1.tar"
mkdir -p "${DATA_DIR}/test-1"
tar xf "${DATA_DIR}/test-1.tar" -C "${DATA_DIR}/test-1"

# Zip it all up
cd "${DATA_DIR}/.."
zip bundle.zip data
chmod ugo+r bundle.zip
