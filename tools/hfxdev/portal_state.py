# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from .model import ModelError, load_json, require_unique
from .performance import load_performance_budgets, verify_static_performance_budgets
from .public_readiness import public_readiness
from .release import ReleaseGate, load_release_gates
from .testgraph import TestNode, load_test_catalog


MIGRATION_KEYS = {
    "id",
    "component",
    "disposition",
    "status",
    "sources",
    "destination_owner",
    "rationale",
}
MIGRATION_STATUSES = ("ACCEPTED", "IN_PROGRESS", "PENDING_REVIEW", "REJECTED")
LANES = ("fast", "full-software", "hardware")


@dataclass(frozen=True)
class RepositoryStatePage:
    content: str
    search_records: tuple[dict[str, str], ...]


def _migration_entries(root: Path) -> tuple[dict[str, Any], ...]:
    value = load_json(root / "migration" / "ledger.json")
    if set(value) != {"$schema", "schema", "default_disposition", "entries"}:
        raise ModelError("migration ledger has missing or unknown top-level fields")
    if (
        value["schema"] != "hyperflux-migration-ledger-v1"
        or value["default_disposition"] != "REJECT_UNTIL_REVIEWED"
    ):
        raise ModelError("migration ledger has an unsupported schema or default")
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise ModelError("migration ledger must contain entries")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != MIGRATION_KEYS:
            raise ModelError(f"migration entry {index}: missing or unknown fields")
        if entry["status"] not in MIGRATION_STATUSES:
            raise ModelError(f"migration entry {entry['id']}: unsupported status")
        if not isinstance(entry["sources"], list) or not entry["sources"]:
            raise ModelError(f"migration entry {entry['id']}: sources are missing")
    require_unique([entry["id"] for entry in entries], "migration entry id")
    return tuple(entries)


def _status_label(value: str) -> str:
    labels = {
        "software-satisfied": "Ready in software",
        "blocked-by-lifecycle-evidence": "Awaiting lifecycle evidence",
        "blocked-by-physical-evidence": "Awaiting hardware evidence",
        "publication-locked": "Publication decision required",
        "enforced-software": "Enforced in software",
    }
    return labels.get(value, value.replace("-", " ").replace("_", " ").title())


def _gate_detail(gate: ReleaseGate) -> str:
    criteria = "".join(f"<li>{escape(item)}</li>" for item in gate.criteria)
    evidence = "".join(f"<li><code>{escape(item)}</code></li>" for item in gate.evidence)
    remaining = (
        "".join(f"<li>{escape(item)}</li>" for item in gate.remaining)
        if gate.remaining
        else "<li>No remaining software work is recorded for this gate.</li>"
    )
    return f"""<details class="state-detail" data-gate data-state="{escape(gate.status)}">
  <summary><span><strong>{escape(gate.title)}</strong><small>{escape(gate.id)}</small></span><span class="state-badge state-badge--{escape(gate.status)}">{escape(_status_label(gate.status))}</span></summary>
  <div class="state-detail-body"><section><h3>Criteria</h3><ul>{criteria}</ul></section><section><h3>Evidence sources</h3><ul>{evidence}</ul></section><section><h3>Remaining boundary</h3><ul>{remaining}</ul></section></div>
</details>"""


def _migration_row(entry: dict[str, Any]) -> str:
    sources = ", ".join(entry["sources"])
    return (
        f'<tr data-migration data-state="{escape(entry["status"])}">'
        f'<td><strong>{escape(entry["component"])}</strong><small>{escape(entry["id"])}</small></td>'
        f'<td><span class="state-badge state-badge--{escape(entry["status"].lower().replace("_", "-"))}">{escape(_status_label(entry["status"]))}</span></td>'
        f'<td>{escape(entry["disposition"].replace("_", " ").title())}</td>'
        f'<td>{escape(entry["destination_owner"])}</td><td>{escape(sources)}</td>'
        f'<td>{escape(entry["rationale"])}</td></tr>'
    )


