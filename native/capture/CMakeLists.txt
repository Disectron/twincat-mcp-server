cmake_minimum_required(VERSION 3.16)
project(tcmcp_capture CXX)

# tcmcp-capture: native capture service for twincat-mcp, built on Beckhoff's AdsLib (third_party/beckhoff-ads).
#
#   Windows (default): AdsLib in TwinCAT mode (USE_TWINCAT_ROUTER) over Beckhoff's TcAdsDll.dll.
#     Without -DTCMCP_TCADSDLL_SDK the TcAdsDll functions are bound at run time (tcadsdll/ + tcadsdll_loader.cpp),
#     so CI builds the exe without the TwinCAT SDK. With
#       -DTCMCP_TCADSDLL_SDK="C:/Program Files (x86)/Beckhoff/TwinCAT/AdsApi/TcAdsDll"
#     Beckhoff's own TcAdsDef.h / TcAdsAPI.h and TcAdsDll.lib are used instead (same static_asserts).
#   Elsewhere: AdsLib standalone (its own AMS/TCP router) — remote targets (TC/BSD, CX) and the simulator.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()

# version = the Python package version (the server refuses an exe of another version)
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/../../pyproject.toml" _ver REGEX "^version *= *\"")
string(REGEX REPLACE "^version *= *\"([^\"]+)\".*" "\\1" TCMCP_VERSION "${_ver}")
if(NOT TCMCP_VERSION)
  set(TCMCP_VERSION "0.0.0-dev")
endif()
message(STATUS "tcmcp-capture ${TCMCP_VERSION}")

if(WIN32)
  set(_default_mode twincat)
else()
  set(_default_mode standalone)
endif()
set(TCMCP_ADS_MODE "${_default_mode}" CACHE STRING "twincat (TcAdsDll router) or standalone (AdsLib AMS/TCP)")
set(TCMCP_TCADSDLL_SDK "" CACHE PATH "TwinCAT AdsApi/TcAdsDll folder (Include, Lib/x64); empty = bind TcAdsDll.dll at run time")

set(ADS ${CMAKE_CURRENT_SOURCE_DIR}/third_party/beckhoff-ads/AdsLib)
set(SRC
  src/main.cpp
  src/engine.cpp
  src/source_ads.cpp
  src/source_sim.cpp
  ${ADS}/AdsDef.cpp
  ${ADS}/AdsDevice.cpp
)
set(DEFS TCMCP_VERSION="${TCMCP_VERSION}" CONFIG_DEFAULT_LOGLEVEL=2)   # AdsLib logs warnings+ to stderr
set(INCS ${CMAKE_CURRENT_SOURCE_DIR}/src ${ADS})
set(LIBS)

if(TCMCP_ADS_MODE STREQUAL "twincat")
  if(NOT WIN32)
    message(FATAL_ERROR "TCMCP_ADS_MODE=twincat needs Windows (TcAdsDll)")
  endif()
  list(APPEND SRC ${ADS}/TwinCAT/AdsLib.cpp)
  list(APPEND DEFS USE_TWINCAT_ROUTER)
  if(TCMCP_TCADSDLL_SDK)
    list(APPEND INCS "${TCMCP_TCADSDLL_SDK}/Include")
    find_library(TCADSDLL_LIB TcAdsDll PATHS "${TCMCP_TCADSDLL_SDK}/Lib/x64" "${TCMCP_TCADSDLL_SDK}/lib/x64" NO_DEFAULT_PATH REQUIRED)
    list(APPEND LIBS ${TCADSDLL_LIB})
  else()
    list(APPEND INCS ${CMAKE_CURRENT_SOURCE_DIR}/tcadsdll)
    list(APPEND SRC src/tcadsdll_loader.cpp)
    list(APPEND DEFS TCMCP_TCADSDLL_RUNTIME)
  endif()
  list(APPEND LIBS ws2_32)
elseif(TCMCP_ADS_MODE STREQUAL "standalone")
  list(APPEND SRC
    ${ADS}/AdsLib.cpp
    ${ADS}/Frame.cpp
    ${ADS}/Log.cpp
    ${ADS}/Sockets.cpp
    ${ADS}/standalone/AdsLib.cpp
    ${ADS}/standalone/AmsConnection.cpp
    ${ADS}/standalone/AmsNetId.cpp
    ${ADS}/standalone/AmsPort.cpp
    ${ADS}/standalone/AmsRouter.cpp
    ${ADS}/standalone/NotificationDispatcher.cpp
  )
  if(WIN32)
    list(APPEND LIBS ws2_32)
  endif()
else()
  message(FATAL_ERROR "TCMCP_ADS_MODE must be twincat or standalone")
endif()

find_package(Threads REQUIRED)
add_executable(tcmcp-capture ${SRC})
target_include_directories(tcmcp-capture PRIVATE ${INCS})
target_compile_definitions(tcmcp-capture PRIVATE ${DEFS})
target_link_libraries(tcmcp-capture PRIVATE ${LIBS} Threads::Threads)

if(MSVC)
  set_property(TARGET tcmcp-capture PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  target_compile_definitions(tcmcp-capture PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
  target_compile_options(tcmcp-capture PRIVATE /W3 /EHsc /permissive- /utf-8)
  # deterministic output (no timestamps in the PE/PDB): the same sources + toolchain give the same bytes, so a
  # release exe can be rebuilt and compared against the SHA-256 shipped next to it
  target_compile_options(tcmcp-capture PRIVATE /Brepro)
  target_link_options(tcmcp-capture PRIVATE /Brepro)
else()
  target_compile_options(tcmcp-capture PRIVATE -Wall -Wextra)
  if(WIN32)
    target_link_options(tcmcp-capture PRIVATE -static)
  endif()
endif()
