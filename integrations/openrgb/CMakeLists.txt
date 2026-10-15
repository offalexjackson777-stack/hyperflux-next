cmake_minimum_required(VERSION 3.25)
project(hyperflux_openrgb LANGUAGES CXX)

include(CTest)
find_package(Threads REQUIRED)

get_filename_component(
    HFX_REPOSITORY_ROOT
    "${CMAKE_CURRENT_SOURCE_DIR}/../.."
    ABSOLUTE
)

file(READ "${HFX_REPOSITORY_ROOT}/runtime/linux.json" HFX_LINUX_RUNTIME_JSON)
string(JSON HFX_LINUX_RUNTIME_SCHEMA GET "${HFX_LINUX_RUNTIME_JSON}" schema)
if(NOT HFX_LINUX_RUNTIME_SCHEMA STREQUAL "hyperflux-linux-runtime-v1")
    message(FATAL_ERROR "runtime/linux.json uses an unsupported schema")
endif()
string(
    JSON HFX_BRIDGE_SERVICE_ACCOUNT
    GET "${HFX_LINUX_RUNTIME_JSON}" bridge service_account
)
string(
    JSON HFX_BRIDGE_RUNTIME_DIRECTORY
    GET "${HFX_LINUX_RUNTIME_JSON}" bridge runtime_directory
)
string(
    JSON HFX_BRIDGE_SOCKET_NAME
    GET "${HFX_LINUX_RUNTIME_JSON}" bridge socket_name
)
string(
    JSON HFX_DESKTOP_USER_RUNTIME_ROOT
    GET "${HFX_LINUX_RUNTIME_JSON}" desktop_access user_runtime_root
)
string(
    JSON HFX_DESKTOP_BRIDGE_SOCKET_NAME
    GET "${HFX_LINUX_RUNTIME_JSON}" desktop_access bridge_socket_name
)

file(
    STRINGS "${HFX_REPOSITORY_ROOT}/Cargo.toml" HFX_COMPONENT_VERSION_LINES
    REGEX "^version = \"[^\"]+\"$"
)
list(LENGTH HFX_COMPONENT_VERSION_LINES HFX_COMPONENT_VERSION_LINE_COUNT)
if(NOT HFX_COMPONENT_VERSION_LINE_COUNT EQUAL 1)
    message(FATAL_ERROR "Cargo.toml must contain exactly one workspace package version")
endif()
list(GET HFX_COMPONENT_VERSION_LINES 0 HFX_COMPONENT_VERSION_LINE)
string(
    REGEX REPLACE "^version = \"([^\"]+)\"$" "\\1"
    HFX_COMPONENT_VERSION "${HFX_COMPONENT_VERSION_LINE}"
)

set(
    HFX_SOURCE_REVISION "development"
    CACHE STRING "Source revision embedded in the OpenRGB plugin"
)
if(HFX_SOURCE_REVISION STREQUAL "")
    message(FATAL_ERROR "HFX_SOURCE_REVISION may not be empty")
endif()

set(HFX_OPENRGB_GENERATED_INCLUDE_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}/hyperflux/openrgb")
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/include/hyperflux/openrgb/build_config.hpp.in"
    "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}/hyperflux/openrgb/build_config.hpp"
    @ONLY
)

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
    # Static libraries carry instrumented objects but do not perform a link of
    # their own. Export the runtime requirement to every final consumer.
    target_link_options(${target} PUBLIC -fsanitize=thread)
endfunction()

if(NOT TARGET hyperflux_sdk)
    add_subdirectory("${CMAKE_CURRENT_SOURCE_DIR}/../../sdk/cpp" "${CMAKE_CURRENT_BINARY_DIR}/sdk")
endif()

add_library(
    hyperflux_openrgb_core
    STATIC
        src/client_identity.cpp
        src/controller_model.cpp
        src/dispatch_queue.cpp
        src/inventory_model.cpp
        src/native_command.cpp
        src/plugin_runtime.cpp
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
        "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}"
)
target_link_libraries(hyperflux_openrgb_core PUBLIC hyperflux_sdk)
hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_core)

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(hyperflux_openrgb_core PRIVATE -Wall -Wextra -Werror -pedantic)
endif()

