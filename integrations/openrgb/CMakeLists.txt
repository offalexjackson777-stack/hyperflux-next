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

set(HFX_OPENRGB_SOURCE_DIR "" CACHE PATH "Pinned OpenRGB 1.0rc3 source directory")
if(NOT HFX_OPENRGB_SOURCE_DIR AND DEFINED ENV{HFX_OPENRGB_SOURCE_DIR})
    set(HFX_OPENRGB_SOURCE_DIR "$ENV{HFX_OPENRGB_SOURCE_DIR}")
endif()

if(HFX_OPENRGB_SOURCE_DIR)
    find_package(Qt5 5.15 REQUIRED COMPONENTS Core Gui Widgets)

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
    )
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
            "${HFX_OPENRGB_SOURCE_DIR}/i2c_smbus"
    )
    target_link_libraries(
        hyperflux_openrgb_qt_shell
        PUBLIC hyperflux_openrgb_native_controller Qt5::Core
    )
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
        PUBLIC hyperflux_openrgb_qt_shell Qt5::Core Qt5::Gui Qt5::Widgets
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
            "${HFX_OPENRGB_SOURCE_DIR}/i2c_smbus"
    )
    target_link_libraries(
        hyperflux-next-openrgb
        PRIVATE hyperflux_openrgb_qt_widget Qt5::Core Qt5::Gui Qt5::Widgets
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
            PRIVATE hyperflux_openrgb_qt_shell Qt5::Core
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
            PRIVATE hyperflux_openrgb_qt_shell Qt5::Core Threads::Threads
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
                "${HFX_OPENRGB_SOURCE_DIR}/i2c_smbus"
        )
        target_link_libraries(
            openrgb-plugin-loader-contract
            PRIVATE Qt5::Core Qt5::Gui Qt5::Widgets
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
