cmake_minimum_required(VERSION 3.20)
project(nohesi_bot CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Release build with full optimisation
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# MSVC-specific flags
if(MSVC)
  add_compile_options(/O2 /Oi /GL /arch:AVX2 /W3)
  add_link_options(/LTCG /SUBSYSTEM:CONSOLE)
endif()

add_executable(nohesi_bot
  src/main.cpp
  src/bot.cpp
  src/spline.cpp
  src/planner.cpp
  src/controller.cpp
  src/wheel_model.cpp
  src/virtual_controller.cpp
)

target_include_directories(nohesi_bot PRIVATE src)

# Windows libs
target_link_libraries(nohesi_bot PRIVATE
  ws2_32
  winmm
)

# Copy data dir alongside binary on build
add_custom_command(TARGET nohesi_bot POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E make_directory
    $<TARGET_FILE_DIR:nohesi_bot>/data
  COMMAND ${CMAKE_COMMAND} -E make_directory
    $<TARGET_FILE_DIR:nohesi_bot>/logs
  COMMENT "Creating data/ and logs/ directories"
)
