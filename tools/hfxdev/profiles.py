# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from .profiles_common import (
    PROFILE_DIRECTORIES,
    WRITE_EVIDENCE_LEVELS,
    PUBLIC_EVIDENCE,
    PROFILE_KEYS,
    FORBIDDEN_COMBINATION_KEYS,
    RECEIVER_COMPATIBILITY_KEYS,
    CHILD_COMPATIBILITY_KEYS,
    SURFACE_COMPATIBILITY_KEYS,
    ROUTE_KINDS,
    PRESENTATION_KEYS,
    PRESENTATION_VARIANT_ROUTES,
    PASSIVE_KEYS,
    RECEIVER_TRANSPORT_KEYS,
    NATIVE_EXCHANGE_KEYS,
    NATIVE_EXCHANGE_REQUIRED_KEYS,
    NATIVE_MATCH_RANGE_KEYS,
    NATIVE_ROUTE_SELECTOR_KEYS,
    NATIVE_REPORT_FORMAT_KEYS,
    NATIVE_BYTE_VALUE_KEYS,
    NATIVE_BYTE_RANGE_KEYS,
    NATIVE_DATA_SIZE_KEYS,
    NATIVE_CHECKSUM_KEYS,
    NATIVE_TRANSACTION_ID_KEYS,
    LIFECYCLE_KEYS,
    LIFECYCLE_REQUIRED_KEYS,
    POWER_OFF_DEPARTURE_EVIDENCE,
    RETURN_EVIDENCE_SOURCES,
    LIFECYCLE_POLICY_KEYS,
    LIFECYCLE_TIMING_AUTHORITY_KEYS,
    LIFECYCLE_POLICY_STRATEGIES,
    LIFECYCLE_POLICY_QUALIFICATIONS,
    LIFECYCLE_TIMING_AUTHORITY_KINDS,
    MAX_RETURN_RETRY_ATTEMPTS,
    MAX_RETURN_RETRY_WINDOW_MS,
    MAX_QUIESCENCE_CANDIDATE_MS,
    QUALIFICATION_MIGRATION_KEYS,
    QUALIFICATION_MIGRATION_STAGE_ROLES,
    PASSIVE_TELEMETRY_CAPABILITIES,
    UPSTREAM_ID_PATTERN,
    PRESENTATION_KEY_PATTERN,
    GIT_COMMIT_PATTERN,
    DIGEST_PATTERN,
    ProfileInputs,
    _canonical_bytes,
    _runtime_profile_digest,
    _expect_keys,
    _expect_nonempty_string,
    _expect_string_list,
    _expect_unique_string_list,
    _walk_keys,
)
from .profiles_capability import (
    _validate_compatibility,
    _validate_capabilities,
    _validate_candidates,
)
from .profiles_evidence import (
    _source_digest,
    _source_inventory,
    _load_documents,
    _validate_evidence,
)
from .profiles_lifecycle import (
    _validate_lifecycle_policy_catalog,
    _validate_lifecycle_policy,
    _validate_qualification_migrations,
)
from .profiles_presentation import (
    _validate_presentation,
    _validate_presentation_sources,
)
from .profiles_telemetry import (
    _validate_lighting,
    _raw_values,
    _validate_native_exchange,
    _validate_passive,
)
from .profiles_compilation import (
    _validate_profiles,
    load_profile_inputs,
    compiled_catalog,
    composition_fixtures,
)
