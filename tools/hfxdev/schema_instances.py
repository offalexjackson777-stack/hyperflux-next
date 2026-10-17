# SPDX-License-Identifier: GPL-2.0-only

"""Validate every tracked JSON document against the schema it declares.

The real `jsonschema` draft 2020-12 validator checks each document, a report
is written below `build/`, and any violation that the committed allow-list
(`verification/schema-instance-allowlist.json`) does not name fails the node.
The allow-list may only shrink: its ceiling equals its entry count, and an
entry that no longer matches a violation is itself a failure. The validator
lives only in the pinned development image, so the import is lazy and a
missing module names the command that has it.

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
from .schema_subset import SchemaViolation, validate

REPORT_PATH = PurePosixPath("build/schema-instances-report.json")
REPORT_SCHEMA = "hyperflux-schema-instance-report-v2"
ALLOWLIST_PATH = PurePosixPath("verification/schema-instance-allowlist.json")
ALLOWLIST_SCHEMA_PATH = PurePosixPath("schemas/schema-instance-allowlist.schema.json")
MAX_REPORTED_FAILURES = 20
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
class AllowlistEntry:
    """One tolerated violation, matched on the report's redacted fields."""

    document: str
    path: str
    keyword: str
    schema_path: str
    owner: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.document, self.path, self.keyword, self.schema_path)


@dataclass(frozen=True)
class Allowlist:
    ceiling: int
    entries: tuple[AllowlistEntry, ...]

    @property
    def keys(self) -> frozenset[tuple[str, str, str, str]]:
        return frozenset(entry.key for entry in self.entries)


EMPTY_ALLOWLIST = Allowlist(0, ())


def parse_allowlist(schema: dict[str, Any], value: Any, owners: Iterable[str]) -> Allowlist:
    """Check an allow-list document and return its entries.

    Uses the stdlib subset validator, so the host can check the file without
    the jsonschema module.
    """
    try:
        validate(schema, value)
    except SchemaViolation as error:
        raise ModelError(f"{ALLOWLIST_PATH}: {error}") from error
    known_owners = set(owners)
    entries = tuple(
        AllowlistEntry(
            document=item["document"],
            path=item["path"],
            keyword=item["keyword"],
            schema_path=item["schema_path"],
            owner=item["owner"],
            reason=item["reason"],
        )
        for item in value["entries"]
    )
    keys = [entry.key for entry in entries]
    if len(set(keys)) != len(keys):
        raise ModelError(f"{ALLOWLIST_PATH}: an entry is listed twice")
    if keys != sorted(keys):
        raise ModelError(f"{ALLOWLIST_PATH}: entries must be sorted by document, path, keyword and schema path")
    for entry in entries:
        if entry.owner not in known_owners:
            raise ModelError(f"{ALLOWLIST_PATH}: entry for {entry.document} names an unknown owner")
    if value["ceiling"] != len(entries):
        raise ModelError(
            f"{ALLOWLIST_PATH}: ceiling must equal the number of entries; "
            "lower it when an entry is removed, never raise it"
        )
    return Allowlist(value["ceiling"], entries)


def load_allowlist(root: Path) -> Allowlist:
    from .atlas import load_repository_atlas

    return parse_allowlist(
        load_json(root / ALLOWLIST_SCHEMA_PATH),
        load_json(root / ALLOWLIST_PATH),
        load_repository_atlas(root).by_id,
    )


