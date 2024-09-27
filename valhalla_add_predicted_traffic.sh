#!/bin/bash

# Title valhalla_add_predicted_traffic.sh
# Description: This script will add copy the traffic data to the 
#              container and add the predicted traffic to the routing graph.
# Usage: bash valhalla_add_predicted_traffic.sh
# Last Modified: 2024-09-23

VALHALLA_CONFIG="/custom_files/conf/valhalla.json"
CONTAINER="valhalla"

docker cp data/traffic ${CONTAINER}:/custom_files/traffic
docker exec -i ${CONTAINER} bash -s <<EOF
    valhalla_add_predicted_traffic -t traffic --config ${VALHALLA_CONFIG}
    valhalla_build_extract --config ${VALHALLA_CONFIG} --overwrite
EOF
docker restart ${CONTAINER}