def _verification_row(node: TestNode, maximum: int) -> str:
    lanes = " ".join(node.lanes)
    dependencies = ", ".join(node.dependencies) if node.dependencies else "None"
    width = max(1, round(node.expected_duration_seconds / maximum * 100))
    return f"""<tr data-verification data-lanes="{escape(lanes)}">
  <td><strong>{escape(node.title)}</strong><small><code>{escape(node.id)}</code></small></td>
  <td>{escape(node.owned_domain)}</td>
  <td><div class="timing-bar" title="Expected budget: {node.expected_duration_seconds} seconds"><span style="width:{width}%"></span></div><small>{node.expected_duration_seconds}s expected / {node.timeout_seconds}s timeout</small></td>
  <td>{escape(node.isolation)}</td><td>{escape(node.resume_policy)}</td><td>{escape(dependencies)}</td>
</tr>"""


def render_repository_state(root: Path) -> RepositoryStatePage:
    readiness = public_readiness(root)
    gates = load_release_gates(root)
    migration = _migration_entries(root)
    tests = load_test_catalog(root).ordered()
    performance = load_performance_budgets(root)
    measured = verify_static_performance_budgets(root, performance)

    satisfied = readiness["software"]["gates_ready"]
    accepted = sum(entry["status"] == "ACCEPTED" for entry in migration)
    software_nodes = sum("full-software" in node.lanes for node in tests)
    expected_total = sum(
        node.expected_duration_seconds for node in tests if "full-software" in node.lanes
    )
    maximum_duration = max(node.expected_duration_seconds for node in tests)

    gate_options = "".join(
        f'<option value="{escape(status)}">{escape(_status_label(status))}</option>'
        for status in sorted({gate.status for gate in gates})
    )
    migration_options = "".join(
        f'<option value="{escape(status)}">{escape(_status_label(status))}</option>'
        for status in MIGRATION_STATUSES
        if any(entry["status"] == status for entry in migration)
    )
    gate_html = "".join(_gate_detail(gate) for gate in gates)
    migration_html = "".join(_migration_row(entry) for entry in migration)
    verification_html = "".join(
        _verification_row(node, maximum_duration) for node in tests
    )
    performance_html = "".join(
        "<tr>"
        f"<td><strong>{escape(metric.title)}</strong><small><code>{escape(metric.id)}</code></small></td>"
        f"<td>{escape(metric.measurement_kind.replace('-', ' '))}</td>"
        f"<td>{escape(f'{measured[metric.id]:g} {metric.unit}' if metric.id in measured else 'Requires its bounded evidence run')}</td>"
        f"<td>{metric.maximum:g} {escape(metric.unit)}</td>"
        f'<td><span class="state-badge state-badge--{escape(metric.status)}">{escape(_status_label(metric.status))}</span></td>'
        f"<td>{escape(metric.rationale)}</td></tr>"
        for metric in performance
    )

    content = f"""<article class="repository-state" data-repository-state>
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="../index.html">Home</a><span>Repository State</span></nav>
  <header class="page-hero page-hero--ledger state-header"><div><p class="page-kicker">Generated readiness dashboard</p><h1>Repository State</h1><p class="lede">Review release boundaries, migration decisions, verification budgets, and performance limits compiled from canonical repository ledgers.</p></div><span class="state-lock">Product unreleased: decision required</span></header>
  <div class="notice"><strong>Software readiness is not hardware evidence.</strong> A software-ready gate has passed its deterministic checks; hardware and lifecycle gates remain visibly separate. Timing values are planning budgets, not historical telemetry.</div>
  <section class="state-metrics" aria-label="Repository state summary"><div><strong>{satisfied}/{len(gates)}</strong><span>gates ready in software</span></div><div><strong>{accepted}/{len(migration)}</strong><span>migration decisions accepted</span></div><div><strong>{software_nodes}</strong><span>software-only checks</span></div><div><strong>{expected_total}s</strong><span>estimated serial verification time</span></div></section>
  <div class="segmented-control state-tabs" role="tablist" aria-label="Repository state view"><button type="button" role="tab" aria-selected="true" aria-controls="state-release" data-state-tab="release">Release gates</button><button type="button" role="tab" aria-selected="false" aria-controls="state-migration" data-state-tab="migration" tabindex="-1">Migration</button><button type="button" role="tab" aria-selected="false" aria-controls="state-verification" data-state-tab="verification" tabindex="-1">Verification</button><button type="button" role="tab" aria-selected="false" aria-controls="state-performance" data-state-tab="performance" tabindex="-1">Performance</button></div>
  <section id="state-release" role="tabpanel" data-state-panel="release" aria-labelledby="state-release-heading"><header class="section-heading"><div><h2 id="state-release-heading">Release gates</h2><p>{satisfied} of {len(gates)} gates are ready in software. Every other gate names the hardware evidence, lifecycle proof, or publication decision still required.</p></div><label>Show <select id="gate-state"><option value="all">All gate states</option>{gate_options}</select></label></header><progress value="{satisfied}" max="{len(gates)}">{satisfied} of {len(gates)}</progress><div class="state-details">{gate_html}</div><p id="gate-filter-status" class="filter-status" role="status" aria-live="polite">Showing all {len(gates)} gates.</p></section>
  <section id="state-migration" role="tabpanel" data-state-panel="migration" aria-labelledby="state-migration-heading" hidden><header class="section-heading"><div><h2 id="state-migration-heading">Migration decisions</h2><p>Every legacy subsystem is admitted, reimplemented, linked, or rejected explicitly. Unreviewed material remains excluded by default.</p></div><label>Show <select id="migration-state"><option value="all">All migration states</option>{migration_options}</select></label></header><div class="state-table-wrap"><table><thead><tr><th>Component</th><th>Status</th><th>Disposition</th><th>Owner</th><th>Sources</th><th>Decision</th></tr></thead><tbody>{migration_html}</tbody></table></div><p id="migration-filter-status" class="filter-status" role="status" aria-live="polite">Showing all {len(migration)} decisions.</p></section>
  <section id="state-verification" role="tabpanel" data-state-panel="verification" aria-labelledby="state-verification-heading" hidden><header class="section-heading"><div><h2 id="state-verification-heading">Verification timing budgets</h2><p>These are deterministic planning values from <code>verification/tests.json</code>, not historical run telemetry. No browser code executes tests.</p></div><div class="segmented-control lane-filter" role="group" aria-label="Verification lane"><button type="button" data-lane="all" aria-pressed="true">All</button><button type="button" data-lane="fast" aria-pressed="false">Fast</button><button type="button" data-lane="full-software" aria-pressed="false">Full software</button><button type="button" data-lane="hardware" aria-pressed="false">Hardware</button></div></header><div class="state-table-wrap"><table><thead><tr><th>Node</th><th>Domain</th><th>Timing budget</th><th>Isolation</th><th>Resume</th><th>Depends on</th></tr></thead><tbody>{verification_html}</tbody></table></div><p id="verification-filter-status" class="filter-status" role="status" aria-live="polite">Showing all {len(tests)} nodes.</p></section>
  <section id="state-performance" role="tabpanel" data-state-panel="performance" aria-labelledby="state-performance-heading" hidden><header class="section-heading"><div><h2 id="state-performance-heading">Performance boundaries</h2><p>Software-enforced limits are measured during their owning verification nodes. Physical metrics remain blocked until bounded evidence exists.</p></div></header><div class="state-table-wrap"><table><thead><tr><th>Metric</th><th>Measurement</th><th>Current static value</th><th>Maximum</th><th>Status</th><th>Why it matters</th></tr></thead><tbody>{performance_html}</tbody></table></div></section>
</article>"""

    search_records = tuple(
        [
            {
                "title": gate.title,
                "audience": "Repository State",
                "summary": f"Release gate | {_status_label(gate.status)}",
                "url": "state/index.html#state-release",
                "search": f"{gate.id} {gate.title} {gate.status} {' '.join(gate.remaining)}".lower(),
            }
            for gate in gates
        ]
        + [
            {
                "title": entry["component"],
                "audience": "Repository State",
                "summary": f"Migration | {_status_label(entry['status'])}",
                "url": "state/index.html#state-migration",
                "search": f"{entry['id']} {entry['component']} {entry['status']} {entry['disposition']} {entry['destination_owner']}".lower(),
            }
            for entry in migration
        ]
        + [
            {
                "title": node.title,
                "audience": "Repository State",
                "summary": f"Verification | {node.expected_duration_seconds}s expected",
                "url": "state/index.html#state-verification",
                "search": f"{node.id} {node.title} {node.owned_domain} {' '.join(node.lanes)}".lower(),
            }
            for node in tests
        ]
    )
    return RepositoryStatePage(content=content, search_records=search_records)


