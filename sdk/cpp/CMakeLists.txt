cmake_minimum_required(VERSION 3.25)
project(hyperflux_sdk LANGUAGES CXX)

add_library(hyperflux_sdk INTERFACE)
target_compile_features(hyperflux_sdk INTERFACE cxx_std_20)
target_include_directories(hyperflux_sdk INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}/include")

