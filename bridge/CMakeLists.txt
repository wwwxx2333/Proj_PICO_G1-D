cmake_minimum_required(VERSION 3.16)
project(g1d_agv_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(unitree_sdk2 CONFIG REQUIRED)

add_executable(g1d_agv_bridge g1d_agv_bridge.cpp)
target_link_libraries(g1d_agv_bridge PRIVATE unitree_sdk2)

add_executable(g1d_height_probe g1d_height_probe.cpp)
target_link_libraries(g1d_height_probe PRIVATE unitree_sdk2)

add_executable(g1d_arm_bridge g1d_arm_bridge.cpp)
target_link_libraries(g1d_arm_bridge PRIVATE unitree_sdk2)

add_executable(g1d_hand_bridge g1d_hand_bridge.cpp)
target_link_libraries(g1d_hand_bridge PRIVATE unitree_sdk2)

add_executable(g1d_hand_state_probe g1d_hand_state_probe.cpp)
target_link_libraries(g1d_hand_state_probe PRIVATE unitree_sdk2)
