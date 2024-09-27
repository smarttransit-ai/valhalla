/*
Title valhalla_ways_to_ids.cc
Description: This program uses the valhalla functions to generate 
             the ways_edges export and convert it into a csv file with the features
             osm_id, edge_id, graph_id, path
Input: N/A
Output: valhalla_tiles/way_edges_ids_paths.csv

Usage: ./valhalla_ways_to_ids 

Compile: g++ -o valhalla_ways_to_ids valhalla_ways_to_ids.cc -I/usr/local/include -I/usr/local/include/valhalla/third_party -L/usr/local/lib -lvalhalla -lz -lcurl
Dependencies: valhalla, libboost-all-dev protobuf-compiler libcurl4-openssl-dev
Last Modified 2024-09-23
*/



#include <iostream>
#include <sstream>
#include <fstream>
#include <filesystem>
#include <vector>
#include <unistd.h>
#include <sys/wait.h>
#include "./valhalla/baldr/graphid.h"
#include "./valhalla/baldr/graphtile.h"
#include <boost/algorithm/string/replace.hpp>

class WayEdge {
    public:
        std::string osm_id;
        uint64_t edge_id;
        std::string graph_id;
        std::string path;
        WayEdge(std::string osm_id, uint64_t  edge_id, std::string graph_id, std::string path) {
            this->osm_id = osm_id;
            this->edge_id = edge_id;
            this->graph_id = graph_id;
            this->path = path;
        }
};

/*
Run the valhalla_ways_to_edges executable to generate the way_edges.txt file
*/
int valhalla_ways_to_edges(const std::string& config_file = "/custom_files/conf/valhalla.json") {
   int pid, status;
   if (pid = fork()) {
       waitpid(pid, &status, 0); // wait for the child to exit
   } else {
       const char executable[] = "/usr/local/bin/valhalla_ways_to_edges";
       execl(executable, executable, "--config", "/custom_files/conf/valhalla.json", NULL);
   }
   return status;
}

/*
Read the way_edges.txt file and return a vector of WayEdge objects
osm id is the first element, graph id is the second element, 4th element, 6th element, etc
Use valhalla::baldr::GraphId and valhalla::baldr::GraphTile::FileSuffix 
to get the level/tile_id/id and tile hierarchy
*/
std::vector<WayEdge> read_way_edges() {
  std::vector<WayEdge> way_edges;
  std::string line;
  std::ifstream file ("valhalla_tiles/way_edges.txt");
  if (!file.is_open()) {
      std::cout << "Unable to open file" << std::endl;
      return way_edges;
  }
  while (getline(file, line)) {
    std::stringstream ss(line);
    std::string substr;

    std::string osm_id;
    uint64_t edge_id;
    valhalla::baldr::GraphId graph_obj;
    std::string path;

    getline(ss, osm_id, ',');
    int indx = 1;
    while (getline(ss, substr, ',')) {
      if (indx % 2 == 0) {
          edge_id = std::stoull(substr);
          graph_obj = valhalla::baldr::GraphId (std::stoull(substr));
          path = valhalla::baldr::GraphTile::FileSuffix(graph_obj);
          boost::replace_all(path, ".gph", ".csv");
          //std::cout << osm_id <<"|"<< edge_id <<"|"<< graph_obj <<"|"<< path << std::endl;
          way_edges.emplace_back(osm_id, edge_id, std::to_string(graph_obj), path);
      }
      ++indx;
    }
  }
  file.close();
  return way_edges;
}

/*
Write the way_edges vector to a csv file
*/
void write_way_edges(std::vector<WayEdge> way_edges) {
  std::ofstream file("valhalla_tiles/way_edges_ids_paths.csv");
  file << "osm_id,edge_id,graph_id,path" << std::endl;
  for (auto way_edge : way_edges) {
    file << way_edge.osm_id << "," << way_edge.edge_id << "," << way_edge.graph_id << "," << way_edge.path << std::endl;
  }
  file.close();
}

int main(int argc, char** argv) {
    valhalla_ways_to_edges(argv[1]);
    std::vector<WayEdge> way_edges = read_way_edges();
    write_way_edges(way_edges);
    return 0;
}