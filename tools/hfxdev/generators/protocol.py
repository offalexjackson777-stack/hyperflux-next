# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from .domain import HEADER_CPP, HEADER_PYTHON, HEADER_RUST
from ..model import ModelError
from ..protocol import FieldSpec, ProtocolCatalog, ProtocolRegistry, RecordSpec, UnionSpec


def _pascal(identifier: str) -> str:
    return "".join(part.capitalize() for part in identifier.replace(".", "-").split("-"))


def _rust_type(field: FieldSpec) -> str:
    base = "bool" if field.type_name == "Boolean" else field.type_name
    if field.many:
        return f"Vec<{base}>"
    if not field.required:
        return f"Option<{base}>"
    return base


def _cpp_type(field: FieldSpec) -> str:
    base = "bool" if field.type_name == "Boolean" else field.type_name
    if field.many:
        return f"std::vector<{base}>"
    if not field.required:
        return f"std::optional<{base}>"
    return base


def _python_type(field: FieldSpec) -> str:
    base = "bool" if field.type_name == "Boolean" else field.type_name
    if field.many:
        return f"tuple[{base}, ...]"
    if not field.required:
        return f"{base} | None"
    return base


def _declaration_order(
    catalog: ProtocolCatalog,
) -> list[tuple[str, RecordSpec | UnionSpec]]:
    internal_names = {record.name for record in catalog.records}
    internal_names.update(union.name for union in catalog.unions)
    pending: list[tuple[str, RecordSpec | UnionSpec]] = [
        *(('record', record) for record in catalog.records),
        *(('union', union) for union in catalog.unions),
    ]
    emitted: set[str] = set()
    ordered: list[tuple[str, RecordSpec | UnionSpec]] = []
    while pending:
        next_pending: list[tuple[str, RecordSpec | UnionSpec]] = []
        progressed = False
        for kind, declaration in pending:
            if kind == "record":
                assert isinstance(declaration, RecordSpec)
                dependencies = {
                    field.type_name
                    for field in declaration.fields
                    if field.type_name in internal_names
                }
            else:
                assert isinstance(declaration, UnionSpec)
                dependencies = {
                    variant.type_name
                    for variant in declaration.variants
                    if variant.type_name in internal_names
                }
            if dependencies <= emitted:
                ordered.append((kind, declaration))
                emitted.add(declaration.name)
                progressed = True
            else:
                next_pending.append((kind, declaration))
        if not progressed:
            blocked = ", ".join(declaration.name for _, declaration in next_pending)
            raise ModelError(f"recursive generated protocol type dependency: {blocked}")
        pending = next_pending
    return ordered


def _rust_domain_imports(names: list[str]) -> list[str]:
    lines = ["use hfx_domain::{"]
    current = "    "
    for name in names:
        addition = f"{name}, "
        if len(current) + len(addition) > 100:
            lines.append(current.rstrip())
            current = "    "
        current += addition
    lines.append(current.rstrip())
    lines.append("};")
    return lines


def _rust_match_pattern(variants: list[str], suffix: str) -> list[str]:
    pattern = " | ".join(f"Self::{variant}(envelope)" for variant in variants)
    candidate = f"            {pattern} {suffix}"
    if len(candidate) <= 100:
        return [candidate]
    lines = [f"            Self::{variants[0]}(envelope)"]
    lines.extend(f"            | Self::{variant}(envelope)" for variant in variants[1:])
    lines[-1] += f" {suffix}"
    return lines


def _rust_response_match_arm(variants: list[str], result: str) -> list[str]:
    return _rust_match_pattern(variants, f"=> {result},")


