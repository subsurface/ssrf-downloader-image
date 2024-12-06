#!/bin/bash
#
# build a specific image locally; this doesn't try to parse the YAML, so it needs
# to be manually kept in sync with build-images.yml

set -e

if [ ! -f ./.secrets ]; then
    echo "missing .secrets file in current directory"
    exit 1
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --name)
            name="$2"
            shift
            ;;
    esac
    shift
done
if [ "$name" = "raspberrypi64-pi-2-3-4-5" ] ; then
        base_arch=arm64
        variant=default
        url=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-10-28/2024-10-22-raspios-bookworm-arm64-lite.img.xz
        magic_path="image/2024-10-22-raspios-bookworm-arm64-lite.img.xz"
else
        echo "Unknown build: $name"
        exit 1
fi

MAGIC_DIR=$(dirname "${magic_path}")
mkdir -p "${MAGIC_DIR}"
[ -f "${magic_path}" ] || wget -qO "${magic_path}" "$url"
# make sure this file shows up as the newest image
touch "${magic_path}"

# figure out the name
if git describe --exact-match 2>/dev/null ; then tag=$(git describe --exact-match) ; else tag=$(git rev-parse HEAD) ; fi
tag=$(echo "$tag" | sed -r 's/^(.{8}).{32}$/g-\1/')

echo "export BASE_ARCH=${base_arch}" >> config
image_name="ssrf-downloader-${name}-${tag}.img"
. ./.secrets
{
    echo "export BASE_USER_PASSWORD=${secrets_USER_PASSWORD}"
    echo "export ROOT_PWD=${secrets_ROOT_PASSWORD}"
    echo "export SSH_PUB_KEY='${secrets_SSH_KEY}'"
    echo "export SSRF_DOWNLOADER_IMAGE_NAME=${image_name}"
 } >> config

# sudo GH_REF_TYPE=${{ github.ref_type }} GH_TRGT_REF=${{ github.ref_name }} bash -x ./build_dist "${variant}"
sudo bash -x ./build_dist "${variant}"

# prepare for release upload
mkdir -p uploads
CURRENT_IMAGE_NAME="$(basename $magic_path)"
echo "${CURRENT_IMAGE_NAME}"
if [ "${variant}" = "default" ]; then
        WORKSPACE="workspace"
elif [[ "${variant}" = *"dietpi"* ]] ; then
        WORKSPACE="image-dietpi"
else
        WORKSPACE="workspace-${variant}"
fi
ls -l "$WORKSPACE"
BUILT_IMAGE="$(find $WORKSPACE -name "*.img" | head -n 1)"
sudo mv -v "${BUILT_IMAGE}" uploads/"${image_name}"