file(READ "${HFX_REPOSITORY_ROOT}/integrations/catalog.json" HFX_INTEGRATION_CATALOG_JSON)
string(JSON HFX_UPSTREAM_COUNT LENGTH "${HFX_INTEGRATION_CATALOG_JSON}" upstreams)
math(EXPR HFX_UPSTREAM_LAST "${HFX_UPSTREAM_COUNT} - 1")
foreach(HFX_UPSTREAM_INDEX RANGE ${HFX_UPSTREAM_LAST})
    string(
        JSON HFX_UPSTREAM_ID
        GET "${HFX_INTEGRATION_CATALOG_JSON}" upstreams ${HFX_UPSTREAM_INDEX} id
    )
    if(HFX_UPSTREAM_ID STREQUAL "openrgb")
        string(
            JSON HFX_OPENRGB_PINNED_VERSION
            GET "${HFX_INTEGRATION_CATALOG_JSON}" upstreams ${HFX_UPSTREAM_INDEX} version
        )
        string(
            JSON HFX_OPENRGB_PINNED_COMMIT
            GET "${HFX_INTEGRATION_CATALOG_JSON}" upstreams ${HFX_UPSTREAM_INDEX} commit
        )
    endif()
endforeach()
if(NOT HFX_OPENRGB_PINNED_VERSION OR NOT HFX_OPENRGB_PINNED_COMMIT)
    message(FATAL_ERROR "integrations/catalog.json does not pin OpenRGB")
endif()

set(HFX_OPENRGB_SOURCE_DIR "" CACHE PATH "Pinned OpenRGB source directory")
if(NOT HFX_OPENRGB_SOURCE_DIR AND DEFINED ENV{HFX_OPENRGB_SOURCE_DIR})
    set(HFX_OPENRGB_SOURCE_DIR "$ENV{HFX_OPENRGB_SOURCE_DIR}")
endif()

set(
    HFX_OPENRGB_NATIVE_SOURCE_DIR ""
    CACHE PATH "Source-bound OpenRGB tree with the HyperFlux feature transport seam"
)
if(NOT HFX_OPENRGB_NATIVE_SOURCE_DIR AND DEFINED ENV{HFX_OPENRGB_NATIVE_SOURCE_DIR})
    set(HFX_OPENRGB_NATIVE_SOURCE_DIR "$ENV{HFX_OPENRGB_NATIVE_SOURCE_DIR}")
endif()