def rust_types(catalog: ProtocolCatalog) -> str:
    record_names = {record.name for record in catalog.records}
    union_names = {union.name for union in catalog.unions}
    domain_names = sorted(
        {
            field.type_name
            for record in catalog.records
            for field in record.fields
            if field.type_name != "Boolean"
            and field.type_name not in record_names
            and field.type_name not in union_names
        }
    )
    lines = [HEADER_RUST.rstrip(), ""]
    lines.extend(_rust_domain_imports(domain_names))
    lines.extend([
        "use serde::{Deserialize, Serialize};",
        "",
        f"pub const MINIMUM_PROTOCOL_VERSION: u16 = {catalog.minimum_version};",
        f"pub const MAXIMUM_PROTOCOL_VERSION: u16 = {catalog.maximum_version};",
        f"pub const MAX_WIRE_MESSAGE_BYTES: usize = {catalog.max_message_bytes:_};",
        f"pub const MAX_JSON_DEPTH: usize = {catalog.max_json_depth:_};",
        "pub const SUPPORTED_FEATURES: &[&str] = &[",
    ])
    lines.extend(f'    "{feature}",' for feature in catalog.features)
    lines.extend(["];"])
    for record in catalog.records:
        lines.extend([
            "",
            f"/// {record.description}",
            "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
            "#[serde(deny_unknown_fields)]",
        ])
        if not record.fields:
            lines.append(f"pub struct {record.name} {{}}")
            continue
        lines.append(f"pub struct {record.name} {{")
        for field in record.fields:
            lines.extend([f"    /// {field.description}", f"    pub {field.name}: {_rust_type(field)},"])
        lines.append("}")
    for union in catalog.unions:
        lines.extend([
            "",
            f"/// {union.description}",
            "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
            f'#[serde(tag = "{union.tag}", content = "{union.content}")]',
            f"pub enum {union.name} {{",
        ])
        for variant in union.variants:
            lines.extend([
                f"    /// {variant.description}",
                f'    #[serde(rename = "{variant.wire}")]',
                f"    {variant.name}({variant.type_name}),",
            ])
        lines.append("}")
    lines.extend([
        "",
        "/// Unnegotiated request envelope used only for the handshake.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct NegotiationRequestEnvelope {",
        "    pub request_id: RequestId,",
        "    pub params: ClientHello,",
        "}",
        "",
        "/// Request envelope bound to one negotiated bridge connection.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct SessionRequestEnvelope<T> {",
        "    pub request_id: RequestId,",
        "    pub protocol_session_id: ProtocolSessionId,",
        "    pub negotiation_token: NegotiationToken,",
        "    pub params: T,",
        "}",
        "",
        "/// Typed request envelope; arbitrary method strings or parameters are impossible.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(tag = \"method\", content = \"request\")]",
        "pub enum RpcRequest {",
    ])
    for method in catalog.methods:
        variant = _pascal(method.name)
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope<{method.request}>"
        )
        lines.extend([
            f'    #[serde(rename = "{method.name}")]',
            f"    {variant}({envelope}),",
        ])
    lines.extend([
        "}",
        "",
        "/// Successful method response bound to the serving bridge instance.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct SuccessEnvelope<T> {",
        "    pub request_id: RequestId,",
        "    pub server_instance_id: ServerInstanceId,",
        "    pub result: T,",
        "}",
        "",
        "/// Structured error response bound to the serving bridge instance.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(deny_unknown_fields)]",
        "pub struct ErrorEnvelope {",
        "    pub request_id: Option<RequestId>,",
        "    pub server_instance_id: ServerInstanceId,",
        "    pub error: RpcError,",
        "}",
        "",
        "/// Typed success-or-error response envelope.",
        "#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]",
        "#[serde(tag = \"type\", content = \"response\")]",
        "pub enum RpcResponse {",
    ])
    for method in catalog.methods:
        variant = f"{_pascal(method.name)}Success"
        lines.extend([
            f'    #[serde(rename = "{method.name}-success")]',
            f"    {variant}(SuccessEnvelope<{method.response}>),",
        ])
    lines.extend([
        "    #[serde(rename = \"error\")]",
        "    Error(ErrorEnvelope),",
        "}",
    ])
    lines.extend(["", "#[derive(Clone, Copy, Debug, Eq, PartialEq)]", "pub struct MethodDescriptor {", "    pub name: &'static str,", "    pub request: &'static str,", "    pub response: &'static str,", "    pub required_feature: Option<&'static str>,", "}", "", "pub const METHODS: &[MethodDescriptor] = &["])
    for method in catalog.methods:
        feature = "None" if method.required_feature is None else f'Some("{method.required_feature}")'
        lines.extend(
            [
                "    MethodDescriptor {",
                f'        name: "{method.name}",',
                f'        request: "{method.request}",',
                f'        response: "{method.response}",',
                f"        required_feature: {feature},",
                "    },",
            ]
        )
    lines.extend([
        "];",
        "",
        "impl RpcRequest {",
        "    /// Returns the request identity carried by every method envelope.",
        "    #[must_use]",
        "    pub fn request_id(&self) -> &RequestId {",
        "        match self {",
    ])
    request_groups: dict[str, list[str]] = {}
    for method in catalog.methods:
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope<{method.request}>"
        )
        request_groups.setdefault(envelope, []).append(_pascal(method.name))
    for variants in request_groups.values():
        lines.extend(_rust_match_pattern(variants, "=> &envelope.request_id,"))
    lines.extend([
        "        }",
        "    }",
        "",
        "    /// Returns the generated descriptor for this request variant.",
        "    #[must_use]",
        "    pub const fn method_descriptor(&self) -> &'static MethodDescriptor {",
        "        match self {",
    ])
    for index, method in enumerate(catalog.methods):
        variant = _pascal(method.name)
        lines.append(f"            Self::{variant}(_) => &METHODS[{index}],")
    lines.extend([
        "        }",
        "    }",
        "",
        "    /// Returns negotiated connection credentials for session-bound methods.",
        "    #[must_use]",
        "    pub fn session_credentials(&self) -> Option<(&ProtocolSessionId, &NegotiationToken)> {",
        "        match self {",
    ])
    negotiation_variant = _pascal(
        next(method.name for method in catalog.methods if method.name == "negotiate")
    )
    lines.append(f"            Self::{negotiation_variant}(_) => None,")
    session_groups: dict[str, list[str]] = {}
    for method in catalog.methods:
        if method.name != "negotiate":
            session_groups.setdefault(method.request, []).append(_pascal(method.name))
    for variants in session_groups.values():
        lines.extend(_rust_match_pattern(variants, "=> {"))
        lines.extend([
            "                Some((&envelope.protocol_session_id, &envelope.negotiation_token))",
            "            }",
        ])
    lines.extend([
        "        }",
        "    }",
        "}",
        "",
        "impl RpcResponse {",
        "    /// Returns the request identity carried by the response envelope.",
        "    #[must_use]",
        "    pub fn request_id(&self) -> Option<&RequestId> {",
        "        match self {",
    ])
    response_groups: dict[str, list[str]] = {}
    for method in catalog.methods:
        response_groups.setdefault(method.response, []).append(
            f"{_pascal(method.name)}Success"
        )
    for variants in response_groups.values():
        lines.extend(_rust_response_match_arm(variants, "Some(&envelope.request_id)"))
    lines.extend([
        "            Self::Error(envelope) => envelope.request_id.as_ref(),",
        "        }",
        "    }",
        "",
        "    /// Returns the bridge process identity carried by the response envelope.",
        "    #[must_use]",
        "    pub fn server_instance_id(&self) -> &ServerInstanceId {",
        "        match self {",
    ])
    for variants in response_groups.values():
        lines.extend(_rust_response_match_arm(variants, "&envelope.server_instance_id"))
    lines.extend([
        "            Self::Error(envelope) => &envelope.server_instance_id,",
        "        }",
        "    }",
        "}",
    ])
    return "\n".join(lines) + "\n"


