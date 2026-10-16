cmake_minimum_required(VERSION 3.18)
project(nvus_scanner LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
file(GLOB_RECURSE NVUS_NATIVE_INPUTS CONFIGURE_DEPENDS RELATIVE "${CMAKE_SOURCE_DIR}"
  "${CMAKE_SOURCE_DIR}/cpp/*.cpp" "${CMAKE_SOURCE_DIR}/cpp/*.hpp")
list(APPEND NVUS_NATIVE_INPUTS CMakeLists.txt)
list(SORT NVUS_NATIVE_INPUTS)
set(NVUS_NATIVE_IDENTITY "")
foreach(path IN LISTS NVUS_NATIVE_INPUTS)
  file(SHA256 "${CMAKE_SOURCE_DIR}/${path}" digest)
  string(APPEND NVUS_NATIVE_IDENTITY "${path}:${digest}\n")
endforeach()
string(SHA256 NVUS_NATIVE_SOURCE_SHA256 "${NVUS_NATIVE_IDENTITY}")
execute_process(COMMAND git -c safe.directory=${CMAKE_SOURCE_DIR} rev-parse HEAD
  WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
  OUTPUT_VARIABLE NVUS_REPOSITORY_REVISION OUTPUT_STRIP_TRAILING_WHITESPACE
  ERROR_QUIET)
find_package(Threads REQUIRED)
add_library(nvus_runtime cpp/src/runtime.cpp cpp/src/hsb_backend.cpp
  cpp/src/sequence.cpp cpp/src/policy.cpp cpp/src/policy_executor.cpp)
set_target_properties(nvus_runtime PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(nvus_runtime PUBLIC cpp/include)
target_link_libraries(nvus_runtime PUBLIC Threads::Threads)
# Preserve reference delay rounding; reassociation/fast-math changes wire bytes.
set_source_files_properties(cpp/src/sequence.cpp PROPERTIES COMPILE_OPTIONS "-ffp-contract=off")
option(NVUS_BUILD_EXAMPLES "Build standalone C++ end-to-end examples (no Python)" OFF)
if(NVUS_BUILD_EXAMPLES)
  add_executable(scanner_pw4cm tests/scanner_policy.cpp)
  target_link_libraries(scanner_pw4cm PRIVATE nvus_runtime)
endif()
option(NVUS_WITH_HOLOLINK "Build native HSB SDK transport (no device access during build)" OFF)
if(NVUS_WITH_HOLOLINK)
  find_package(Python 3.10 COMPONENTS Interpreter REQUIRED)
  execute_process(COMMAND "${Python_EXECUTABLE}" -c
    "import site, sys; print(';'.join(p for p in sys.path + site.getsitepackages() if p))"
    OUTPUT_VARIABLE NVUS_PYTHON_PATHS OUTPUT_STRIP_TRAILING_WHITESPACE
    COMMAND_ERROR_IS_FATAL ANY)
  find_package(CUDAToolkit REQUIRED)
  find_package(holoscan CONFIG REQUIRED HINTS ${NVUS_PYTHON_PATHS} /opt/nvidia/holoscan)
  find_package(Hololink CONFIG REQUIRED HINTS ${NVUS_PYTHON_PATHS})
  target_sources(nvus_runtime PRIVATE cpp/src/hololink_transport.cpp)
  target_compile_definitions(nvus_runtime PUBLIC NVUS_WITH_HOLOLINK=1)
  target_link_libraries(nvus_runtime PUBLIC hololink::core hololink::common
    hololink::operators::roce_receiver hololink::operators::roce_transmitter
    holoscan::core CUDA::cuda_driver)
endif()
option(NVUS_WITH_CUDA "Build owned CUDA buffers" OFF)
if(NVUS_WITH_CUDA OR NVUS_WITH_HOLOLINK)
  find_package(CUDAToolkit REQUIRED)
  target_sources(nvus_runtime PRIVATE cpp/src/cuda_buffer.cpp)
  target_compile_definitions(nvus_runtime PUBLIC NVUS_WITH_CUDA=1)
  target_link_libraries(nvus_runtime PUBLIC CUDA::cuda_driver)
endif()
option(NVUS_BUILD_PYTHON "Build thin Python bindings" OFF)
if(NVUS_BUILD_PYTHON)
  find_package(Python 3.10 COMPONENTS Interpreter Development.Module REQUIRED)
  execute_process(COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir OUTPUT_VARIABLE nanobind_DIR OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
  find_package(nanobind CONFIG REQUIRED)
  nanobind_add_module(_scanner cpp/src/bindings.cpp)
  target_compile_definitions(_scanner PRIVATE
    NVUS_NATIVE_SOURCE_SHA256="${NVUS_NATIVE_SOURCE_SHA256}"
    NVUS_REPOSITORY_REVISION="${NVUS_REPOSITORY_REVISION}")
  target_link_libraries(_scanner PRIVATE nvus_runtime)
  install(TARGETS _scanner LIBRARY DESTINATION nvus)
  install(FILES examples/scanner_cpp_live.py DESTINATION nvus RENAME _pw4cm.py)
endif()
