#!/bin/bash

# Title valhalla_add_predicted_traffic.sh
# Description: This script will copy a folder of traffic data to the
#              container and add the predicted traffic to the routing graph.
#              Every load starts from the tiles without traffic, so the
#              speeds of an earlier load do not stay in the graph.
# Usage: bash valhalla_add_predicted_traffic.sh [traffic folder] [container]
# Last Modified: 2026-11-24
set -euo pipefail

TRAFFIC="${1:-data/traffic}"
CONTAINER="${2:-valhalla}"
VALHALLA_CONFIG="/custom_files/conf/valhalla.json"
TILES_NO_TRAFFIC="/custom_files/valhalla_tiles_no_traffic.tar"

if [ ! -d "${TRAFFIC}" ]; then
    echo "${TRAFFIC} is not a folder" >&2
    exit 1
fi
if ! docker exec "${CONTAINER}" true; then
    echo "cannot run a command in the container ${CONTAINER}: is it running?" >&2
    exit 1
fi
if ! docker exec "${CONTAINER}" test -f "${TILES_NO_TRAFFIC}"; then
    echo "${CONTAINER} has no ${TILES_NO_TRAFFIC}: build the image again from the Dockerfile" >&2
    exit 1
fi

docker exec "${CONTAINER}" tar -xf "${TILES_NO_TRAFFIC}" -C /custom_files/valhalla_tiles --exclude index.bin
docker exec "${CONTAINER}" rm -rf /custom_files/traffic
docker cp "${TRAFFIC}" "${CONTAINER}:/custom_files/traffic"
docker exec "${CONTAINER}" valhalla_add_predicted_traffic -t /custom_files/traffic --config "${VALHALLA_CONFIG}"
docker exec "${CONTAINER}" valhalla_build_extract --config "${VALHALLA_CONFIG}" --overwrite
docker restart "${CONTAINER}"