def rust_registry(registry: ProtocolRegistry) -> str:
    lines = [HEADER_RUST.rstrip(), ""]
    for version in registry.versions:
        lines.extend(
            [
                f'#[path = "generated_v{version.version}.rs"]',
                f"pub mod v{version.version};",
            ]
        )
    lines.extend(
        [
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct ProtocolVersionDescriptor {",
            "    pub version: u16,",
            "    pub catalog_sha256: &'static str,",
            "    pub catalog_features: &'static [&'static str],",
            "    pub served_features: &'static [&'static str],",
            "}",
            "",
            f"pub const CURRENT_PROTOCOL_VERSION: u16 = {registry.current_version};",
            "pub const GENERATED_PROTOCOL_VERSIONS: &[ProtocolVersionDescriptor] = &[",
        ]
    )
    for version in registry.versions:
        lines.extend(
            [
                "    ProtocolVersionDescriptor {",
                f"        version: {version.version},",
                f'        catalog_sha256: "{version.source_sha256}",',
                f"        catalog_features: v{version.version}::SUPPORTED_FEATURES,",
                "        served_features: &[",
            ]
        )
        lines.extend(f'            "{feature}",' for feature in version.served_features)
        lines.extend(["        ],", "    },"])
    lines.extend(["];"])
    return "\n".join(lines) + "\n"


def cpp_types(catalog: ProtocolCatalog, namespace: str = "hyperflux") -> str:
    lines = [
        HEADER_CPP.rstrip(),
        "",
        "#pragma once",
        "",
        '#include "domain_types.hpp"',
        "",
        "#include <cstddef>",
        "#include <cstdint>",
        "#include <optional>",
        "#include <string_view>",
        "#include <variant>",
        "#include <vector>",
        "",
        f"namespace {namespace}",
        "{",
        "",
        f"inline constexpr std::uint16_t minimum_protocol_version = {catalog.minimum_version};",
        f"inline constexpr std::uint16_t maximum_protocol_version = {catalog.maximum_version};",
        f"inline constexpr std::size_t max_wire_message_bytes = {catalog.max_message_bytes};",
        f"inline constexpr std::size_t max_json_depth = {catalog.max_json_depth};",
    ]
    for kind, declaration in _declaration_order(catalog):
        if kind == "record":
            assert isinstance(declaration, RecordSpec)
            lines.extend([
                "",
                f"// {declaration.description}",
                f"struct {declaration.name}",
                "{",
            ])
            for field in declaration.fields:
                lines.append(f"    {_cpp_type(field)} {field.name};")
            lines.append(
                f"    friend bool operator==(const {declaration.name}&, "
                f"const {declaration.name}&) = default;"
            )
            lines.append("};")
            continue
        assert isinstance(declaration, UnionSpec)
        union = declaration
        alternatives: list[str] = []
        for variant in union.variants:
            alternative = f"{union.name}{variant.name}"
            alternatives.append(alternative)
            lines.extend([
                "",
                f"// {variant.description}",
                f"struct {alternative}",
                "{",
                f'    static constexpr std::string_view {union.tag} = "{variant.wire}";',
                f"    {variant.type_name} {union.content};",
                f"    friend bool operator==(const {alternative}&, const {alternative}&) = default;",
                "};",
            ])
        lines.extend([
            "",
            f"// {union.description}",
            f"using {union.name} = std::variant<{', '.join(alternatives)}>;",
        ])
    lines.extend([
        "",
        "struct NegotiationRequestEnvelope",
        "{",
        "    RequestId request_id;",
        "    ClientHello params;",
        "};",
        "",
        "template<typename T>",
        "struct SessionRequestEnvelope",
        "{",
        "    RequestId request_id;",
        "    ProtocolSessionId protocol_session_id;",
        "    NegotiationToken negotiation_token;",
        "    T params;",
        "};",
    ])
    request_alternatives: list[str] = []
    for method in catalog.methods:
        wrapper = f"RpcRequest{_pascal(method.name)}"
        request_alternatives.append(wrapper)
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope<{method.request}>"
        )
        lines.extend([
            "",
            f"struct {wrapper}",
            "{",
            f'    static constexpr std::string_view method = "{method.name}";',
            f"    {envelope} request;",
            "};",
        ])
    lines.extend([
        "",
        f"using RpcRequest = std::variant<{', '.join(request_alternatives)}>;",
        "",
        "template<typename T>",
        "struct SuccessEnvelope",
        "{",
        "    RequestId request_id;",
        "    ServerInstanceId server_instance_id;",
        "    T result;",
        "};",
        "",
        "struct ErrorEnvelope",
        "{",
        "    std::optional<RequestId> request_id;",
        "    ServerInstanceId server_instance_id;",
        "    RpcError error;",
        "};",
    ])
    response_alternatives: list[str] = []
    for method in catalog.methods:
        wrapper = f"RpcResponse{_pascal(method.name)}Success"
        response_alternatives.append(wrapper)
        lines.extend([
            "",
            f"struct {wrapper}",
            "{",
            f'    static constexpr std::string_view type = "{method.name}-success";',
            f"    SuccessEnvelope<{method.response}> response;",
            "};",
        ])
    response_alternatives.append("ErrorEnvelope")
    lines.extend([
        "",
        f"using RpcResponse = std::variant<{', '.join(response_alternatives)}>;",
    ])
    lines.extend(["", "struct MethodDescriptor", "{", "    std::string_view name;", "    std::string_view request;", "    std::string_view response;", "    std::optional<std::string_view> required_feature;", "};", "", f"inline constexpr MethodDescriptor methods[{len(catalog.methods)}] = {{"])
    for method in catalog.methods:
        feature = "std::nullopt" if method.required_feature is None else f'std::optional<std::string_view>("{method.required_feature}")'
        lines.append(
            f'    {{"{method.name}", "{method.request}", "{method.response}", {feature}}},'
        )
    lines.extend(["};", "", f"}} // namespace {namespace}", ""])
    return "\n".join(lines)


