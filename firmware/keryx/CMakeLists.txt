cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS ../components/xvf_control ../components/keryx_wakeword ../components/micro_frontend
                         ../components/keryx_console ../components/keryx_net ../components/keryx_link)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(keryx)
