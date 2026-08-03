# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from typing import Any, Iterable

from .model import ModelError, load_json, require_unique


POLICY_KEYS = {
    "$schema",
    "schema",
    "topic_terms",
    "private_overlay_sha256",
    "context_messages_before",
    "context_messages_after",
    "maximum_line_bytes",
    "maximum_narrative_bytes",
    "eras",
}
PRIVATE_OVERLAY_KEYS = {
    "schema",
    "context_break_terms",
    "control_envelope_prefixes",
    "publication_forbidden_terms",
}
ERA_KEYS = {"id", "title", "order"}
EVENT_KEYS = {
    "schema",
    "id",
    "date",
    "era",
    "role",
    "claim",
    "outcome",
    "then_status",
    "current_status",
    "supersession",
    "confidence",
    "privacy",
    "publication_use",
    "narrative_sha256",
    "narrative",
    "sources",
}
TECHNICAL_SOURCE_KINDS = {"commit", "issue", "mission", "evidence"}


@dataclass(frozen=True)
class JourneyPolicy:
    topic_terms: tuple[str, ...]
    private_overlay_sha256: str
    context_messages_before: int
    context_messages_after: int
    maximum_line_bytes: int
    maximum_narrative_bytes: int
    eras: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class JourneyPrivateOverlay:
    context_break_terms: tuple[str, ...]
    control_envelope_prefixes: tuple[str, ...]
    publication_forbidden_terms: tuple[str, ...]


@dataclass(frozen=True)
class Narrative:
    item_id_hash: str
    role: str
    text: str
    line_number: int


@dataclass
class SourceScan:
    path: Path
    source_id: str
    sha256: str = ""
    bytes: int = 0
    lines: int = 0
    session_id: str | None = None
    parent_session_id: str | None = None
    session_timestamp: str | None = None
    included: int = 0
    duplicates: int = 0
    excluded_record_type: int = 0
    excluded_non_narrative: int = 0
    excluded_unrelated: int = 0
    excluded_oversized: int = 0
    malformed: int = 0


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ModelError(f"{label}: expected an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise ModelError(
            f"{label}: missing {missing or 'none'}; unknown {extra or 'none'}"
        )
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"{label}: expected an integer")
    if not minimum <= value <= maximum:
        raise ModelError(f"{label}: outside {minimum} through {maximum}")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ModelError(f"{label}: expected a non-empty trimmed string")
    if len(value) > maximum or "\n" in value or "\r" in value:
        raise ModelError(f"{label}: exceeds its single-line bound")
    return value


def load_journey_policy(root: Path) -> JourneyPolicy:
    value = _exact(
        load_json(root / "docs" / "journey-pipeline.json"),
        POLICY_KEYS,
        "journey pipeline",
    )
    if value["$schema"] != "../schemas/journey-pipeline.schema.json":
        raise ModelError("journey pipeline has a non-canonical schema reference")
    if value["schema"] != "hyperflux-journey-pipeline-v2":
        raise ModelError("unsupported journey pipeline schema")
    raw_terms = value["topic_terms"]
    if not isinstance(raw_terms, list) or not 3 <= len(raw_terms) <= 32:
        raise ModelError("journey pipeline requires 3 through 32 topic terms")
    terms = tuple(_text(term, "journey topic term", 64).casefold() for term in raw_terms)
    require_unique(list(terms), "journey topic term")
    private_overlay_sha256 = _text(
        value["private_overlay_sha256"], "journey private overlay SHA-256", 64
    )
    if re.fullmatch(r"[0-9a-f]{64}", private_overlay_sha256) is None:
        raise ModelError("journey private overlay SHA-256 is malformed")
    raw_eras = value["eras"]
    if not isinstance(raw_eras, list) or len(raw_eras) != 6:
        raise ModelError("journey pipeline requires exactly six eras")
    eras: list[tuple[str, str]] = []
    for index, raw in enumerate(raw_eras, start=1):
        item = _exact(raw, ERA_KEYS, f"journey era {index}")
        if _integer(item["order"], f"journey era {index} order", 1, 6) != index:
            raise ModelError("journey eras must use exact contiguous order")
        eras.append(
            (
                _text(item["id"], f"journey era {index} id", 64),
                _text(item["title"], f"journey era {index} title", 80),
            )
        )
    require_unique([identifier for identifier, _ in eras], "journey era id")
    return JourneyPolicy(
        topic_terms=terms,
        private_overlay_sha256=private_overlay_sha256,
        context_messages_before=_integer(
            value["context_messages_before"], "journey prelude", 0, 16
        ),
        context_messages_after=_integer(
            value["context_messages_after"], "journey context", 1, 64
        ),
        maximum_line_bytes=_integer(
            value["maximum_line_bytes"], "journey line bound", 1024 * 1024, 32 * 1024 * 1024
        ),
        maximum_narrative_bytes=_integer(
            value["maximum_narrative_bytes"],
            "journey narrative bound",
            4096,
            1024 * 1024,
        ),
        eras=tuple(eras),
    )


