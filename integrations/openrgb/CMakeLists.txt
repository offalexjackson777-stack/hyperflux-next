cmake_minimum_required(VERSION 3.25)
project(hyperflux_openrgb LANGUAGES CXX)

include(CTest)

if(NOT TARGET hyperflux_sdk)
    add_subdirectory("${CMAKE_CURRENT_SOURCE_DIR}/../../sdk/cpp" "${CMAKE_CURRENT_BINARY_DIR}/sdk")
endif()

add_library(
    hyperflux_openrgb_core
    STATIC
        src/controller_model.cpp
)
set_target_properties(hyperflux_openrgb_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_features(hyperflux_openrgb_core PUBLIC cxx_std_20)
target_include_directories(
    hyperflux_openrgb_core
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(hyperflux_openrgb_core PUBLIC hyperflux_sdk)

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(hyperflux_openrgb_core PRIVATE -Wall -Wextra -Werror -pedantic)
endif()

set(HFX_OPENRGB_SOURCE_DIR "" CACHE PATH "Pinned OpenRGB 1.0rc3 source directory")
if(NOT HFX_OPENRGB_SOURCE_DIR AND DEFINED ENV{HFX_OPENRGB_SOURCE_DIR})
    set(HFX_OPENRGB_SOURCE_DIR "$ENV{HFX_OPENRGB_SOURCE_DIR}")
endif()

if(HFX_OPENRGB_SOURCE_DIR)
    execute_process(
        COMMAND git rev-parse HEAD
        WORKING_DIRECTORY "${HFX_OPENRGB_SOURCE_DIR}"
        OUTPUT_VARIABLE HFX_OPENRGB_SOURCE_REVISION
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE HFX_OPENRGB_GIT_RESULT
    )
    if(NOT HFX_OPENRGB_GIT_RESULT EQUAL 0)
        message(FATAL_ERROR "HFX_OPENRGB_SOURCE_DIR is not a readable Git checkout")
    endif()
    if(NOT HFX_OPENRGB_SOURCE_REVISION STREQUAL "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0")
        message(FATAL_ERROR "OpenRGB source revision does not match the integration catalog")
    endif()

    add_library(
        hyperflux_openrgb_pinned_upstream
        OBJECT
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController/RGBControllerKeyNames.cpp"
            "${HFX_OPENRGB_SOURCE_DIR}/KeyboardLayoutManager/KeyboardLayoutManager.cpp"
            "${HFX_OPENRGB_SOURCE_DIR}/Controllers/RazerController/RazerDevices.cpp"
    )
    set_target_properties(
        hyperflux_openrgb_pinned_upstream
        PROPERTIES POSITION_INDEPENDENT_CODE ON
    )
    target_compile_features(hyperflux_openrgb_pinned_upstream PUBLIC cxx_std_20)
    target_include_directories(
        hyperflux_openrgb_pinned_upstream
        PRIVATE
            "${HFX_OPENRGB_SOURCE_DIR}"
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
            "${HFX_OPENRGB_SOURCE_DIR}/KeyboardLayoutManager"
            "${HFX_OPENRGB_SOURCE_DIR}/Controllers/RazerController"
            "${HFX_OPENRGB_SOURCE_DIR}/Controllers/RazerController/RazerController"
            "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
            "${HFX_OPENRGB_SOURCE_DIR}/interop"
            "/usr/include/hidapi"
    )

    add_library(
        hyperflux_openrgb_razer_registry
        STATIC
            src/razer_registry.cpp
            $<TARGET_OBJECTS:hyperflux_openrgb_pinned_upstream>
    )
    set_target_properties(
        hyperflux_openrgb_razer_registry
        PROPERTIES POSITION_INDEPENDENT_CODE ON
    )
    target_compile_features(hyperflux_openrgb_razer_registry PUBLIC cxx_std_20)
    target_include_directories(
        hyperflux_openrgb_razer_registry
        PUBLIC
            "${CMAKE_CURRENT_SOURCE_DIR}/include"
        PRIVATE
            "${HFX_OPENRGB_SOURCE_DIR}"
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
            "${HFX_OPENRGB_SOURCE_DIR}/KeyboardLayoutManager"
            "${HFX_OPENRGB_SOURCE_DIR}/Controllers/RazerController"
            "${HFX_OPENRGB_SOURCE_DIR}/Controllers/RazerController/RazerController"
            "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
            "${HFX_OPENRGB_SOURCE_DIR}/interop"
            "/usr/include/hidapi"
    )
    target_link_libraries(hyperflux_openrgb_razer_registry PUBLIC hyperflux_openrgb_core)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux_openrgb_razer_registry
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()

    if(BUILD_TESTING)
        add_executable(
            openrgb-razer-registry-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_razer_registry_contract.cpp"
        )
        target_link_libraries(
            openrgb-razer-registry-contract
            PRIVATE hyperflux_openrgb_razer_registry
        )
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-razer-registry-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(NAME openrgb-razer-registry-contract COMMAND openrgb-razer-registry-contract)
    endif()
else()
    message(STATUS "Pinned OpenRGB source not configured; building adapter core only")
endif()

if(BUILD_TESTING)
    add_executable(
        openrgb-controller-model-contract
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_controller_model_contract.cpp"
    )
    target_link_libraries(openrgb-controller-model-contract PRIVATE hyperflux_openrgb_core)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-controller-model-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(NAME openrgb-controller-model-contract COMMAND openrgb-controller-model-contract)
endif()
