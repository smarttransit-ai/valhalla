/*
Title valhalla_encode_speed.cc
Description: This program uses the valhalla::baldr::compress_speed_buckets and 
             valhalla::baldr::encode_compressed_speeds functions to compress and encode a
             vector of historical speeds.
Input: historical speeds of format:
       "10, 10, ... 100, 100, 100"
Output: encoded speeds of format:
        "AAAAAA...AAAAAA"

Usage: ./valhalla_encode_speed <historical_speeds>
Compile: g++ -o valhalla_encode_speed valhalla_encode_speed.cc -I/usr/local/include -L/usr/local/lib -lvalhalla
Dependencies: valhalla
Last Modified 2024-09-23
*/

#include "./valhalla/baldr/predictedspeeds.h"

#include <iostream>
#include <string>
#include <vector>

std::string encode_speed(const std::string& historical_speeds) {

  std::vector<float> speeds;
  std::string speed;

  for (size_t i = 0; i < historical_speeds.size(); i++) {
    if (historical_speeds[i] == ',') {
      speeds.push_back(std::stoi(speed));
      speed.clear();
    } else {
      speed += historical_speeds[i];
    }
  }
  speeds.push_back(std::stoi(speed));

  // Compress and encode the historical speeds
  std::array<short int, 200> compressed_speeds = valhalla::baldr::compress_speed_buckets(speeds.data());
  std::string encoded_speeds = valhalla::baldr::encode_compressed_speeds(compressed_speeds.data());

  return encoded_speeds;
}

int main(int argc, char** argv) {
  // Check if the user has provided the historical speeds
  if (argc < 2) {
    std::cerr << "Usage: " << argv[0] << " <historical_speeds>" << std::endl;
    return 1;
  }
  //encode_speed(argv[1]);
  std::cout << encode_speed(argv[1]);

  return 0;
}