if(HFX_OPENRGB_SOURCE_DIR)
    find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Widgets)

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
    if(NOT HFX_OPENRGB_SOURCE_REVISION STREQUAL HFX_OPENRGB_PINNED_COMMIT)
        message(FATAL_ERROR "OpenRGB source revision does not match the integration catalog")
    endif()

    if(HFX_OPENRGB_NATIVE_SOURCE_DIR)
        file(
            READ
            "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/hyperflux-native-transform-result.json"
            HFX_OPENRGB_NATIVE_TRANSFORM_JSON
        )
        string(
            JSON HFX_OPENRGB_NATIVE_TRANSFORM_SCHEMA
            GET "${HFX_OPENRGB_NATIVE_TRANSFORM_JSON}" schema
        )
        string(
            JSON HFX_OPENRGB_NATIVE_SOURCE_REVISION
            GET "${HFX_OPENRGB_NATIVE_TRANSFORM_JSON}" source_commit
        )
        if(NOT HFX_OPENRGB_NATIVE_TRANSFORM_SCHEMA STREQUAL
            "hyperflux-openrgb-native-transform-result-v1")
            message(FATAL_ERROR "OpenRGB native source binding schema is invalid")
        endif()
        if(NOT HFX_OPENRGB_NATIVE_SOURCE_REVISION STREQUAL
            HFX_OPENRGB_PINNED_COMMIT)
            message(FATAL_ERROR "OpenRGB native source binding has the wrong revision")
        endif()

        add_library(
            hyperflux_openrgb_razer_feature_transport
            STATIC
                src/native_route_session.cpp
                src/native_route_worker.cpp
                src/razer_feature_transport.cpp
        )
        set_target_properties(
            hyperflux_openrgb_razer_feature_transport
            PROPERTIES POSITION_INDEPENDENT_CODE ON
        )
        target_compile_features(
            hyperflux_openrgb_razer_feature_transport PUBLIC cxx_std_20
        )
        target_include_directories(
            hyperflux_openrgb_razer_feature_transport
            PUBLIC
                "${CMAKE_CURRENT_SOURCE_DIR}/include"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/RGBController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/KeyboardLayoutManager"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/dependencies/json"
                "/usr/include/hidapi"
        )
        hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_razer_feature_transport)
        target_link_libraries(
            hyperflux_openrgb_razer_feature_transport
            PUBLIC hyperflux_openrgb_core
        )
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                hyperflux_openrgb_razer_feature_transport
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()

        find_library(
            HFX_HIDAPI_LIBRARY
            NAMES hidapi-hidraw hidapi-libusb
            REQUIRED
        )
        add_library(
            hyperflux_openrgb_upstream_device_guard
            STATIC
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop/DeviceGuard.cpp"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop/DeviceGuardLock.cpp"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop/DeviceGuardManager.cpp"
        )
        set_target_properties(
            hyperflux_openrgb_upstream_device_guard
            PROPERTIES POSITION_INDEPENDENT_CODE ON
        )
        target_compile_features(
            hyperflux_openrgb_upstream_device_guard PUBLIC cxx_std_20
        )
        target_include_directories(
            hyperflux_openrgb_upstream_device_guard
            PUBLIC
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop"
        )
        target_link_libraries(
            hyperflux_openrgb_upstream_device_guard
            PUBLIC Threads::Threads
        )
        add_library(
            hyperflux_openrgb_native_razer_upstream
            STATIC
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerController/RazerController.cpp"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerDeviceGuard.cpp"
        )
        set_target_properties(
            hyperflux_openrgb_native_razer_upstream
            PROPERTIES POSITION_INDEPENDENT_CODE ON
        )
        target_compile_features(
            hyperflux_openrgb_native_razer_upstream PUBLIC cxx_std_20
        )
        target_include_directories(
            hyperflux_openrgb_native_razer_upstream
            PUBLIC
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/RGBController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/KeyboardLayoutManager"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/dependencies/json"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop"
                "/usr/include/hidapi"
        )
        target_link_libraries(
            hyperflux_openrgb_native_razer_upstream
            PUBLIC
                hyperflux_openrgb_razer_feature_transport
                hyperflux_openrgb_razer_registry
                hyperflux_openrgb_upstream_device_guard
                "${HFX_HIDAPI_LIBRARY}"
        )
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                hyperflux_openrgb_native_razer_upstream
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
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

    set(HFX_OPENRGB_CONTROLLER_SOURCE_DIR "${HFX_OPENRGB_SOURCE_DIR}")
    if(HFX_OPENRGB_NATIVE_SOURCE_DIR)
        set(HFX_OPENRGB_CONTROLLER_SOURCE_DIR "${HFX_OPENRGB_NATIVE_SOURCE_DIR}")
    endif()

    add_library(
        hyperflux_openrgb_native_controller
        STATIC
            src/controller_registry.cpp
            src/device_thread_barrier.cpp
            src/native_controller.cpp
            src/plugin_coordinator.cpp
            "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController/RGBController.cpp"
            "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/StringUtils.cpp"
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
            "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}"
            "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController"
    )
    target_link_libraries(
        hyperflux_openrgb_native_controller
        PUBLIC hyperflux_openrgb_razer_registry
    )
    # Upstream source: diagnostics stay visible but cannot fail the build on
    # toolchain-specific false positives (GCC 16 -Warray-bounds in nlohmann).
    set_property(
        SOURCE "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController/RGBController.cpp"
        APPEND PROPERTY COMPILE_OPTIONS "-Wno-error"
    )
    hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_native_controller)
    if(HFX_OPENRGB_THREAD_SANITIZER)
        # The pinned base starts a virtual worker in its constructor. Its
        # source-bound flag initialization and annotations model the real
        # atomic handoff while excluding that upstream vptr lifetime pattern.
        target_compile_definitions(
            hyperflux_openrgb_native_controller
            PRIVATE HFX_OPENRGB_TSAN_ANNOTATIONS=1
        )
        set_property(
            SOURCE "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController/RGBController.cpp"
            APPEND PROPERTY COMPILE_OPTIONS "-fno-sanitize=thread"
        )
    endif()
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux_openrgb_native_controller
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()

    if(TARGET hyperflux_openrgb_native_razer_upstream)
        add_library(
            hyperflux_openrgb_upstream_razer_registry
            STATIC
                src/upstream_razer_registry.cpp
                src/upstream_razer_coordinator.cpp
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerController/RGBController_Razer.cpp"
        )
        set_target_properties(
            hyperflux_openrgb_upstream_razer_registry
            PROPERTIES POSITION_INDEPENDENT_CODE ON
        )
        target_compile_features(
            hyperflux_openrgb_upstream_razer_registry PUBLIC cxx_std_20
        )
        target_include_directories(
            hyperflux_openrgb_upstream_razer_registry
            PUBLIC
                "${CMAKE_CURRENT_SOURCE_DIR}/include"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/RGBController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/KeyboardLayoutManager"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerController"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/dependencies/json"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/interop"
        )
        target_link_libraries(
            hyperflux_openrgb_upstream_razer_registry
            PUBLIC
                hyperflux_openrgb_native_razer_upstream
                hyperflux_openrgb_native_controller
        )
        hfx_openrgb_enable_thread_sanitizer(
            hyperflux_openrgb_upstream_razer_registry
        )
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                hyperflux_openrgb_upstream_razer_registry
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
    endif()

    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/hyperflux-next-openrgb.json.in"
        "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}/hyperflux-next-openrgb.json"
        @ONLY
    )

    add_library(
        hyperflux_openrgb_qt_shell
        STATIC
            src/plugin_application.cpp
            src/plugin_view_model.cpp
            src/qt_dispatcher.cpp
            src/virtual_controller_mirror.cpp
    )
    if(TARGET hyperflux_openrgb_upstream_razer_registry)
        target_sources(
            hyperflux_openrgb_qt_shell
            PRIVATE src/native_plugin_application.cpp
        )
        target_compile_definitions(
            hyperflux_openrgb_qt_shell
            PUBLIC HFX_OPENRGB_NATIVE_ROUTE_ENABLED=1
        )
    else()
        target_compile_definitions(
            hyperflux_openrgb_qt_shell
            PUBLIC HFX_OPENRGB_NATIVE_ROUTE_ENABLED=0
        )
    endif()
    set_target_properties(
        hyperflux_openrgb_qt_shell
        PROPERTIES POSITION_INDEPENDENT_CODE ON
    )
    target_compile_features(hyperflux_openrgb_qt_shell PUBLIC cxx_std_20)
    target_include_directories(
        hyperflux_openrgb_qt_shell
        PUBLIC
            "${CMAKE_CURRENT_SOURCE_DIR}/include"
            "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}"
        PRIVATE
            "${HFX_OPENRGB_SOURCE_DIR}"
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
            "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
    )
    target_link_libraries(
        hyperflux_openrgb_qt_shell
        PUBLIC hyperflux_openrgb_native_controller Qt6::Core Qt6::Gui Qt6::Widgets
    )
    if(TARGET hyperflux_openrgb_upstream_razer_registry)
        target_link_libraries(
            hyperflux_openrgb_qt_shell
            PUBLIC hyperflux_openrgb_upstream_razer_registry
        )
    endif()
    hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_qt_shell)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux_openrgb_qt_shell
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()

    add_library(
        hyperflux_openrgb_qt_widget
        STATIC
            src/plugin_widget.cpp
    )
    set_target_properties(
        hyperflux_openrgb_qt_widget
        PROPERTIES POSITION_INDEPENDENT_CODE ON
    )
    target_compile_features(hyperflux_openrgb_qt_widget PUBLIC cxx_std_20)
    target_include_directories(
        hyperflux_openrgb_qt_widget
        PUBLIC
            "${CMAKE_CURRENT_SOURCE_DIR}/include"
            "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}"
        PRIVATE
            "${CMAKE_CURRENT_SOURCE_DIR}/src"
    )
    target_link_libraries(
        hyperflux_openrgb_qt_widget
        PUBLIC hyperflux_openrgb_qt_shell Qt6::Core Qt6::Gui Qt6::Widgets
    )
    hfx_openrgb_enable_thread_sanitizer(hyperflux_openrgb_qt_widget)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux_openrgb_qt_widget
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()

    add_library(
        hyperflux-next-openrgb
        MODULE
            src/openrgb_plugin.cpp
            "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}/hyperflux-next-openrgb.json"
    )
    set_target_properties(
        hyperflux-next-openrgb
        PROPERTIES
            AUTOMOC ON
            PREFIX ""
            OUTPUT_NAME "hyperflux-next-openrgb"
    )
    target_compile_features(hyperflux-next-openrgb PRIVATE cxx_std_20)
    target_include_directories(
        hyperflux-next-openrgb
        PRIVATE
            "${CMAKE_CURRENT_SOURCE_DIR}/include"
            "${HFX_OPENRGB_GENERATED_INCLUDE_DIR}"
            "${HFX_OPENRGB_SOURCE_DIR}"
            "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
            "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
    )
    target_link_libraries(
        hyperflux-next-openrgb
        PRIVATE hyperflux_openrgb_qt_widget Qt6::Core Qt6::Gui Qt6::Widgets
    )
    hfx_openrgb_enable_thread_sanitizer(hyperflux-next-openrgb)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            hyperflux-next-openrgb
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()

    if(BUILD_TESTING)
        add_executable(
            openrgb-client-identity-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_client_identity_contract.cpp"
        )
        target_link_libraries(
            openrgb-client-identity-contract
            PRIVATE hyperflux_openrgb_core Threads::Threads
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-client-identity-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-client-identity-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
            target_link_options(
                openrgb-client-identity-contract
                PRIVATE -Wl,--wrap=getrandom
            )
        endif()
        add_test(
            NAME openrgb-client-identity-contract
            COMMAND openrgb-client-identity-contract
        )

        if(TARGET hyperflux_openrgb_razer_feature_transport)
            add_executable(
                openrgb-razer-feature-transport-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_razer_feature_transport_contract.cpp"
            )
            target_link_libraries(
                openrgb-razer-feature-transport-contract
                PRIVATE hyperflux_openrgb_razer_feature_transport
            )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-razer-feature-transport-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-razer-feature-transport-contract
                COMMAND openrgb-razer-feature-transport-contract
            )

            add_executable(
                openrgb-native-route-session-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_native_route_session_contract.cpp"
            )
            target_link_libraries(
                openrgb-native-route-session-contract
                PRIVATE hyperflux_openrgb_razer_feature_transport
            )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-native-route-session-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-native-route-session-contract
                COMMAND openrgb-native-route-session-contract
            )

            add_executable(
                openrgb-native-route-worker-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_native_route_worker_contract.cpp"
            )
            target_link_libraries(
                openrgb-native-route-worker-contract
                PRIVATE hyperflux_openrgb_razer_feature_transport Threads::Threads
            )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-native-route-worker-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-native-route-worker-contract
                COMMAND openrgb-native-route-worker-contract
            )

            add_executable(
                openrgb-native-razer-upstream-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_native_razer_upstream_contract.cpp"
            )
            target_link_libraries(
                openrgb-native-razer-upstream-contract
                PRIVATE hyperflux_openrgb_native_razer_upstream
            )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-native-razer-upstream-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-native-razer-upstream-contract
                COMMAND openrgb-native-razer-upstream-contract
            )

            add_executable(
                openrgb-native-razer-presentation-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_native_razer_presentation_contract.cpp"
                "${HFX_OPENRGB_NATIVE_SOURCE_DIR}/Controllers/RazerController/RazerController/RGBController_Razer.cpp"
            )
            target_link_libraries(
                openrgb-native-razer-presentation-contract
                PRIVATE
                    hyperflux_openrgb_native_razer_upstream
                    hyperflux_openrgb_native_controller
                )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-native-razer-presentation-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-native-razer-presentation-contract
                COMMAND openrgb-native-razer-presentation-contract
            )

            add_executable(
                openrgb-upstream-razer-registry-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_upstream_razer_registry_contract.cpp"
            )
            target_link_libraries(
                openrgb-upstream-razer-registry-contract
                PRIVATE hyperflux_openrgb_upstream_razer_registry
            )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-upstream-razer-registry-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-upstream-razer-registry-contract
                COMMAND openrgb-upstream-razer-registry-contract
            )

            add_executable(
                openrgb-upstream-razer-coordinator-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_upstream_razer_coordinator_contract.cpp"
            )
            target_link_libraries(
                openrgb-upstream-razer-coordinator-contract
                PRIVATE hyperflux_openrgb_upstream_razer_registry Threads::Threads
            )
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-upstream-razer-coordinator-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-upstream-razer-coordinator-contract
                COMMAND openrgb-upstream-razer-coordinator-contract
            )

            add_executable(
                openrgb-native-plugin-application-contract
                "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_native_plugin_application_contract.cpp"
                "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController/RGBController_Virtual.cpp"
            )
            target_include_directories(
                openrgb-native-plugin-application-contract
                PRIVATE
                    "${HFX_OPENRGB_SOURCE_DIR}"
                    "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
                    "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
            )
            target_link_libraries(
                openrgb-native-plugin-application-contract
                PRIVATE hyperflux_openrgb_qt_shell
            )
            hfx_openrgb_enable_thread_sanitizer(openrgb-native-plugin-application-contract)
            if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
                target_compile_options(
                    openrgb-native-plugin-application-contract
                    PRIVATE -Wall -Wextra -Werror -pedantic
                )
            endif()
            add_test(
                NAME openrgb-native-plugin-application-contract
                COMMAND openrgb-native-plugin-application-contract
            )
        endif()

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

        add_executable(
            openrgb-plugin-runtime-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_plugin_runtime_contract.cpp"
        )
        target_link_libraries(
            openrgb-plugin-runtime-contract
            PRIVATE hyperflux_openrgb_native_controller
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-plugin-runtime-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-plugin-runtime-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-plugin-runtime-contract
            COMMAND openrgb-plugin-runtime-contract
        )

        add_executable(
            openrgb-plugin-application-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_plugin_application_contract.cpp"
        )
        target_link_libraries(
            openrgb-plugin-application-contract
            PRIVATE hyperflux_openrgb_qt_shell Qt6::Core
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-plugin-application-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-plugin-application-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-plugin-application-contract
            COMMAND openrgb-plugin-application-contract
        )

        add_executable(
            openrgb-qt-dispatcher-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_qt_dispatcher_contract.cpp"
        )
        target_include_directories(
            openrgb-qt-dispatcher-contract
            PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src"
        )
        target_link_libraries(
            openrgb-qt-dispatcher-contract
            PRIVATE hyperflux_openrgb_qt_shell Qt6::Core Threads::Threads
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-qt-dispatcher-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-qt-dispatcher-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-qt-dispatcher-contract
            COMMAND openrgb-qt-dispatcher-contract
        )

        add_executable(
            openrgb-plugin-view-model-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_plugin_view_model_contract.cpp"
        )
        target_link_libraries(
            openrgb-plugin-view-model-contract
            PRIVATE hyperflux_openrgb_qt_shell
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-plugin-view-model-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-plugin-view-model-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-plugin-view-model-contract
            COMMAND openrgb-plugin-view-model-contract
        )

        add_executable(
            openrgb-plugin-widget-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_plugin_widget_contract.cpp"
        )
        target_include_directories(
            openrgb-plugin-widget-contract
            PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src"
        )
        target_link_libraries(
            openrgb-plugin-widget-contract
            PRIVATE hyperflux_openrgb_qt_widget
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-plugin-widget-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-plugin-widget-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-plugin-widget-contract
            COMMAND openrgb-plugin-widget-contract
        )
        set_tests_properties(
            openrgb-plugin-widget-contract
            PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
        )

        add_executable(
            openrgb-plugin-loader-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_plugin_loader_contract.cpp"
        )
        target_compile_features(openrgb-plugin-loader-contract PRIVATE cxx_std_20)
        target_include_directories(
            openrgb-plugin-loader-contract
            PRIVATE
                "${HFX_OPENRGB_SOURCE_DIR}"
                "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
                "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
        )
        target_link_libraries(
            openrgb-plugin-loader-contract
            PRIVATE Qt6::Core Qt6::Gui Qt6::Widgets
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-plugin-loader-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-plugin-loader-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-plugin-loader-contract
            COMMAND openrgb-plugin-loader-contract $<TARGET_FILE:hyperflux-next-openrgb>
        )
        set_tests_properties(
            openrgb-plugin-loader-contract
            PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
        )

        add_executable(
            openrgb-virtual-controller-mirror-contract
            "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_virtual_controller_mirror_contract.cpp"
            "${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController/RGBController_Virtual.cpp"
        )
        target_compile_features(openrgb-virtual-controller-mirror-contract PRIVATE cxx_std_20)
        target_include_directories(
            openrgb-virtual-controller-mirror-contract
            PRIVATE
                "${HFX_OPENRGB_SOURCE_DIR}"
                "${HFX_OPENRGB_SOURCE_DIR}/RGBController"
                "${HFX_OPENRGB_SOURCE_DIR}/dependencies/json"
        )
        target_link_libraries(
            openrgb-virtual-controller-mirror-contract
            PRIVATE hyperflux_openrgb_qt_shell
        )
        hfx_openrgb_enable_thread_sanitizer(openrgb-virtual-controller-mirror-contract)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(
                openrgb-virtual-controller-mirror-contract
                PRIVATE -Wall -Wextra -Werror -pedantic
            )
        endif()
        add_test(
            NAME openrgb-virtual-controller-mirror-contract
            COMMAND openrgb-virtual-controller-mirror-contract
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
        openrgb-inventory-model-contract
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_inventory_model_contract.cpp"
    )
    target_link_libraries(openrgb-inventory-model-contract PRIVATE hyperflux_openrgb_core)
    hfx_openrgb_enable_thread_sanitizer(openrgb-inventory-model-contract)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-inventory-model-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(NAME openrgb-inventory-model-contract COMMAND openrgb-inventory-model-contract)

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
        openrgb-multi-receiver-dispatch-contract
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/openrgb_multi_receiver_dispatch_contract.cpp"
    )
    target_link_libraries(
        openrgb-multi-receiver-dispatch-contract
        PRIVATE hyperflux_openrgb_core
    )
    hfx_openrgb_enable_thread_sanitizer(openrgb-multi-receiver-dispatch-contract)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(
            openrgb-multi-receiver-dispatch-contract
            PRIVATE -Wall -Wextra -Werror -pedantic
        )
    endif()
    add_test(
        NAME openrgb-multi-receiver-dispatch-contract
        COMMAND openrgb-multi-receiver-dispatch-contract
    )

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
