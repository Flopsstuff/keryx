cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS ../components/xvf_control ../components/keryx_wakeword ../components/micro_frontend
                         ../components/keryx_console ../components/keryx_net ../components/keryx_link
                         ../components/keryx_panel ../components/keryx_ota)
# The version is the last commit that touched firmware/, "-dirty" if firmware/ has uncommitted changes: the rest of
# the repository (host tools, prompts) does not change what runs on the board.
set(fw_dir ${CMAKE_CURRENT_LIST_DIR}/..)
# release/ holds the built images: committing them must not change the version they carry
execute_process(COMMAND git log -1 --format=%h -- . ":(exclude)release" WORKING_DIRECTORY ${fw_dir}
                OUTPUT_VARIABLE fw_hash OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
execute_process(COMMAND git status --porcelain -- . ":(exclude)release" WORKING_DIRECTORY ${fw_dir}
                OUTPUT_VARIABLE fw_changes OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(fw_hash)
    set(PROJECT_VER ${fw_hash})
    if(fw_changes)
        set(PROJECT_VER ${fw_hash}-dirty)
    endif()
endif()
execute_process(COMMAND git rev-parse --git-dir WORKING_DIRECTORY ${fw_dir}
                OUTPUT_VARIABLE git_dir OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(git_dir)
    get_filename_component(git_dir ${git_dir} ABSOLUTE BASE_DIR ${fw_dir})
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${git_dir}/HEAD ${git_dir}/index)
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(keryx)
