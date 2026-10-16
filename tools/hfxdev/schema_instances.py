# SPDX-License-Identifier: GPL-2.0-only

"""Report how every tracked JSON document fares against the schema it declares.

This is stage one of JSON Schema enforcement: it validates with the real
`jsonschema` draft 2020-12 validator, writes a report and never fails on a
violation. The validator lives only in the pinned development image, so the
import is lazy and a missing module names the command that has it.

Report entries follow the privacy contract of `schema_subset`: a JSON path,
the failed keyword and the schema path, never an instance value. A path
segment that the schema does not name under `properties` (a map key, an
unknown key) is shown as `*`, so private keys never reach the report.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Protocol

from .model import ModelError, load_json, repository_files

REPORT_PATH = PurePosixPath("build/schema-instances-report.json")
REPORT_SCHEMA = "hyperflux-schema-instance-report-v1"
METASCHEMA_ID = "https://json-schema.org/draft/2020-12/schema"
INSTALLED_SCHEMA_ROOT = PurePosixPath("/usr/share/hyperflux-next/schemas")
CHECKED_FORMATS = ("date", "date-time", "uri")
MAX_RECORDED_VIOLATIONS = 50
REDACTED_SEGMENT = "*"
MISSING_VALIDATOR_MESSAGE = (
    "schema-instances needs the jsonschema module, which only the pinned "
    "development image carries; run it with ./hfx ci verify --lane full "
    "(or ./hfx verify, which uses that image)"
)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
# RFC 3986 absolute URI: a scheme, then only unreserved, reserved and
# percent-encoded characters.
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})*$")


class _ValidationError(Protocol):
    validator: Any
    absolute_path: Iterable[Any]
    absolute_schema_path: Iterable[Any]


class _Validator(Protocol):
    def iter_errors(self, instance: Any) -> Iterator[_ValidationError]: ...


ValidatorFactory = Callable[[dict[str, Any]], _Validator]


@dataclass(frozen=True)
class SchemaIndex:
    """Repository schemas by `$id`, and the file each id came from."""

    by_id: dict[str, dict[str, Any]]
    id_by_path: dict[str, str]


def is_date(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if _DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if _DATE_TIME.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return _URI.fullmatch(value) is not None


FORMAT_CHECKS: dict[str, Callable[[object], bool]] = {
    "date": is_date,
    "date-time": is_date_time,
    "uri": is_uri,
}


def _pointer(segments: Iterable[object]) -> str:
    parts = [str(segment).replace("~", "~0").replace("/", "~1") for segment in segments]
    return "/" + "/".join(parts) if parts else ""


def _schema_named_properties(schema_path: list[object]) -> set[str]:
    """Names the schema itself declares under a `properties` keyword."""
    names: set[str] = set()
    index = 0
    while index < len(schema_path):
        if schema_path[index] == "properties" and index + 1 < len(schema_path):
            names.add(str(schema_path[index + 1]))
            index += 2
        else:
            index += 1
    return names


def violation_record(error: _ValidationError) -> dict[str, str]:
    """A privacy-preserving description of one validation error.

    Uses only the error's instance path, keyword and schema path. Never the
    message, the instance or the failing value: those can hold private text
    or the names of unknown keys.
    """
    schema_path = list(error.absolute_schema_path)
    named = _schema_named_properties(schema_path)
    instance_path = [
        segment
        if isinstance(segment, int) and not isinstance(segment, bool)
        else (str(segment) if str(segment) in named else REDACTED_SEGMENT)
        for segment in error.absolute_path
    ]
    keyword = error.validator if isinstance(error.validator, str) else "unknown"
    return {
        "path": _pointer(instance_path),
        "keyword": keyword,
        "schema_path": _pointer(schema_path),
    }


def load_schema_index(root: Path, files: Iterable[Path]) -> SchemaIndex:
    by_id: dict[str, dict[str, Any]] = {}
    id_by_path: dict[str, str] = {}
    for path in files:
        if not path.name.endswith(".schema.json"):
            continue
        schema = load_json(path)
        schema_id = schema.get("$id") if isinstance(schema, dict) else None
        if not isinstance(schema_id, str):
            continue
        if schema_id in by_id:
            raise ModelError(f"{path.relative_to(root).as_posix()}: duplicate JSON Schema id")
        by_id[schema_id] = schema
        id_by_path[path.relative_to(root).as_posix()] = schema_id
    return SchemaIndex(by_id, id_by_path)


def _declared_schema_id(
    root: Path, relative: str, reference: str, index: SchemaIndex
) -> str | None:
    if reference == METASCHEMA_ID or reference in index.by_id:
        return reference
    if reference.startswith(("http://", "https://")):
        return None
    candidate = PurePosixPath(reference)
    if candidate.is_absolute():
        if candidate.parent != INSTALLED_SCHEMA_ROOT:
            return None
        target = f"schemas/{candidate.name}"
    else:
        resolved = (root / relative).parent.joinpath(reference).resolve()
        try:
            target = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    return index.id_by_path.get(target)


def build_report(
    root: Path,
    validator_for: ValidatorFactory,
    *,
    validator_version: str,
    files: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Validate every tracked JSON document that declares `$schema`."""
    candidates = sorted(
        path for path in (repository_files(root) if files is None else files) if path.suffix == ".json"
    )
    index = load_schema_index(root, candidates)
    documents: list[dict[str, Any]] = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            documents.append({"path": relative, "status": "unreadable"})
            continue
        if not isinstance(value, dict) or not isinstance(value.get("$schema"), str):
            continue
        reference = value["$schema"]
        entry: dict[str, Any] = {"path": relative, "schema": reference}
        schema_id = _declared_schema_id(root, relative, reference, index)
        if schema_id is None:
            entry["status"] = "unresolved"
            documents.append(entry)
            continue
        entry["schema_id"] = schema_id
        try:
            schema = {"$ref": METASCHEMA_ID} if schema_id == METASCHEMA_ID else index.by_id[schema_id]
            violations = [violation_record(error) for error in validator_for(schema).iter_errors(value)]
        except Exception as error:  # a broken schema must not hide the rest of the report
            entry["status"] = "validator-error"
            entry["error"] = type(error).__name__
            documents.append(entry)
            continue
        violations.sort(key=lambda item: (item["path"], item["keyword"], item["schema_path"]))
        entry["status"] = "violations" if violations else "clean"
        entry["violation_count"] = len(violations)
        entry["violations"] = violations[:MAX_RECORDED_VIOLATIONS]
        documents.append(entry)

    statuses = [entry["status"] for entry in documents]
    keywords: dict[str, int] = {}
    for entry in documents:
        for violation in entry.get("violations", []):
            keywords[violation["keyword"]] = keywords.get(violation["keyword"], 0) + 1
    return {
        "schema": REPORT_SCHEMA,
        "mode": "report-only",
        "validator": {
            "implementation": "jsonschema",
            "version": validator_version,
            "dialect": METASCHEMA_ID,
            "checked_formats": list(CHECKED_FORMATS),
        },
        "summary": {
            "documents": len(documents),
            "clean": statuses.count("clean"),
            "with_violations": statuses.count("violations"),
            "unresolved": statuses.count("unresolved"),
            "unreadable": statuses.count("unreadable"),
            "validator_errors": statuses.count("validator-error"),
            "violations": sum(entry.get("violation_count", 0) for entry in documents),
            "recorded_violations_by_keyword": dict(sorted(keywords.items())),
        },
        "documents": documents,
    }