def allowlist_growth(base_value: Any, current: Allowlist) -> list[str]:
    """How `current` grew compared with an earlier allow-list document.

    The earlier copy usually comes from the merge base with the integration
    branch. Only its ceiling and entry keys are read, so an older layout does
    not need to pass today's owner checks.
    """
    issues: list[str] = []
    if not isinstance(base_value, dict):
        return ["the earlier allow-list is not a JSON object"]
    base_ceiling = base_value.get("ceiling")
    base_entries = base_value.get("entries")
    if not isinstance(base_ceiling, int) or not isinstance(base_entries, list):
        return ["the earlier allow-list has no ceiling or entries"]
    if current.ceiling > base_ceiling:
        issues.append(f"ceiling rose from {base_ceiling} to {current.ceiling}")
    base_keys = {
        (item.get("document"), item.get("path"), item.get("keyword"), item.get("schema_path"))
        for item in base_entries
        if isinstance(item, dict)
    }
    added = [entry for entry in current.entries if entry.key not in base_keys]
    for entry in added:
        issues.append(f"new entry for {entry.document} {entry.keyword} at {entry.path or '/'}")
    return issues


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


def redacted_path(error: _ValidationError) -> list[int | str | None]:
    """The error's instance path with every name the schema does not declare as None.

    Array indexes stay. An object key stays only when the schema names it
    under `properties` on the way to the failed keyword; map keys and unknown
    keys come from the document, so they are dropped.
    """
    named = _schema_named_properties(list(error.absolute_schema_path))
    return [
        segment
        if isinstance(segment, int) and not isinstance(segment, bool)
        else (str(segment) if str(segment) in named else None)
        for segment in error.absolute_path
    ]


def violation_record(error: _ValidationError) -> dict[str, str]:
    """A privacy-preserving description of one validation error.

    Uses only the error's instance path, keyword and schema path. Never the
    message, the instance or the failing value: those can hold private text
    or the names of unknown keys.
    """
    schema_path = list(error.absolute_schema_path)
    instance_path = [REDACTED_SEGMENT if segment is None else segment for segment in redacted_path(error)]
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
    allowlist: Allowlist = EMPTY_ALLOWLIST,
) -> dict[str, Any]:
    """Validate every tracked JSON document that declares `$schema`.

    Violations the allow-list names are reported apart and do not count as
    violations; allow-list entries that match nothing are listed as stale.
    """
    candidates = sorted(
        path for path in (repository_files(root) if files is None else files) if path.suffix == ".json"
    )
    index = load_schema_index(root, candidates)
    allowed_keys = allowlist.keys
    matched_keys: set[tuple[str, str, str, str]] = set()
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
        allowed: list[dict[str, str]] = []
        remaining: list[dict[str, str]] = []
        for violation in violations:
            key = (relative, violation["path"], violation["keyword"], violation["schema_path"])
            if key in allowed_keys:
                matched_keys.add(key)
                allowed.append(violation)
            else:
                remaining.append(violation)
        entry["status"] = "violations" if remaining else ("allowed" if allowed else "clean")
        entry["violation_count"] = len(remaining)
        entry["violations"] = remaining[:MAX_RECORDED_VIOLATIONS]
        if allowed:
            entry["allowed_count"] = len(allowed)
        documents.append(entry)

    statuses = [entry["status"] for entry in documents]
    keywords: dict[str, int] = {}
    for entry in documents:
        for violation in entry.get("violations", []):
            keywords[violation["keyword"]] = keywords.get(violation["keyword"], 0) + 1
    stale = [
        {"document": entry.document, "path": entry.path, "keyword": entry.keyword, "schema_path": entry.schema_path}
        for entry in allowlist.entries
        if entry.key not in matched_keys
    ]
    return {
        "schema": REPORT_SCHEMA,
        "mode": "enforcing",
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
            "allowed_violations": sum(entry.get("allowed_count", 0) for entry in documents),
            "allowlist_entries": len(allowlist.entries),
            "stale_allowlist_entries": len(stale),
            "recorded_violations_by_keyword": dict(sorted(keywords.items())),
        },
        "stale_allowlist_entries": stale,
        "documents": documents,
    }


def import_jsonschema() -> Any:
    """The jsonschema module, or a ModelError naming the command that has it."""
    try:
        import jsonschema
        import jsonschema_specifications  # noqa: F401
        import referencing  # noqa: F401
    except ImportError as error:
        raise ModelError(MISSING_VALIDATOR_MESSAGE) from error
    return jsonschema