REPOSITORY_STATE_SCRIPT = """(() => {
  const root = document.querySelector('[data-repository-state]');
  if (!root) return;
  const tabs = [...root.querySelectorAll('[data-state-tab]')];
  const panels = [...root.querySelectorAll('[data-state-panel]')];
  const selectTab = (id, focus = false) => {
    tabs.forEach((tab) => {
      const selected = tab.dataset.stateTab === id;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset.statePanel !== id; });
    history.replaceState(null, '', `#state-${id}`);
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.stateTab));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let target = index;
      if (event.key === 'ArrowLeft') target = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'ArrowRight') target = (index + 1) % tabs.length;
      if (event.key === 'Home') target = 0;
      if (event.key === 'End') target = tabs.length - 1;
      selectTab(tabs[target].dataset.stateTab, true);
    });
  });

  const bindSelect = (controlId, itemSelector, messageId, noun) => {
    const control = document.getElementById(controlId);
    const items = [...root.querySelectorAll(itemSelector)];
    const message = document.getElementById(messageId);
    const apply = () => {
      let visible = 0;
      items.forEach((item) => {
        const matches = control.value === 'all' || item.dataset.state === control.value;
        item.hidden = !matches;
        if (matches) visible += 1;
      });
      message.textContent = `Showing ${visible} of ${items.length} ${noun}.`;
    };
    control.addEventListener('input', apply);
  };
  bindSelect('gate-state', '[data-gate]', 'gate-filter-status', 'gates');
  bindSelect('migration-state', '[data-migration]', 'migration-filter-status', 'decisions');

  const laneButtons = [...root.querySelectorAll('[data-lane]')];
  const nodes = [...root.querySelectorAll('[data-verification]')];
  const laneMessage = document.getElementById('verification-filter-status');
  laneButtons.forEach((button) => button.addEventListener('click', () => {
    laneButtons.forEach((candidate) => candidate.setAttribute('aria-pressed', String(candidate === button)));
    let visible = 0;
    nodes.forEach((node) => {
      const matches = button.dataset.lane === 'all' || node.dataset.lanes.split(' ').includes(button.dataset.lane);
      node.hidden = !matches;
      if (matches) visible += 1;
    });
    laneMessage.textContent = `Showing ${visible} of ${nodes.length} nodes.`;
  }));
  const selectFromHash = () => {
    const id = location.hash.startsWith('#state-') ? location.hash.slice(7) : 'release';
    if (tabs.some((tab) => tab.dataset.stateTab === id)) selectTab(id);
  };
  addEventListener('hashchange', selectFromHash);
  selectFromHash();
})();
"""


