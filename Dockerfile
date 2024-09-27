# Title Valhalla DockerFile
# Description: This DockerFile is used to build a docker image for the Valhalla routing engine.
#              The image is built from the official Valhalla docker image 3.5.0 and adds 
#              dependancies and tools to run the routing engine with predicted traffic.
# Usage: sudo docker build -t valhalla . && docker run -dt --name valhalla -p 8002:8002 valhalla
# Last Modified: 2024-09-24

FROM ghcr.io/valhalla/valhalla:3.5.0

# Install the necessary dependancies for compiling tools
RUN apt-get update > /dev/null && \
  export DEBIAN_FRONTEND=noninteractive && \
  apt-get install -y g++ libboost-all-dev protobuf-compiler libcurl4-openssl-dev 
# python3-dev python3 python3-pip python3-venv


RUN mkdir /custom_files
RUN mkdir /custom_files/valhalla_tiles
RUN mkdir /custom_files/traffic
RUN mkdir /custom_files/conf
WORKDIR /custom_files

ENV VALHALLA_CONFIG /custom_files/conf/valhalla.json

RUN valhalla_build_config \
    --mjolnir-tile-dir        /custom_files/valhalla_tiles \
    --mjolnir-tile-extract    /custom_files/valhalla_tiles.tar \
    --mjolnir-timezone        /custom_files/valhalla_tiles/timezones.sqlite \
    --mjolnir-admin           /custom_files/valhalla_tiles/admins.sqlite \
    > ${VALHALLA_CONFIG}

RUN wget http://download.geofabrik.de/north-america/us/tennessee-latest.osm.pbf -O tennessee-latest.osm.pbf


RUN valhalla_build_timezones > valhalla_tiles/timezones.sqlite
RUN valhalla_build_tiles --config ${VALHALLA_CONFIG} tennessee-latest.osm.pbf


# ========== predicted traffic ==========

# Generate the valhalla_encode_speed and valhalla_ways_to_ids tools
COPY valhalla_ways_to_ids.cc .
COPY valhalla_encode_speed.cc .
RUN g++ -o valhalla_encode_speed valhalla_encode_speed.cc -I/usr/local/include -L/usr/local/lib -lvalhalla && \
    g++ -o valhalla_ways_to_ids valhalla_ways_to_ids.cc -I/usr/local/include -I/usr/local/include/valhalla/third_party \
    -L/usr/local/lib -lvalhalla -lz -lcurl

# Generate the file valhalla_tiles/way_edges_ids_paths.csv
RUN ./valhalla_ways_to_ids ${VALHALLA_CONFIG}

# Adds the predicted traffic to the tiles (since there is no in traffic, it will be empty)
RUN valhalla_add_predicted_traffic -t traffic --config ${VALHALLA_CONFIG}
RUN valhalla_build_extract --config ${VALHALLA_CONFIG} 

# Prepare the traffic directory (makes copying easier later)
RUN cd valhalla_tiles;\
    find . -type d -exec mkdir -p -- ../traffic/{} \; && \
    find . -type f -name *.gph -exec sh -c 'touch $(echo ../traffic/{} | sed "s/gph$/csv/g")' \;

# Expose the necessary port
EXPOSE 8002

ENTRYPOINT ["valhalla_service"]
CMD ["/custom_files/conf/valhalla.json", "4"]