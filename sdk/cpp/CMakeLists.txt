cmake_minimum_required(VERSION 3.25)
project(hyperflux_sdk LANGUAGES CXX)

include(CTest)

add_library(
    hyperflux_sdk
    STATIC
        src/channel.cpp
        src/client.cpp
        src/identity.cpp
)
set_target_properties(hyperflux_sdk PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_features(hyperflux_sdk PUBLIC cxx_std_20)
target_include_directories(
    hyperflux_sdk
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${CMAKE_CURRENT_SOURCE_DIR}/vendor/include"
)

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(hyperflux_sdk PRIVATE -Wall -Wextra -Werror -pedantic)
endif()

if(BUILD_TESTING)
    function(add_hyperflux_sdk_contract target source)
        add_executable(${target} "${CMAKE_CURRENT_SOURCE_DIR}/../../tests/cpp/${source}")
        target_link_libraries(${target} PRIVATE hyperflux_sdk)
        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            target_compile_options(${target} PRIVATE -Wall -Wextra -Werror -pedantic)
        endif()
        add_test(NAME ${target} COMMAND ${target})
    endfunction()

    add_hyperflux_sdk_contract(domain-types-contract domain_types_smoke.cpp)
    add_hyperflux_sdk_contract(integration-catalog-contract integration_catalog_smoke.cpp)
    add_hyperflux_sdk_contract(protocol-json-contract protocol_json_contract.cpp)
    add_hyperflux_sdk_contract(sdk-client-contract sdk_client_contract.cpp)
    add_hyperflux_sdk_contract(sdk-channel-contract sdk_channel_contract.cpp)
endif()