def jsonschema_validator_factory(root: Path, files: Iterable[Path] | None = None) -> tuple[ValidatorFactory, str]:
    """Build a draft 2020-12 validator factory over the repository registry."""
    try:
        import jsonschema
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        from jsonschema_specifications import REGISTRY as SPECIFICATIONS
        from referencing.jsonschema import DRAFT202012
    except ImportError as error:
        raise ModelError(MISSING_VALIDATOR_MESSAGE) from error

    candidates = [
        path for path in (repository_files(root) if files is None else files) if path.suffix == ".json"
    ]
    index = load_schema_index(root, candidates)
    # The published draft 2020-12 meta-schemas come from the pinned
    # jsonschema-specifications package; nothing is ever fetched.
    registry = SPECIFICATIONS.combine(
        Registry().with_resources(
            (schema_id, Resource.from_contents(schema, default_specification=DRAFT202012))
            for schema_id, schema in index.by_id.items()
        )
    )
    format_checker = FormatChecker(formats=())
    for name, check in FORMAT_CHECKS.items():
        format_checker.checks(name)(check)

    def validator_for(schema: dict[str, Any]) -> _Validator:
        return Draft202012Validator(schema, registry=registry, format_checker=format_checker)

    version = getattr(jsonschema, "__version__", None)
    if not isinstance(version, str):
        from importlib.metadata import version as package_version

        version = package_version("jsonschema")
    return validator_for, version


def write_report(root: Path, report: dict[str, Any]) -> Path:
    destination = root / REPORT_PATH
    if destination.is_symlink():
        raise ModelError(f"{REPORT_PATH} may not be a symbolic link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def summary_line(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return (
        f"schema instances (report only): {summary['documents']} documents, "
        f"{summary['clean']} clean, {summary['with_violations']} with violations "
        f"({summary['violations']} violations), {summary['unresolved']} unresolved, "
        f"{summary['validator_errors']} validator errors; report: {REPORT_PATH}"
    )


def run_schema_instance_report(root: Path) -> dict[str, Any]:
    """Write the report and return it. Violations never raise."""
    files = repository_files(root)
    validator_for, version = jsonschema_validator_factory(root, files)
    report = build_report(root, validator_for, validator_version=version, files=files)
    write_report(root, report)
    print(summary_line(report), flush=True)
    return report
