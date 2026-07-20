# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from .model import ModelError, load_foundation


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ModelError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def capture_inventory(root: Path, source_id: str, source_path: Path) -> Path:
    _, sources, _ = load_foundation(root)
    matches = [source for source in sources["sources"] if source["id"] == source_id]
    if len(matches) != 1 or matches[0]["kind"] != "git":
        raise ModelError(f"{source_id}: expected exactly one git source")
    source = matches[0]
    commit = _git(source_path, "rev-parse", f"{source['commit']}^{{commit}}").strip()
    if not commit.startswith(source["commit"]):
        raise ModelError(f"{source_id}: resolved commit {commit} does not match {source['commit']}")
    raw = _git(source_path, "ls-tree", "-r", commit)
    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        metadata, path = line.split("\t", 1)
        mode, object_type, blob = metadata.split(" ")
        entries.append({"path": path, "mode": mode, "type": object_type, "object": blob})
    inventory: dict[str, Any] = {
        "schema": "hyperflux-source-inventory-v1",
        "source": source_id,
        "commit": commit,
        "entry_count": len(entries),
        "entries": entries,
    }
    destination = root / source["inventory"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def summary(root: Path) -> str:
    _, sources, ledger = load_foundation(root)
    source_count = len(sources["sources"])
    decisions: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for entry in ledger["entries"]:
        decisions[entry["disposition"]] = decisions.get(entry["disposition"], 0) + 1
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    lines = [
        "HyperFlux Next migration",
        f"Sources: {source_count}",
        f"Subsystem decisions: {len(ledger['entries'])}",
        "Decisions: " + ", ".join(f"{name}={count}" for name, count in sorted(decisions.items())),
        "Status: " + ", ".join(f"{name}={count}" for name, count in sorted(statuses.items())),
        f"Default: {ledger['default_disposition']}",
    ]
    return "\n".join(lines)