REPOSITORY_STATE_CSS = """
.repository-state { min-width: 0; }
.state-header { display: flex; align-items: start; justify-content: space-between; gap: 24px; }
.state-header h1 { margin: 0 0 10px; font-size: 34px; line-height: 1.2; }
.state-lock { flex: 0 0 auto; padding: 3px 8px; border: 1px solid var(--coral); border-radius: 4px; color: var(--coral); font-size: 12px; font-weight: 700; }
.state-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 24px 0; border: 1px solid var(--line); }
.state-metrics div { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }
.state-metrics div:last-child { border-right: 0; }
.state-metrics strong, .state-metrics span { display: block; }
.state-metrics strong { color: var(--teal); font-size: 24px; }
.state-metrics span, .filter-status { color: var(--muted); }
.segmented-control { display: inline-flex; max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 5px; background: var(--surface); }
.segmented-control button { min-height: 38px; padding: 7px 11px; border: 0; border-right: 1px solid var(--line); color: var(--muted); background: transparent; font: inherit; cursor: pointer; white-space: nowrap; }
.segmented-control button:last-child { border-right: 0; }
.segmented-control button[aria-selected="true"], .segmented-control button[aria-pressed="true"] { color: var(--ink); background: var(--surface-strong); box-shadow: inset 0 -2px var(--teal); }
.state-tabs { margin-bottom: 20px; }
.repository-state [role="tabpanel"] { min-width: 0; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin: 14px 0; }
.section-heading h2 { margin: 0; font-size: 23px; }
.section-heading p { max-width: 760px; margin: 5px 0 0; color: var(--muted); }
.section-heading label { flex: 0 0 auto; color: var(--muted); font-size: 13px; }
.section-heading select { min-height: 38px; margin-left: 6px; padding: 6px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); font: inherit; }
.repository-state progress { width: 100%; height: 8px; border: 0; border-radius: 0; color: var(--teal); background: var(--surface-strong); }
.repository-state progress::-webkit-progress-bar { background: var(--surface-strong); }
.repository-state progress::-webkit-progress-value { background: var(--teal); }
.repository-state progress::-moz-progress-bar { background: var(--teal); }
.state-details { margin-top: 12px; }
.state-detail { min-width: 0; margin-top: 7px; border: 1px solid var(--line); background: var(--surface); }
.state-detail > summary { display: flex; min-height: 52px; align-items: center; justify-content: space-between; gap: 16px; padding: 9px 12px; cursor: pointer; }
.state-detail > summary > span:first-child { min-width: 0; overflow-wrap: anywhere; }
.state-detail summary strong, .state-detail summary small, .state-table-wrap td strong, .state-table-wrap td small { display: block; }
.state-detail summary small, .state-table-wrap td small { color: var(--muted); }
.state-detail-body { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; padding: 1px; border-top: 1px solid var(--line); background: var(--line); }
.state-detail-body section { min-width: 0; padding: 12px; background: var(--bg); }
.state-detail-body h3 { margin: 0 0 8px; font-size: 14px; }
.state-detail-body ul { margin: 0; padding-left: 18px; }
.state-badge { display: inline-flex; padding: 1px 6px; border: 1px solid var(--line); border-radius: 4px; font-size: 12px; white-space: nowrap; }
.state-badge--software-satisfied, .state-badge--accepted, .state-badge--enforced-software { border-color: var(--teal); color: var(--teal); }
.state-badge--in-progress, .state-badge--blocked-by-lifecycle-evidence { border-color: var(--yellow); color: var(--yellow); }
.state-badge--pending-review, .state-badge--blocked-by-physical-evidence { border-color: var(--coral); color: var(--coral); }
.state-badge--publication-locked, .state-badge--rejected { border-color: var(--danger); color: var(--danger); }
.state-table-wrap { width: 100%; overflow: auto; border: 1px solid var(--line); }
.state-table-wrap table { width: 100%; border-collapse: collapse; font-size: 13px; }
.state-table-wrap th, .state-table-wrap td { padding: 8px 10px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.state-table-wrap tr:last-child > * { border-bottom: 0; }
.state-table-wrap tr > *:last-child { border-right: 0; }
.state-table-wrap thead th { background: var(--surface-strong); }
.timing-bar { width: 170px; max-width: 100%; height: 6px; margin: 4px 0 5px; background: var(--surface-strong); }
.timing-bar span { display: block; height: 100%; background: var(--cyan); }
.state-detail-body li, .state-detail-body code { overflow-wrap: anywhere; }
@media (max-width: 900px) {
  .state-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .state-metrics div:nth-child(2) { border-right: 0; }
  .state-metrics div:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
  .state-detail-body { grid-template-columns: 1fr; }
  .section-heading { align-items: start; flex-direction: column; }
}
@media (max-width: 560px) {
  .state-header { display: block; }
  .state-lock { display: inline-flex; margin-top: 8px; }
  .state-metrics { grid-template-columns: 1fr; }
  .state-metrics div { border-right: 0; border-bottom: 1px solid var(--line); }
  .state-metrics div:last-child { border-bottom: 0; }
  .state-detail > summary { align-items: start; flex-wrap: wrap; }
  .state-badge { max-width: 100%; white-space: normal; text-align: left; }
}
"""