def load_journey_private_overlay(
    path: Path, expected_sha256: str
) -> JourneyPrivateOverlay:
    path = Path(os.path.abspath(path.expanduser()))
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ModelError(f"journey private overlay is unavailable: {error}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_size < 2
            or before.st_size > 64 * 1024
        ):
            raise ModelError(
                "journey private overlay must be one owner-held mode-0600 regular file"
            )
        raw = bytearray()
        while len(raw) <= 64 * 1024:
            chunk = os.read(descriptor, min(16 * 1024, 64 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) != before.st_size
        or len(raw) > 64 * 1024
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ModelError("journey private overlay changed while it was being read")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ModelError("journey private overlay SHA-256 does not match public authority")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelError("journey private overlay is not canonical UTF-8 JSON") from error
    value = _exact(decoded, PRIVATE_OVERLAY_KEYS, "journey private overlay")
    if value["schema"] != "hyperflux-private-journey-overlay-v1":
        raise ModelError("unsupported journey private overlay schema")

    def terms(field: str, minimum: int, maximum: int) -> tuple[str, ...]:
        raw = value[field]
        if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
            raise ModelError(
                f"journey private overlay {field} requires {minimum} through {maximum} terms"
            )
        normalized = tuple(
            _text(term, f"journey private overlay {field} term", 96).casefold()
            for term in raw
        )
        require_unique(list(normalized), f"journey private overlay {field} term")
        return normalized

    return JourneyPrivateOverlay(
        context_break_terms=terms("context_break_terms", 1, 64),
        control_envelope_prefixes=terms("control_envelope_prefixes", 1, 16),
        publication_forbidden_terms=terms("publication_forbidden_terms", 1, 64),
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _narrative(
    value: object,
    line_number: int,
    maximum_bytes: int,
    control_envelope_prefixes: tuple[str, ...],
) -> Narrative | None:
    if not isinstance(value, dict):
        return None
    if value.get("type") != "response_item":
        return None
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    allowed = "input_text" if role == "user" else "output_text"
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != allowed:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    text = _canonical_text("\n\n".join(parts))
    if text.casefold().startswith(control_envelope_prefixes):
        return None
    if not text or len(text.encode("utf-8")) > maximum_bytes:
        return None
    item_id = payload.get("id")
    item_identity = item_id if isinstance(item_id, str) and item_id else f"line:{line_number}"
    return Narrative(
        item_id_hash=_hash_text(item_identity),
        role=role,
        text=text,
        line_number=line_number,
    )


def _session_metadata(value: object) -> tuple[str, str | None, str] | None:
    if not isinstance(value, dict) or value.get("type") != "session_meta":
        return None
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id") or payload.get("session_id")
    timestamp = payload.get("timestamp")
    parent = payload.get("forked_from_id")
    if not isinstance(session_id, str) or not session_id:
        raise ModelError("session metadata has no stable id")
    if not isinstance(timestamp, str):
        raise ModelError("session metadata has no timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ModelError("session metadata timestamp is malformed") from error
    if parent is not None and not isinstance(parent, str):
        raise ModelError("session parent id is malformed")
    return session_id, parent, parsed.date().isoformat()


class _Ledger:
    def __init__(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(path)
        self.connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE events (
                content_hash TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                narrative TEXT NOT NULL,
                date TEXT NOT NULL,
                first_source TEXT NOT NULL,
                first_line INTEGER NOT NULL
            );
            CREATE TABLE item_ids (
                item_hash TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL
            );
            CREATE TABLE sources (
                content_hash TEXT NOT NULL,
                source_id TEXT NOT NULL,
                item_hash TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                UNIQUE(content_hash, source_id, item_hash, line_number)
            );
            """
        )

    def add(self, narrative: Narrative, source_id: str, date: str) -> bool:
        content_hash = _hash_text(narrative.text)
        existing_item = self.connection.execute(
            "SELECT content_hash FROM item_ids WHERE item_hash = ?",
            (narrative.item_id_hash,),
        ).fetchone()
        if existing_item is not None and existing_item[0] != content_hash:
            raise ModelError("one narrative item id maps to different content")
        existing = self.connection.execute(
            "SELECT 1 FROM events WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if existing is None:
            self.connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    content_hash,
                    narrative.role,
                    narrative.text,
                    date,
                    source_id,
                    narrative.line_number,
                ),
            )
        self.connection.execute(
            "INSERT OR IGNORE INTO item_ids VALUES (?, ?)",
            (narrative.item_id_hash, content_hash),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?)",
            (
                content_hash,
                source_id,
                narrative.item_id_hash,
                narrative.line_number,
            ),
        )
        return existing is None

    def events(self) -> Iterable[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT content_hash, role, narrative, date FROM events "
            "ORDER BY date, first_source, first_line, content_hash"
        )
        for content_hash, role, narrative, date in rows:
            sources = [
                {
                    "kind": "chat",
                    "reference": f"{source_id}:{line_number}:{item_hash}",
                    "source_id": source_id,
                    "item_id_hash": item_hash,
                    "line": line_number,
                }
                for source_id, item_hash, line_number in self.connection.execute(
                    "SELECT source_id, item_hash, line_number FROM sources "
                    "WHERE content_hash = ? ORDER BY source_id, line_number, item_hash",
                    (content_hash,),
                )
            ]
            event: dict[str, object] = {
                "schema": "hyperflux-private-journey-event-v1",
                "id": f"narrative-{content_hash[:24]}",
                "date": date,
                "era": "unassigned",
                "role": role,
                "claim": "unclassified narrative",
                "outcome": "pending-review",
                "then_status": "as-recorded",
                "current_status": "unreviewed",
                "supersession": None,
                "confidence": "motivation-only",
                "privacy": "private-source",
                "publication_use": "paraphrase-only",
                "narrative_sha256": _hash_text(narrative),
                "narrative": narrative,
                "sources": sources,
            }
            validate_journey_event(event)
            yield event

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def _regular_source(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ModelError(f"journey source is not a regular non-symbolic file: {path}")
    return metadata


def validate_journey_event(value: object) -> None:
    item = _exact(value, EVENT_KEYS, "journey event")
    if item["schema"] != "hyperflux-private-journey-event-v1":
        raise ModelError("unsupported private journey event schema")
    confidence = item["confidence"]
    if confidence not in {"motivation-only", "technical-source-backed"}:
        raise ModelError("journey event has an invalid confidence class")
    if item["privacy"] != "private-source":
        raise ModelError("journey event must remain private source material")
    if item["publication_use"] != "paraphrase-only":
        raise ModelError("journey source cannot authorize a direct quotation")
    sources = item["sources"]
    if not isinstance(sources, list) or not sources:
        raise ModelError("journey event requires at least one source")
    source_kinds: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ModelError(f"journey event source {index} must be an object")
        kind = source.get("kind")
        reference = source.get("reference")
        if kind not in {"chat", *TECHNICAL_SOURCE_KINDS}:
            raise ModelError(f"journey event source {index} has an invalid kind")
        if not isinstance(reference, str) or not reference:
            raise ModelError(f"journey event source {index} has no reference")
        source_kinds.add(kind)
    if confidence == "technical-source-backed" and not (
        source_kinds & TECHNICAL_SOURCE_KINDS
    ):
        raise ModelError(
            "technical journey claims require commit, issue, mission, or evidence authority"
        )


def _scan_source(
    path: Path,
    policy: JourneyPolicy,
    private_overlay: JourneyPrivateOverlay,
    ledger: _Ledger,
) -> SourceScan:
    initial_metadata = _regular_source(path)
    source_id = f"source-{_hash_text(str(path.resolve()))[:20]}"
    result = SourceScan(path=path.resolve(), source_id=source_id)
    digest = hashlib.sha256()
    pending: deque[Narrative] = deque()
    context_remaining = 0

    def emit(item: Narrative) -> None:
        if result.session_timestamp is None:
            raise ModelError(f"journey narrative precedes session metadata: {path}")
        if ledger.add(item, result.source_id, result.session_timestamp):
            result.included += 1
        else:
            result.duplicates += 1

    with path.open("rb", buffering=0) as source:
        while True:
            raw = source.readline(policy.maximum_line_bytes + 1)
            if not raw:
                break
            result.lines += 1
            result.bytes += len(raw)
            digest.update(raw)
            oversized = len(raw) > policy.maximum_line_bytes
            while oversized and raw and not raw.endswith(b"\n"):
                raw = source.readline(policy.maximum_line_bytes + 1)
                if not raw:
                    break
                result.bytes += len(raw)
                digest.update(raw)
                oversized = True
            if oversized:
                result.excluded_oversized += 1
                continue
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                result.malformed += 1
                continue
            metadata = _session_metadata(value)
            if metadata is not None:
                session_id, parent, timestamp = metadata
                if result.session_id not in {None, session_id}:
                    raise ModelError(f"journey source changes session id: {path}")
                if result.parent_session_id not in {None, parent}:
                    raise ModelError(f"journey source changes parent session id: {path}")
                if result.session_timestamp not in {None, timestamp}:
                    raise ModelError(f"journey source changes session date: {path}")
                result.session_id = session_id
                result.parent_session_id = parent
                result.session_timestamp = timestamp
                continue
            narrative = _narrative(
                value,
                result.lines,
                policy.maximum_narrative_bytes,
                private_overlay.control_envelope_prefixes,
            )
            if narrative is None:
                if isinstance(value, dict) and value.get("type") == "response_item":
                    result.excluded_non_narrative += 1
                else:
                    result.excluded_record_type += 1
                continue
            folded = narrative.text.casefold()
            topic_hits = {term for term in policy.topic_terms if term in folded}
            context_break = any(
                term in folded for term in private_overlay.context_break_terms
            )
            relevant = bool(topic_hits) and not (context_break and len(topic_hits) < 3)
            if relevant:
                while pending:
                    emit(pending.popleft())
                emit(narrative)
                context_remaining = policy.context_messages_after
            elif context_break:
                result.excluded_unrelated += len(pending) + 1
                pending.clear()
                context_remaining = 0
            elif context_remaining > 0:
                if narrative.role == "user":
                    emit(narrative)
                else:
                    result.excluded_unrelated += 1
                context_remaining -= 1
            else:
                if policy.context_messages_before == 0:
                    result.excluded_unrelated += 1
                    continue
                if len(pending) == policy.context_messages_before:
                    pending.popleft()
                    result.excluded_unrelated += 1
                pending.append(narrative)
    result.excluded_unrelated += len(pending)
    final_metadata = _regular_source(path)
    initial_identity = (
        initial_metadata.st_dev,
        initial_metadata.st_ino,
        initial_metadata.st_size,
        initial_metadata.st_mtime_ns,
    )
    final_identity = (
        final_metadata.st_dev,
        final_metadata.st_ino,
        final_metadata.st_size,
        final_metadata.st_mtime_ns,
    )
    if final_identity != initial_identity or result.bytes != final_metadata.st_size:
        raise ModelError(f"journey source changed while it was being streamed: {path}")
    if result.session_id is None or result.session_timestamp is None:
        raise ModelError(f"journey source has no session metadata: {path}")
    result.sha256 = digest.hexdigest()
    return result


def _session_hash(value: str | None) -> str | None:
    return None if value is None else _hash_text(value)


def _ancestry(scans: list[SourceScan]) -> dict[str, list[str]]:
    parents: dict[str, str | None] = {}
    for scan in scans:
        assert scan.session_id is not None
        previous = parents.get(scan.session_id)
        if scan.session_id in parents and previous != scan.parent_session_id:
            raise ModelError("duplicate session sources disagree about fork ancestry")
        parents[scan.session_id] = scan.parent_session_id
    ancestry: dict[str, list[str]] = {}
    for session_id in parents:
        seen: set[str] = set()
        lineage: list[str] = []
        current = parents[session_id]
        while current is not None:
            if current == session_id or current in seen:
                raise ModelError("journey source fork ancestry contains a cycle")
            seen.add(current)
            lineage.append(_hash_text(current))
            current = parents.get(current)
        ancestry[session_id] = lineage
    return ancestry


def _write_private_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _write_private_ledger(path: Path, events: Iterable[dict[str, object]]) -> int:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
            for event in events:
                output.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
                output.write("\n")
                count += 1
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    return count


def extract_journey_sources(
    root: Path,
    sources: Iterable[Path],
    output: Path,
    private_overlay: Path,
) -> dict[str, object]:
    policy = load_journey_policy(root.resolve())
    overlay = load_journey_private_overlay(
        private_overlay, policy.private_overlay_sha256
    )
    source_paths = sorted(
        {Path(os.path.abspath(path)) for path in sources},
        key=str,
    )
    if not source_paths:
        raise ModelError("journey extraction requires at least one source")
    if output.exists() or output.is_symlink():
        raise ModelError("journey output must not already exist")
    output.mkdir(mode=0o700, parents=True)
    os.chmod(output, 0o700)
    database_path = output / ".journey-index.sqlite3"
    ledger = _Ledger(database_path)
    scans: list[SourceScan] = []
    try:
        for path in source_paths:
            scans.append(_scan_source(path, policy, overlay, ledger))
        ledger.commit()
        ancestry = _ancestry(scans)
        ledger_count = _write_private_ledger(output / "event-ledger.jsonl", ledger.events())
    finally:
        ledger.close()
    database_path.unlink()
    manifest_sources = []
    for scan in scans:
        assert scan.session_id is not None
        manifest_sources.append(
            {
                "source_id": scan.source_id,
                "path": str(scan.path),
                "sha256": scan.sha256,
                "bytes": scan.bytes,
                "lines": scan.lines,
                "session_id_hash": _session_hash(scan.session_id),
                "parent_session_id_hash": _session_hash(scan.parent_session_id),
                "ancestor_session_id_hashes": ancestry[scan.session_id],
                "included": scan.included,
                "duplicates": scan.duplicates,
                "excluded_record_type": scan.excluded_record_type,
                "excluded_non_narrative": scan.excluded_non_narrative,
                "excluded_unrelated": scan.excluded_unrelated,
                "excluded_oversized": scan.excluded_oversized,
                "malformed": scan.malformed,
            }
        )
    incomplete = any(
        source["malformed"] or source["excluded_oversized"]
        for source in manifest_sources
    )
    manifest: dict[str, object] = {
        "schema": "hyperflux-private-journey-source-manifest-v1",
        "status": "requires-review" if incomplete else "complete",
        "privacy": "private-0600",
        "private_overlay_sha256": policy.private_overlay_sha256,
        "deduplication": "item-id-or-canonical-content-sha256",
        "narrative_scope": "user-and-assistant-hyperflux-context-only",
        "era_ids": [identifier for identifier, _ in policy.eras],
        "event_count": ledger_count,
        "sources": manifest_sources,
    }
    _write_private_json(output / "source-manifest.json", manifest)
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return manifest