def jsonschema_version() -> str:
    jsonschema = import_jsonschema()
    version = getattr(jsonschema, "__version__", None)
    if not isinstance(version, str):
        from importlib.metadata import version as package_version

        version = package_version("jsonschema")
    return version


def schema_registry(index: SchemaIndex) -> Any:
    """A `referencing` registry of the repository schemas by `$id`.

    The published draft 2020-12 meta-schemas come from the pinned
    jsonschema-specifications package; nothing is ever fetched.
    """
    import_jsonschema()
    from jsonschema_specifications import REGISTRY as SPECIFICATIONS
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    return SPECIFICATIONS.combine(
        Registry().with_resources(
            (schema_id, Resource.from_contents(schema, default_specification=DRAFT202012))
            for schema_id, schema in index.by_id.items()
        )
    )


def format_checker() -> Any:
    """A format checker for exactly the repository's checked formats."""
    from jsonschema import FormatChecker

    checker = FormatChecker(formats=())
    for name, check in FORMAT_CHECKS.items():
        checker.checks(name)(check)
    return checker


def jsonschema_validator_factory(root: Path, files: Iterable[Path] | None = None) -> tuple[ValidatorFactory, str]:
    """Build a draft 2020-12 validator factory over the repository registry."""
    import_jsonschema()
    from jsonschema import Draft202012Validator

    candidates = [
        path for path in (repository_files(root) if files is None else files) if path.suffix == ".json"
    ]
    registry = schema_registry(load_schema_index(root, candidates))
    checker = format_checker()

    def validator_for(schema: dict[str, Any]) -> _Validator:
        return Draft202012Validator(schema, registry=registry, format_checker=checker)

    return validator_for, jsonschema_version()


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
        f"schema instances: {summary['documents']} documents, "
        f"{summary['clean']} clean, {summary['with_violations']} with violations "
        f"({summary['violations']} violations, {summary['allowed_violations']} allow-listed), "
        f"{summary['unresolved']} unresolved, {summary['unreadable']} unreadable, "
        f"{summary['validator_errors']} validator errors, "
        f"{summary['stale_allowlist_entries']} stale allow-list entries; report: {REPORT_PATH}"
    )


def enforcement_failures(report: dict[str, Any]) -> list[str]:
    """Lines naming what makes the report fail, using redacted fields only."""
    failures: list[str] = []
    for entry in report["documents"]:
        status = entry["status"]
        if status == "violations":
            for violation in entry["violations"]:
                failures.append(
                    f"{entry['path']}: {violation['keyword']} at {violation['path'] or '/'} "
                    f"(schema {violation['schema_path']})"
                )
            hidden = entry["violation_count"] - len(entry["violations"])
            if hidden > 0:
                failures.append(f"{entry['path']}: {hidden} more violations")
        elif status in {"unresolved", "unreadable", "validator-error"}:
            failures.append(f"{entry['path']}: {status}")
    for stale in report.get("stale_allowlist_entries", []):
        failures.append(
            f"{ALLOWLIST_PATH}: entry for {stale['document']} {stale['keyword']} at "
            f"{stale['path'] or '/'} matches no violation; remove it and lower the ceiling"
        )
    return failures


def run_schema_instance_report(root: Path) -> dict[str, Any]:
    """Write the report, then fail on any violation the allow-list does not name."""
    allowlist = load_allowlist(root)
    files = repository_files(root)
    validator_for, version = jsonschema_validator_factory(root, files)
    report = build_report(root, validator_for, validator_version=version, files=files, allowlist=allowlist)
    write_report(root, report)
    print(summary_line(report), flush=True)
    failures = enforcement_failures(report)
    if failures:
        shown = failures[:MAX_REPORTED_FAILURES]
        if len(failures) > len(shown):
            shown.append(f"... and {len(failures) - len(shown)} more; see {REPORT_PATH}")
        raise ModelError("JSON Schema instance validation failed:\n  " + "\n  ".join(shown))
    return report