def cpp_json(
    catalog: ProtocolCatalog,
    namespace: str = "hyperflux",
    types_header: str = "protocol_types.hpp",
) -> str:
    record_names = {record.name for record in catalog.records}
    union_names = {union.name for union in catalog.unions}

    def qualify(type_name: str) -> str:
        if type_name == "Boolean":
            return "bool"
        if type_name in record_names or type_name in union_names:
            return f"::{namespace}::{type_name}"
        return f"::hyperflux::{type_name}"

    def record_type(name: str) -> str:
        return f"::{namespace}::{name}"

    def field_decode(field: FieldSpec) -> str:
        field_type = qualify(field.type_name)
        if field.many:
            return (
                f' decode_vector<{field_type}>(required_field(value, "{field.name}"), '
                f"{field.max_items})"
            )
        if field.required:
            return f' decode<{field_type}>(required_field(value, "{field.name}"))'
        return f' decode_optional_field<{field_type}>(value, "{field.name}")'

    lines = [
        HEADER_CPP.rstrip(),
        "",
        "#pragma once",
        "",
        '#include "domain_json.hpp"',
        f'#include "{types_header}"',
        "",
        "#include <string>",
        "#include <type_traits>",
        "#include <utility>",
        "#include <variant>",
        "",
        "namespace hyperflux::json_codec",
        "{",
    ]
    specialized_types = [record_type(record.name) for record in catalog.records]
    specialized_types.extend(record_type(union.name) for union in catalog.unions)
    specialized_types.extend(
        [
            record_type("NegotiationRequestEnvelope"),
            record_type("ErrorEnvelope"),
            record_type("RpcRequest"),
            record_type("RpcResponse"),
        ]
    )
    for type_name in specialized_types:
        lines.extend(
            [
                "",
                "template<>",
                f"[[nodiscard]] {type_name} decode<{type_name}>(const Json& value);",
                "",
                "template<>",
                f"[[nodiscard]] Json encode<{type_name}>(const {type_name}& value);",
            ]
        )
    for record in catalog.records:
        type_name = record_type(record.name)
        allowed = ", ".join(f'"{field.name}"' for field in record.fields)
        lines.extend(
            [
                "",
                "template<>",
                f"[[nodiscard]] inline {type_name} decode<{type_name}>(const Json& value)",
                "{",
                f"    require_object(value, {{{allowed}}});",
            ]
        )
        if record.fields:
            lines.append(f"    return {type_name} {{")
            lines.extend(f"       {field_decode(field)}," for field in record.fields)
            lines.append("    };")
        else:
            lines.append(f"    return {type_name} {{}};")
        lines.extend(
            [
                "}",
                "",
                "template<>",
                f"[[nodiscard]] inline Json encode<{type_name}>(const {type_name}& value)",
                "{",
                "    Json result = Json::object();",
            ]
        )
        if not record.fields:
            lines.append("    static_cast<void>(value);")
        for field in record.fields:
            if field.many:
                expression = f"encode_vector(value.{field.name})"
            elif not field.required:
                expression = (
                    f"value.{field.name}.has_value() "
                    f"? encode(*value.{field.name}) : Json(nullptr)"
                )
            else:
                expression = f"encode(value.{field.name})"
            lines.append(f'    result["{field.name}"] = {expression};')
        lines.extend(["    return result;", "}"])
    for union in catalog.unions:
        type_name = record_type(union.name)
        lines.extend(
            [
                "",
                "template<>",
                f"[[nodiscard]] inline {type_name} decode<{type_name}>(const Json& value)",
                "{",
                f'    require_object(value, {{"{union.tag}", "{union.content}"}});',
                f'    const auto& tag_value = required_field(value, "{union.tag}");',
                "    if(!tag_value.is_string())",
                "    {",
                f'        throw CodecError("{union.name} tag must be a string");',
                "    }",
                "    const auto tag = tag_value.get<std::string>();",
            ]
        )
        for variant in union.variants:
            alternative = record_type(f"{union.name}{variant.name}")
            content_type = qualify(variant.type_name)
            lines.extend(
                [
                    f'    if(tag == "{variant.wire}")',
                    "    {",
                    f"        return {type_name} {{{alternative} {{",
                    f'            decode<{content_type}>(required_field(value, "{union.content}"))',
                    "        }};",
                    "    }",
                ]
            )
        lines.extend(
            [
                f'    throw CodecError("unknown {union.name} tag");',
                "}",
                "",
                "template<>",
                f"[[nodiscard]] inline Json encode<{type_name}>(const {type_name}& value)",
                "{",
                "    return std::visit(",
                "        [](const auto& alternative) {",
                "            using Alternative = std::decay_t<decltype(alternative)>;",
                "            return Json {",
                f'                {{"{union.tag}", std::string(Alternative::{union.tag})}},',
                f'                {{"{union.content}", encode(alternative.{union.content})}},',
                "            };",
                "        },",
                "        value);",
                "}",
            ]
        )
    negotiation = record_type("NegotiationRequestEnvelope")
    lines.extend(
        [
            "",
            "template<>",
            f"[[nodiscard]] inline {negotiation} decode<{negotiation}>(const Json& value)",
            "{",
            '    require_object(value, {"request_id", "params"});',
            f"    return {negotiation} {{",
            '        decode<::hyperflux::RequestId>(required_field(value, "request_id")),',
            f'        decode<{record_type("ClientHello")}>(required_field(value, "params")),',
            "    };",
            "}",
            "",
            "template<>",
            f"[[nodiscard]] inline Json encode<{negotiation}>(const {negotiation}& value)",
            "{",
            "    return Json {",
            '        {"request_id", encode(value.request_id)},',
            '        {"params", encode(value.params)},',
            "    };",
            "}",
            "",
            "template<typename T>",
            f"[[nodiscard]] {record_type('SessionRequestEnvelope')}<T> decode_session_request(",
            "    const Json& value)",
            "{",
            '    require_object(value, {"request_id", "protocol_session_id", "negotiation_token", "params"});',
            f"    return {record_type('SessionRequestEnvelope')}<T> {{",
            '        decode<::hyperflux::RequestId>(required_field(value, "request_id")),',
            '        decode<::hyperflux::ProtocolSessionId>(required_field(value, "protocol_session_id")),',
            '        decode<::hyperflux::NegotiationToken>(required_field(value, "negotiation_token")),',
            '        decode<T>(required_field(value, "params")),',
            "    };",
            "}",
            "",
            "template<typename T>",
            "[[nodiscard]] Json encode_session_request(",
            f"    const {record_type('SessionRequestEnvelope')}<T>& value)",
            "{",
            "    return Json {",
            '        {"request_id", encode(value.request_id)},',
            '        {"protocol_session_id", encode(value.protocol_session_id)},',
            '        {"negotiation_token", encode(value.negotiation_token)},',
            '        {"params", encode(value.params)},',
            "    };",
            "}",
            "",
            "template<typename T>",
            f"[[nodiscard]] {record_type('SuccessEnvelope')}<T> decode_success(const Json& value)",
            "{",
            '    require_object(value, {"request_id", "server_instance_id", "result"});',
            f"    return {record_type('SuccessEnvelope')}<T> {{",
            '        decode<::hyperflux::RequestId>(required_field(value, "request_id")),',
            '        decode<::hyperflux::ServerInstanceId>(required_field(value, "server_instance_id")),',
            '        decode<T>(required_field(value, "result")),',
            "    };",
            "}",
            "",
            "template<typename T>",
            "[[nodiscard]] Json encode_success(",
            f"    const {record_type('SuccessEnvelope')}<T>& value)",
            "{",
            "    return Json {",
            '        {"request_id", encode(value.request_id)},',
            '        {"server_instance_id", encode(value.server_instance_id)},',
            '        {"result", encode(value.result)},',
            "    };",
            "}",
        ]
    )
    error_envelope = record_type("ErrorEnvelope")
    lines.extend(
        [
            "",
            "template<>",
            f"[[nodiscard]] inline {error_envelope} decode<{error_envelope}>(const Json& value)",
            "{",
            '    require_object(value, {"request_id", "server_instance_id", "error"});',
            f"    return {error_envelope} {{",
            '        decode_optional_field<::hyperflux::RequestId>(value, "request_id"),',
            '        decode<::hyperflux::ServerInstanceId>(required_field(value, "server_instance_id")),',
            f'        decode<{record_type("RpcError")}>(required_field(value, "error")),',
            "    };",
            "}",
            "",
            "template<>",
            f"[[nodiscard]] inline Json encode<{error_envelope}>(const {error_envelope}& value)",
            "{",
            "    return Json {",
            '        {"request_id", value.request_id.has_value() ? encode(*value.request_id) : Json(nullptr)},',
            '        {"server_instance_id", encode(value.server_instance_id)},',
            '        {"error", encode(value.error)},',
            "    };",
            "}",
        ]
    )
    request_type = record_type("RpcRequest")
    lines.extend(
        [
            "",
            "template<>",
            f"[[nodiscard]] inline {request_type} decode<{request_type}>(const Json& value)",
            "{",
            '    require_object(value, {"method", "request"});',
            '    const auto& method_value = required_field(value, "method");',
            "    if(!method_value.is_string())",
            "    {",
            '        throw CodecError("RPC request method must be a string");',
            "    }",
            "    const auto method = method_value.get<std::string>();",
            '    const auto& envelope = required_field(value, "request");',
        ]
    )
    for method in catalog.methods:
        wrapper = record_type(f"RpcRequest{_pascal(method.name)}")
        envelope_decode = (
            f"decode<{negotiation}>(envelope)"
            if method.name == "negotiate"
            else f"decode_session_request<{qualify(method.request)}>(envelope)"
        )
        lines.extend(
            [
                f'    if(method == "{method.name}")',
                "    {",
                f"        return {request_type} {{{wrapper} {{{envelope_decode}}}}};",
                "    }",
            ]
        )
    lines.extend(
        [
            '    throw CodecError("unknown RPC request method");',
            "}",
            "",
            "template<>",
            f"[[nodiscard]] inline Json encode<{request_type}>(const {request_type}& value)",
            "{",
            "    return std::visit(",
            "        [](const auto& request) {",
            "            using Request = std::decay_t<decltype(request)>;",
            "            Json envelope;",
        ]
    )
    negotiation_wrapper = record_type("RpcRequestNegotiate")
    lines.extend(
        [
            f"            if constexpr(std::is_same_v<Request, {negotiation_wrapper}>)",
            "            {",
            "                envelope = encode(request.request);",
            "            }",
            "            else",
            "            {",
            "                envelope = encode_session_request(request.request);",
            "            }",
            "            return Json {",
            '                {"method", std::string(Request::method)},',
            '                {"request", std::move(envelope)},',
            "            };",
            "        },",
            "        value);",
            "}",
        ]
    )
    response_type = record_type("RpcResponse")
    lines.extend(
        [
            "",
            "template<>",
            f"[[nodiscard]] inline {response_type} decode<{response_type}>(const Json& value)",
            "{",
            '    require_object(value, {"type", "response"});',
            '    const auto& type_value = required_field(value, "type");',
            "    if(!type_value.is_string())",
            "    {",
            '        throw CodecError("RPC response type must be a string");',
            "    }",
            "    const auto type = type_value.get<std::string>();",
            '    const auto& envelope = required_field(value, "response");',
        ]
    )
    for method in catalog.methods:
        wrapper = record_type(f"RpcResponse{_pascal(method.name)}Success")
        lines.extend(
            [
                f'    if(type == "{method.name}-success")',
                "    {",
                f"        return {response_type} {{{wrapper} {{",
                f"            decode_success<{qualify(method.response)}>(envelope)",
                "        }};",
                "    }",
            ]
        )
    lines.extend(
        [
            '    if(type == "error")',
            "    {",
            f"        return {response_type} {{decode<{error_envelope}>(envelope)}};",
            "    }",
            '    throw CodecError("unknown RPC response type");',
            "}",
            "",
            "template<>",
            f"[[nodiscard]] inline Json encode<{response_type}>(const {response_type}& value)",
            "{",
            "    return std::visit(",
            "        [](const auto& response) {",
            "            using Response = std::decay_t<decltype(response)>;",
            f"            if constexpr(std::is_same_v<Response, {error_envelope}>)",
            "            {",
            "                return Json {",
            '                    {"type", "error"},',
            '                    {"response", encode(response)},',
            "                };",
            "            }",
            "            else",
            "            {",
            "                return Json {",
            '                    {"type", std::string(Response::type)},',
            '                    {"response", encode_success(response.response)},',
            "                };",
            "            }",
            "        },",
            "        value);",
            "}",
            "",
            "} // namespace hyperflux::json_codec",
            "",
        ]
    )
    return "\n".join(lines)


