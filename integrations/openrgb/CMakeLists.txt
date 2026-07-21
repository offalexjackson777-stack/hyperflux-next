cmake_minimum_required(VERSION 3.25)
project(hyperflux_openrgb LANGUAGES CXX)

include(CTest)
find_package(Threads REQUIRED)

option(
    HFX_OPENRGB_THREAD_SANITIZER
    "Instrument HyperFlux-owned OpenRGB code with ThreadSanitizer"
    OFF
)

function(hfx_openrgb_enable_thread_sanitizer target)
    if(NOT HFX_OPENRGB_THREAD_SANITIZER)
        return()
    endif()
    if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        message(FATAL_ERROR "HFX_OPENRGB_THREAD_SANITIZER requires Clang or GCC")
    endif()
    target_compile_options(${target} PRIVATE -fsanitize=thread -fno-omit-frame-pointer -g)
    target_link_options(${target} PRIVATE -fsanitize=thread)
endfunction()

if(NOT TARGET hyperflux_sdk)
    add_subdirectory("${CMAKE_CURRENT_SOURCE_DIR}/../../sdk/cpp" "${CMAKE_CURRENT_BINARY_DIR}/sdk")
endif()

add_library(
    hyperflux_openrgb_core
    STATIC
        src/controller_model.cpp
        src/dispatch_queue.cpp
        src/native_command.cpp
        src/runtime_bridge.cpp
        src/runtime_core.cpp
        src/runtime_dispatch.cpp
        src/runtime_events.cpp
        src/runtime_sessions.cpp
        src/runtime_worker.cpp
)
set_target_properties(hyperflux_openrgb_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_features(hyperflux_openrgb_core PUBLIC cxx_std_20)
target_include_directories(
    hyperflux_openrgb_core
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(hyperflux_openrgb_core PUBLIC hyperflux_sdk)
hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_core)

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
    hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_razer_registry)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux_openrgb_razer_registry
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()

    add_library(
        hyperflux_openrgb_native_controller
        STATIC
            src/controller_registry.cpp
            src/native_controller.cpp
            src/plugin_coordinator.cpp
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController/RGBController.cpp"
    )
    set_target_properties(
        hyperflux_openrgb_native_controller
        PROPERTIES POSITION_INDEPENDENT_CODE ON
    )
    target_compile_features(hyperflux_openrgb_native_controller PUBLIC cxx_std_20)
    target_include_directories(
        hyperflux_openrgb_native_controller
        PUBLIC
            "${CMAKE_CURRENT_SOURCE_DIR}/include"
            "${HFX_OPENRGB_SOURCE_DIR}"
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
    )
    target_link_libraries(
        hyperflux_openrgb_native_controller
        PUBLIC hyperflux_openrgb_razer_registry
    )
    hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_native_controller)
    if(HFX_OPENRGB_THREAD_SANITIZER)
        # OpenRGB starts RGBController's worker from its base constructor. Keep
        # that pinned dependency unit outside our race-ownership boundary while
        # instrumenting the HyperFlux-derived controller and registry around it.
        set_source_files_properties(
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController/RGBController.cpp"
            PROPERTIES COMPILE_OPTIONS "-fno-sanitize=thread"
        )
    endif()
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux_openrgb_native_controller
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
        hfx_openrgb_enable_thread_sanitizer(openrgb-razer-registry-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-razer-registry-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(NAME openrgb-razer-registry-contract COMMAND openrgb-razer-registry-contract)

        add_executable(
            openrgb-native-controller-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_native_controller_contract.cpp"
        )
        target_link_libraries(
            openrgb-native-controller-contract
            PRIVATE hyperflux_openrgb_native_controller
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-native-controller-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-native-controller-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-native-controller-contract
            COMMAND openrgb-native-controller-contract
        )

        add_executable(
            openrgb-controller-registry-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_controller_registry_contract.cpp"
        )
        target_link_libraries(
            openrgb-controller-registry-contract
            PRIVATE hyperflux_openrgb_native_controller Threads::Threads
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-controller-registry-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-controller-registry-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-controller-registry-contract
            COMMAND openrgb-controller-registry-contract
        )

        add_executable(
            openrgb-plugin-coordinator-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_plugin_coordinator_contract.cpp"
        )
        target_link_libraries(
            openrgb-plugin-coordinator-contract
            PRIVATE hyperflux_openrgb_native_controller Threads::Threads
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-plugin-coordinator-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-plugin-coordinator-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-plugin-coordinator-contract
            COMMAND openrgb-plugin-coordinator-contract
        )
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
    hfx_openrgb_enable_thread_sanitizer(openrgb-controller-model-contract)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-controller-model-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(NAME openrgb-controller-model-contract COMMAND openrgb-controller-model-contract)

    add_executable(
        openrgb-dispatch-queue-contract
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_dispatch_queue_contract.cpp"
    )
    target_link_libraries(openrgb-dispatch-queue-contract PRIVATE hyperflux_openrgb_core)
    hfx_openrgb_enable_thread_sanitizer(openrgb-dispatch-queue-contract)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-dispatch-queue-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(NAME openrgb-dispatch-queue-contract COMMAND openrgb-dispatch-queue-contract)

    add_executable(
        openrgb-runtime-core-contract
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_runtime_core_contract.cpp"
    )
    target_link_libraries(openrgb-runtime-core-contract PRIVATE hyperflux_openrgb_core)
    hfx_openrgb_enable_thread_sanitizer(openrgb-runtime-core-contract)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-runtime-core-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(NAME openrgb-runtime-core-contract COMMAND openrgb-runtime-core-contract)

    add_executable(
        openrgb-runtime-worker-contract
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_runtime_worker_contract.cpp"
    )
    target_link_libraries(openrgb-runtime-worker-contract PRIVATE hyperflux_openrgb_core)
    hfx_openrgb_enable_thread_sanitizer(openrgb-runtime-worker-contract)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-runtime-worker-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(NAME openrgb-runtime-worker-contract COMMAND openrgb-runtime-worker-contract)
endif()
