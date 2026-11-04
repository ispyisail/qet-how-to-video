# SPDX-License-Identifier: GPL-2.0-or-later
cmake_minimum_required(VERSION 3.16)
project(qetdemoprobe CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_AUTOMOC ON)
find_package(Qt6 REQUIRED COMPONENTS Core Widgets Network)
add_library(qetdemoprobe SHARED probe.cpp)
target_link_libraries(qetdemoprobe PRIVATE Qt6::Core Qt6::Widgets Qt6::Network)