def python_types(catalog: ProtocolCatalog) -> str:
    domain_names = sorted(
        {
            field.type_name
            for record in catalog.records
            for field in record.fields
            if field.type_name != "Boolean"
            and field.type_name not in {candidate.name for candidate in catalog.records}
            and field.type_name not in {candidate.name for candidate in catalog.unions}
        }
    )
    lines = [
        HEADER_PYTHON.rstrip(),
        "",
        "from dataclasses import dataclass",
        "from typing import ClassVar, Generic, TypeAlias, TypeVar",
        "",
        "from .domain_types import (",
    ]
    lines.extend(f"    {name}," for name in domain_names)
    lines.extend([
        ")",
        "",
        "T = TypeVar(\"T\")",
        "",
        f"MINIMUM_PROTOCOL_VERSION = {catalog.minimum_version}",
        f"MAXIMUM_PROTOCOL_VERSION = {catalog.maximum_version}",
        f"MAX_WIRE_MESSAGE_BYTES = {catalog.max_message_bytes}",
        f"MAX_JSON_DEPTH = {catalog.max_json_depth}",
        "FIELD_LIMITS = {",
    ])
    for record in catalog.records:
        for field in record.fields:
            if field.max_items is not None:
                lines.append(
                    f'    ("{record.name}", "{field.name}"): {field.max_items},'
                )
    lines.extend([
        "}",
        "SUPPORTED_FEATURES = (",
    ])
    lines.extend(f'    "{feature}",' for feature in catalog.features)
    lines.extend([")", ""])
    exports: list[str] = []
    for kind, declaration in _declaration_order(catalog):
        if kind == "record":
            assert isinstance(declaration, RecordSpec)
            exports.append(declaration.name)
            lines.extend([
                "@dataclass(frozen=True, slots=True)",
                f"class {declaration.name}:",
                f'    """{declaration.description}"""',
            ])
            if not declaration.fields:
                lines.append("    pass")
            else:
                for field in declaration.fields:
                    lines.append(f"    {field.name}: {_python_type(field)}")
            lines.extend(["", ""])
            continue
        assert isinstance(declaration, UnionSpec)
        union = declaration
        alternatives: list[str] = []
        for variant in union.variants:
            alternative = f"{union.name}{variant.name}"
            alternatives.append(alternative)
            exports.append(alternative)
            lines.extend([
                "@dataclass(frozen=True, slots=True)",
                f"class {alternative}:",
                f'    """{variant.description}"""',
                f'    {union.tag.upper()}: ClassVar[str] = "{variant.wire}"',
                f"    {union.content}: {variant.type_name}",
                "",
                "",
            ])
        exports.append(union.name)
        lines.extend([
            f"{union.name}: TypeAlias = {' | '.join(alternatives)}",
            "",
            "",
        ])
    exports.extend([
        "NegotiationRequestEnvelope",
        "SessionRequestEnvelope",
        "SuccessEnvelope",
        "ErrorEnvelope",
    ])
    lines.extend([
        "@dataclass(frozen=True, slots=True)",
        "class NegotiationRequestEnvelope:",
        "    request_id: RequestId",
        "    params: ClientHello",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class SessionRequestEnvelope(Generic[T]):",
        "    request_id: RequestId",
        "    protocol_session_id: ProtocolSessionId",
        "    negotiation_token: NegotiationToken",
        "    params: T",
        "",
        "",
    ])
    request_variants: list[str] = []
    for method in catalog.methods:
        variant = f"RpcRequest{_pascal(method.name)}"
        request_variants.append(variant)
        exports.append(variant)
        envelope = (
            "NegotiationRequestEnvelope"
            if method.name == "negotiate"
            else f"SessionRequestEnvelope[{method.request}]"
        )
        lines.extend([
            "@dataclass(frozen=True, slots=True)",
            f"class {variant}:",
            f'    METHOD: ClassVar[str] = "{method.name}"',
            f"    request: {envelope}",
            "",
            "",
        ])
    exports.append("RpcRequest")
    lines.extend([
        f"RpcRequest: TypeAlias = {' | '.join(request_variants)}",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class SuccessEnvelope(Generic[T]):",
        "    request_id: RequestId",
        "    server_instance_id: ServerInstanceId",
        "    result: T",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class ErrorEnvelope:",
        "    request_id: RequestId | None",
        "    server_instance_id: ServerInstanceId",
        "    error: RpcError",
        "",
        "",
    ])
    response_variants: list[str] = []
    for method in catalog.methods:
        variant = f"RpcResponse{_pascal(method.name)}Success"
        response_variants.append(variant)
        exports.append(variant)
        lines.extend([
            "@dataclass(frozen=True, slots=True)",
            f"class {variant}:",
            f'    TYPE: ClassVar[str] = "{method.name}-success"',
            f"    response: SuccessEnvelope[{method.response}]",
            "",
            "",
        ])
    exports.append("RpcResponse")
    response_variants.append("ErrorEnvelope")
    lines.extend([
        f"RpcResponse: TypeAlias = {' | '.join(response_variants)}",
        "",
        "",
    ])
    lines.extend(["METHODS = ("])
    for method in catalog.methods:
        feature = "None" if method.required_feature is None else f'"{method.required_feature}"'
        lines.append(
            f'    ("{method.name}", "{method.request}", "{method.response}", {feature}),'
        )
    lines.extend([
        ")",
        "",
        "__all__ = [",
        '    "MAXIMUM_PROTOCOL_VERSION",',
        '    "FIELD_LIMITS",',
        '    "MAX_JSON_DEPTH",',
        '    "MAX_WIRE_MESSAGE_BYTES",',
        '    "METHODS",',
        '    "MINIMUM_PROTOCOL_VERSION",',
        '    "SUPPORTED_FEATURES",',
    ])
    lines.extend(f'    "{name}",' for name in exports)
    lines.extend(["]", ""])
    return "\n".join(lines)


