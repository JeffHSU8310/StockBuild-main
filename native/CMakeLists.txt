cmake_minimum_required(VERSION 3.25)
project(stockbuild_native VERSION 0.3.0 LANGUAGES CXX)

option(STOCKBUILD_BUILD_PYTHON "Build the pybind11 extension" ON)
option(STOCKBUILD_ENABLE_SANITIZERS "Enable supported compiler sanitizers" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(stockbuild_native_core STATIC
    src/indicators.cpp
    src/resampler.cpp
    src/sqlite_reader.cpp
)
target_include_directories(stockbuild_native_core PUBLIC include)
set_target_properties(stockbuild_native_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

function(stockbuild_strict_target target)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /WX /permissive- /EHsc /utf-8)
        if(STOCKBUILD_ENABLE_SANITIZERS)
            # /Zi 同時避免 C5072（ASan 無偵錯資訊）；警告仍維持 /WX。
            target_compile_options(${target} PRIVATE /fsanitize=address /Zi)
            target_link_options(${target} PRIVATE /INCREMENTAL:NO /DEBUG)
        endif()
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Werror)
        if(STOCKBUILD_ENABLE_SANITIZERS)
            target_compile_options(${target} PRIVATE -fsanitize=address,undefined)
            target_link_options(${target} PRIVATE -fsanitize=address,undefined)
        endif()
    endif()
endfunction()

stockbuild_strict_target(stockbuild_native_core)
if(UNIX AND NOT APPLE)
    target_link_libraries(stockbuild_native_core PRIVATE dl)
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(stockbuild_schema_smoke tests/schema_smoke.cpp)
    target_link_libraries(stockbuild_schema_smoke PRIVATE stockbuild_native_core)
    stockbuild_strict_target(stockbuild_schema_smoke)
    add_test(NAME stockbuild_schema_smoke COMMAND stockbuild_schema_smoke)
endif()

if(STOCKBUILD_BUILD_PYTHON)
    find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
    execute_process(
        COMMAND "${Python_EXECUTABLE}" -m pybind11 --cmakedir
        OUTPUT_VARIABLE _pybind11_cmake_dir
        OUTPUT_STRIP_TRAILING_WHITESPACE
        COMMAND_ERROR_IS_FATAL ANY
    )
    list(PREPEND CMAKE_PREFIX_PATH "${_pybind11_cmake_dir}")
    find_package(pybind11 CONFIG REQUIRED)
    pybind11_add_module(_stockbuild_native MODULE python/module.cpp)
    target_link_libraries(_stockbuild_native PRIVATE stockbuild_native_core)
    target_include_directories(_stockbuild_native PRIVATE include)
    stockbuild_strict_target(_stockbuild_native)
endif()