def markdown(catalog: ProtocolCatalog, title: str = "Bridge Protocol") -> str:
    lines = [
        f"# {title}",
        "",
        "> Generated by `./hfx generate`. Do not edit manually.",
        "",
        f"Supported protocol versions: **{catalog.minimum_version} through {catalog.maximum_version}**.",
        "",
        f"Maximum encoded message: **{catalog.max_message_bytes} bytes**. Maximum JSON nesting: "
        f"**{catalog.max_json_depth} levels**.",
        "",
        "## Clock And Ordering Semantics",
        "",
        "`MonotonicMs` values belong to the serving bridge process monotonic clock. They are "
        "comparable only while `server_instance_id` remains unchanged; they are never wall-clock "
        "timestamps and must not be retained across bridge restart.",
        "",
        "Lease expiries and transaction deadlines are exclusive absolute instants in that clock "
        "domain. Transaction `frame_index` values are contiguous, zero-based, and ordered within "
        "one atomic request. Terminal outcomes are immutable; a client looks them up instead of "
        "replaying a request after uncertain transport.",
        "",
        "## Negotiated Features",
        "",
    ]
    lines.extend(f"- `{feature}`" for feature in catalog.features)
    lines.extend(["", "## Methods", "", "| Method | Request | Response | Feature |", "| --- | --- | --- | --- |"])
    for method in catalog.methods:
        feature = f"`{method.required_feature}`" if method.required_feature else "base"
        lines.append(f"| `{method.name}` | `{method.request}` | `{method.response}` | {feature} |")
    lines.extend(["", "## Records", ""])
    for record in catalog.records:
        lines.extend([f"### {record.name}", "", record.description, ""])
        if not record.fields:
            lines.extend(["This record has no fields.", ""])
            continue
        lines.extend(["| Field | Type | Required | Bound |", "| --- | --- | --- | --- |"])
        for field in record.fields:
            bound = f"max {field.max_items}" if field.many else "scalar"
            lines.append(
                f"| `{field.name}` | `{field.type_name}` | `{str(field.required).lower()}` | {bound} |"
            )
        lines.append("")
    if catalog.unions:
        lines.extend(["## Tagged Unions", ""])
    for union in catalog.unions:
        lines.extend([f"### {union.name}", "", union.description, ""])
        lines.extend(["| Tag | Variant | Payload |", "| --- | --- | --- |"])
        for variant in union.variants:
            lines.append(
                f"| `{variant.wire}` | `{variant.name}` | `{variant.type_name}` |"
            )
        lines.append("")
    return "\n".join(lines)
