# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
from typing import Any

from .model import ModelError


@dataclass(frozen=True)
class DeviceLabPage:
    content: str
    search_records: tuple[dict[str, str], ...]


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_text(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "Not recorded"
    return str(value)


def _label(value: str) -> str:
    return value.replace("-", " ").replace(".", " / ").title()


def _badge(label: str, tone: str) -> str:
    return f'<span class="lab-badge lab-badge--{tone}">{escape(label)}</span>'


def _support(candidate: dict[str, Any]) -> str:
    return "Route qualified" if candidate["hyperflux_support"] == "route-qualified" else "Research only"


def _knowledge(candidate: dict[str, Any]) -> str:
    labels = {
        "cross-referenced": "Cross-referenced",
        "single-source": "Single pinned project",
        "conflicted": "Source conflict",
        "missing": "No exact pinned record",
    }
    return labels[candidate["knowledge_status"]]


def _candidate_search(candidate: dict[str, Any]) -> str:
    values = [
        candidate["official_name"],
        candidate["candidate_id"],
        candidate["device_kind"],
        candidate["knowledge_status"],
        candidate["hyperflux_support"],
        *candidate["hyperflux_profile_ids"],
        *candidate["sources_present"],
        *(fact["label"] for fact in candidate["reviewed_facts"]),
        *(fact["semantic_capability"] for fact in candidate["reviewed_facts"]),
        *(gap["title"] for gap in candidate["knowledge_gaps"]),
    ]
    return " ".join(values).lower()


def _inventory_row(candidate: dict[str, Any]) -> str:
    coverage = candidate["coverage"]
    support = candidate["hyperflux_support"]
    tone = "qualified" if support == "route-qualified" else "research"
    search = _candidate_search(candidate)
    return f"""<tr data-device-row data-kind="{escape(candidate['device_kind'])}" data-support="{escape(support)}" data-search="{escape(search, quote=True)}">
  <td><label class="compare-choice"><input type="checkbox" data-compare-id="{escape(candidate['candidate_id'])}"><span class="sr-only">Compare </span>{escape(candidate['official_name'])}</label></td>
  <td>{_badge(_support(candidate), tone)}</td>
  <td>{escape(_knowledge(candidate))}</td>
  <td>{coverage['reviewed_fact_count']}</td>
  <td>{coverage['implementation_record_count']}</td>
  <td>{coverage['open_gap_count']}</td>
  <td><a href="#device-{escape(candidate['candidate_id'])}">Evidence</a></td>
</tr>"""


def _fact_rows(candidate: dict[str, Any]) -> str:
    rows = []
    for fact in candidate["reviewed_facts"]:
        layers = fact["evidence_layers"]
        product = layers["product_documentation"] or layers["upstream_report"]
        rows.append(
            "<tr>"
            f"<th scope=\"row\"><strong>{escape(fact['label'])}</strong><code>{escape(fact['semantic_capability'])}</code></th>"
            f"<td>{escape(_text(fact['value']))}</td>"
            f"<td>{'Present' if product else 'Absent'}</td>"
            f"<td>{'Matched' if layers['pinned_linux_implementation'] else 'Not matched'}</td>"
            f"<td>{'Mapped' if layers['hyperflux_route_mapping'] else 'Not mapped'}</td>"
            f"<td>{'Qualified' if layers['physical_qualification'] else 'Not qualified'}</td>"
            "</tr>"
        )
    return "".join(rows)


def _gap_rows(candidate: dict[str, Any]) -> str:
    if not candidate["knowledge_gaps"]:
        return '<p class="lab-empty">No explicit knowledge gap is recorded.</p>'
    return '<div class="lab-gap-list">' + "".join(
        f'<article><header><strong>{escape(gap["title"])}</strong>'
        f'{_badge(_label(gap["status"]), "warning")}</header>'
        f'<p>{escape(gap["detail"])}</p>'
        f'<small>Sources: {escape(", ".join(gap["source_ids"]) or "none recorded")}</small></article>'
        for gap in candidate["knowledge_gaps"]
    ) + "</div>"


def _source_record_rows(candidate: dict[str, Any]) -> str:
    if not candidate["source_records"]:
        return '<p class="lab-empty">No exact OpenRazer or OpenRGB record is linked at the pinned revisions.</p>'
    rows = []
    for record in candidate["source_records"]:
        identity = record["usb_identity"]
        vendor = identity["vendor_id"]
        usb = f"{vendor:#06x}" if isinstance(vendor, int) else "unknown"
        usb += f":{identity['product_id']:#06x}"
        location = record["source_location"]
        rows.append(
            "<tr>"
            f"<td>{escape(record['record_id'].split(':', 1)[0])}</td>"
            f"<td>{escape(record['model_name'])}</td>"
            f"<td>{escape(_label(record['source_route']))}</td>"
            f"<td><code>{escape(usb)}</code></td>"
            f"<td><a href=\"{escape(record['source_url'], quote=True)}\">{escape(location['path'])}:{location['line']}</a></td>"
            "</tr>"
        )
    return "".join(rows)


def _conflicts(candidate: dict[str, Any]) -> str:
    if not candidate["source_conflicts"]:
        return '<p class="lab-empty">No material disagreement is recorded between the selected upstream records.</p>'
    return '<ul class="lab-conflicts">' + "".join(
        f'<li><strong>{escape(_label(conflict["field"]))}</strong>: '
        f'{escape(" versus ".join(_text(value) for value in conflict["values"]))} '
        f'({_label(conflict["route"])})</li>'
        for conflict in candidate["source_conflicts"]
    ) + "</ul>"


def _candidate_detail(candidate: dict[str, Any]) -> str:
    coverage = candidate["coverage"]
    profile_text = ", ".join(candidate["hyperflux_profile_ids"]) or "No receiver-backed child profile"
    setting_enabled = sum(item["control_state"] == "enabled" for item in candidate["settings"])
    gap_label = "gap" if coverage["open_gap_count"] == 1 else "gaps"
    return f"""<details class="lab-device-detail" id="device-{escape(candidate['candidate_id'])}" data-device-detail data-kind="{escape(candidate['device_kind'])}" data-support="{escape(candidate['hyperflux_support'])}" data-search="{escape(_candidate_search(candidate), quote=True)}">
  <summary><span><strong>{escape(candidate['official_name'])}</strong><small>{escape(candidate['candidate_id'])}</small></span><span>{coverage['reviewed_fact_count']} facts | {coverage['open_gap_count']} {gap_label}</span></summary>
  <div class="lab-detail-body">
    <dl class="lab-definitions">
      <div><dt>HyperFlux route</dt><dd>{escape(_support(candidate))}</dd></div>
      <div><dt>Exact profile</dt><dd><code>{escape(profile_text)}</code></dd></div>
      <div><dt>Pinned code</dt><dd>{escape(_knowledge(candidate))}</dd></div>
      <div><dt>Control projection</dt><dd>{setting_enabled} enabled of {len(candidate['settings'])} source-derived settings</dd></div>
      <div><dt>Reviewed</dt><dd>{escape(candidate['reviewed_on'])}</dd></div>
      <div><dt>Sources</dt><dd>{coverage['reviewed_source_count']} reviewed | {coverage['implementation_record_count']} exact code records</dd></div>
    </dl>
    <h3>Reviewed product facts</h3>
    <div class="lab-table-wrap"><table class="lab-fact-table"><thead><tr><th>Fact</th><th>Value</th><th>Product</th><th>Pinned Linux</th><th>Route map</th><th>Physical</th></tr></thead><tbody>{_fact_rows(candidate)}</tbody></table></div>
    <h3>Open questions and limits</h3>{_gap_rows(candidate)}
    <h3>Pinned implementation records</h3>
    <div class="lab-table-wrap"><table><thead><tr><th>Project</th><th>Model record</th><th>Route</th><th>USB identity</th><th>Exact source</th></tr></thead><tbody>{_source_record_rows(candidate)}</tbody></table></div>
    <h3>Source disagreements</h3>{_conflicts(candidate)}
  </div>
</details>"""


def _qualified_table(candidates: list[dict[str, Any]]) -> str:
    rows = []
    for candidate in candidates:
        rows.append(
            "<tr>"
            f"<th scope=\"row\"><a href=\"#device-{escape(candidate['candidate_id'])}\">{escape(candidate['official_name'])}</a></th>"
            f"<td>{escape(_label(candidate['device_kind']))}</td>"
            f"<td><code>{escape(', '.join(candidate['hyperflux_profile_ids']))}</code></td>"
            f"<td>{len(candidate['qualified_hyperflux_capabilities'])}</td>"
            f"<td>{candidate['coverage']['physically_qualified_fact_count']}</td>"
            "</tr>"
        )
    return "".join(rows)


def _matrix(candidates: list[dict[str, Any]]) -> str:
    semantic_ids = sorted(
        {fact["semantic_capability"] for candidate in candidates for fact in candidate["reviewed_facts"]}
    )
    indexes = [
        {fact["semantic_capability"]: fact for fact in candidate["reviewed_facts"]}
        for candidate in candidates
    ]
    head = "".join(
        f'<th scope="col"><span title="{escape(candidate["official_name"], quote=True)}">{escape(candidate["official_name"].replace("Razer ", ""))}</span></th>'
        for candidate in candidates
    )
    rows = []
    for semantic_id in semantic_ids:
        cells = []
        for index in indexes:
            fact = index.get(semantic_id)
            if fact is None:
                state, short = "absent", "-"
            elif fact["evidence_layers"]["physical_qualification"]:
                state, short = "qualified", "Q"
            elif fact["evidence_layers"]["pinned_linux_implementation"]:
                state, short = "implemented", "L"
            else:
                state, short = "documented", "D"
            cells.append(
                f'<td class="matrix-{state}"><span aria-label="{escape(_label(state))}" title="{escape(_label(state), quote=True)}">{short}</span></td>'
            )
        rows.append(f'<tr><th scope="row"><code>{escape(semantic_id)}</code></th>{"".join(cells)}</tr>')
    return f'<div class="lab-table-wrap lab-matrix-wrap"><table class="lab-matrix"><thead><tr><th>Semantic capability</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _source_ledger(catalog: dict[str, Any]) -> str:
    return "".join(
        "<tr>"
        f"<th scope=\"row\"><a href=\"{escape(source['url'], quote=True)}\">{escape(source['title'])}</a><code>{escape(source['id'])}</code></th>"
        f"<td>{escape(source['publisher'])}</td>"
        f"<td>{escape(_label(source['kind']))}</td>"
        f"<td>{escape(source['revision'])}</td>"
        f"<td>{escape(source['retrieved_on'])}</td>"
        "</tr>"
        for source in catalog["reviewed_sources"]
    )


def _upstream_rows(catalog: dict[str, Any]) -> str:
    return "".join(
        "<tr>"
        f"<th scope=\"row\">{escape(upstream['upstream_id'])}</th>"
        f"<td>{upstream['record_count']}</td>"
        f"<td><code>{escape(upstream['commit'])}</code></td>"
        f"<td>{escape(upstream['extractor'])}</td>"
        f"<td>{escape(upstream['license_expression'])}</td>"
        "</tr>"
        for upstream in catalog["upstreams"]
    )


def _comparison_payload(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": candidate["candidate_id"],
            "name": candidate["official_name"],
            "kind": candidate["device_kind"],
            "support": _support(candidate),
            "knowledge": _knowledge(candidate),
            "facts": candidate["coverage"]["reviewed_fact_count"],
            "records": candidate["coverage"]["implementation_record_count"],
            "qualified_facts": candidate["coverage"]["physically_qualified_fact_count"],
            "gaps": candidate["coverage"]["open_gap_count"],
            "profiles": candidate["hyperflux_profile_ids"],
            "sources": candidate["sources_present"],
        }
        for candidate in candidates
    ]


def render_device_lab(catalog: dict[str, Any]) -> DeviceLabPage:
    if catalog.get("schema") != "hyperflux-compiled-device-knowledge-v1":
        raise ModelError("Device Lab requires the compiled device-knowledge catalog")
    if catalog.get("policy", {}).get("source_knowledge_grants_transport_authority") is not False:
        raise ModelError("Device Lab refuses a catalog that escalates source knowledge")
    candidates = sorted(catalog["candidates"], key=lambda item: item["official_name"].casefold())
    qualified = [item for item in candidates if item["hyperflux_support"] == "route-qualified"]
    research = [item for item in candidates if item["hyperflux_support"] == "candidate-only"]
    facts = sum(len(item["reviewed_facts"]) for item in candidates)
    gaps = sum(len(item["knowledge_gaps"]) for item in candidates)
    conflicts = sum(len(item["source_conflicts"]) for item in candidates)
    payload = json.dumps(
        {"mode": "static-read-only", "candidates": _comparison_payload(candidates)},
        ensure_ascii=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    inventory_rows = "".join(_inventory_row(item) for item in candidates)
    details = "".join(_candidate_detail(item) for item in candidates)
    content = f"""<article class="device-lab" data-device-lab>
  <p class="breadcrumb">Research / Device Lab</p>
  <header class="lab-header">
    <div><h1>Device Lab</h1><p class="lede">A provenance-bound view of product documentation, pinned Linux registries, HyperFlux route mapping, and physical qualification.</p></div>
    {_badge('Static read-only catalog', 'static')}
  </header>
  <div class="notice"><strong>No live hardware is connected to this page.</strong> Values are generated from reviewed repository evidence. Controls are not simulated, and candidate names never authorize receiver writes.</div>
  <section class="lab-metrics" aria-label="Device knowledge summary">
    <div><strong>{len(candidates)}</strong><span>reviewed candidates</span></div><div><strong>{len(qualified)}</strong><span>route-qualified profiles</span></div><div><strong>{facts}</strong><span>reviewed facts</span></div><div><strong>{gaps}</strong><span>explicit unknowns</span></div>
  </section>
  <nav class="lab-tabs" role="tablist" aria-label="Device Lab views">
    <button type="button" role="tab" id="lab-tab-inventory" aria-controls="lab-panel-inventory" aria-selected="true" tabindex="0" data-lab-tab="inventory">Inventory</button>
    <button type="button" role="tab" id="lab-tab-compare" aria-controls="lab-panel-compare" aria-selected="false" tabindex="-1" data-lab-tab="compare">Compare</button>
    <button type="button" role="tab" id="lab-tab-matrix" aria-controls="lab-panel-matrix" aria-selected="false" tabindex="-1" data-lab-tab="matrix">Capability matrix</button>
    <button type="button" role="tab" id="lab-tab-evidence" aria-controls="lab-panel-evidence" aria-selected="false" tabindex="-1" data-lab-tab="evidence">Evidence</button>
  </nav>
  <section role="tabpanel" id="lab-panel-inventory" aria-labelledby="lab-tab-inventory" data-lab-panel="inventory">
    <div class="lab-toolbar">
      <label><span>Search candidates</span><input type="search" id="device-filter" placeholder="Model, capability, gap, or profile" autocomplete="off"></label>
      <label><span>Device kind</span><select id="device-kind-filter"><option value="all">All kinds</option><option value="keyboard">Keyboards</option><option value="mouse">Mice</option></select></label>
      <label><span>Qualification</span><select id="device-support-filter"><option value="all">All states</option><option value="route-qualified">Route qualified</option><option value="candidate-only">Research only</option></select></label>
      <button type="button" id="device-filter-clear">Clear filters</button>
    </div>
    <p id="device-filter-status" class="lab-filter-status" role="status" aria-live="polite">Showing all {len(candidates)} candidates.</p>
    <section class="lab-section" aria-labelledby="qualified-heading"><h2 id="qualified-heading">Physically qualified receiver routes</h2><p>These exact child profiles have public physical evidence for the listed HyperFlux capabilities. Product features outside those profiles remain unclaimed.</p><div class="lab-table-wrap"><table><thead><tr><th>Device</th><th>Kind</th><th>Exact child profile</th><th>Qualified capabilities</th><th>Intersecting facts</th></tr></thead><tbody>{_qualified_table(qualified)}</tbody></table></div></section>
    <section class="lab-section" aria-labelledby="inventory-heading"><h2 id="inventory-heading">Candidate inventory</h2><p>Select up to three devices for comparison. Research-only candidates stay visible without acquiring a writable route.</p><div class="lab-table-wrap"><table><thead><tr><th>Device</th><th>Route state</th><th>Pinned knowledge</th><th>Facts</th><th>Code records</th><th>Gaps</th><th>Trace</th></tr></thead><tbody>{inventory_rows}</tbody></table></div><p class="lab-empty" id="device-filter-empty" hidden>No candidates match the current filters.</p></section>
    <section class="lab-section lab-details" aria-labelledby="records-heading"><h2 id="records-heading">Candidate evidence records</h2>{details}</section>
  </section>
  <section role="tabpanel" id="lab-panel-compare" aria-labelledby="lab-tab-compare" data-lab-panel="compare" hidden>
    <h2>Device comparison</h2><p>Choose two or three candidates in Inventory. Comparison never implies compatibility or qualification.</p><p id="compare-status" role="status" aria-live="polite">No devices selected.</p><div class="lab-table-wrap"><table id="comparison-table"><thead><tr id="comparison-head"><th>Field</th></tr></thead><tbody id="comparison-body"></tbody></table></div>
  </section>
  <section role="tabpanel" id="lab-panel-matrix" aria-labelledby="lab-tab-matrix" data-lab-panel="matrix" hidden>
    <h2>Capability heatmap</h2><p>Each cell names the strongest recorded evidence layer: <strong>Q</strong> physically qualified, <strong>L</strong> exact pinned Linux implementation, <strong>D</strong> product documentation or upstream report, and <strong>-</strong> absent from reviewed facts. This is not a control matrix.</p>{_matrix(candidates)}
  </section>
  <section role="tabpanel" id="lab-panel-evidence" aria-labelledby="lab-tab-evidence" data-lab-panel="evidence" hidden>
    <h2>Evidence and provenance</h2><div class="lab-policy"><div><strong>Product</strong><span>Manufacturer documentation and clearly labeled upstream reports.</span></div><div><strong>Pinned Linux</strong><span>Exact model records parsed without executing upstream source.</span></div><div><strong>Route map</strong><span>Semantic capability intersection with an exact HyperFlux profile.</span></div><div><strong>Physical</strong><span>Public evidence attached to the exact profile capability.</span></div></div>
    <h3>Reproducible upstream imports</h3><div class="lab-table-wrap"><table><thead><tr><th>Project</th><th>Normalized records</th><th>Exact revision</th><th>Extractor</th><th>License</th></tr></thead><tbody>{_upstream_rows(catalog)}</tbody></table></div>
    <h3>Reviewed source ledger</h3><p>{len(catalog['reviewed_sources'])} reviewed sources are retained with publisher, revision, retrieval date, and URL.</p><div class="lab-table-wrap"><table><thead><tr><th>Source</th><th>Publisher</th><th>Kind</th><th>Revision</th><th>Reviewed</th></tr></thead><tbody>{_source_ledger(catalog)}</tbody></table></div>
    <h3>Recorded uncertainty</h3><p>{gaps} explicit gaps and {conflicts} selected-record conflicts remain visible. They are never promoted into controls automatically.</p>
    <details class="lab-technical"><summary>Catalog lineage and policy</summary><dl class="lab-definitions"><div><dt>Selected snapshot</dt><dd><code>{escape(catalog['candidate_snapshot_id'])}</code></dd></div><div><dt>Reviewed on</dt><dd>{escape(catalog['reviewed_on'])}</dd></div><div><dt>Source digest</dt><dd><code>{escape(catalog['source_sha256'])}</code></dd></div><div><dt>Transport authority from sources</dt><dd>Forbidden</dd></div></dl></details>
  </section>
  <script id="device-lab-data" type="application/json">{payload}</script>
</article>"""
    search_records = tuple(
        {
            "title": candidate["official_name"],
            "audience": "Device Lab",
            "summary": f"{_support(candidate)} | {candidate['device_kind']} | {candidate['coverage']['reviewed_fact_count']} facts",
            "url": f"devices/index.html#device-{candidate['candidate_id']}",
            "search": _candidate_search(candidate),
        }
        for candidate in candidates
    )
    return DeviceLabPage(content=content, search_records=search_records)


DEVICE_LAB_SCRIPT = """(() => {
  const root = document.querySelector('[data-device-lab]');
  const source = document.getElementById('device-lab-data');
  if (!root || !source) return;
  const data = JSON.parse(source.textContent || '{}');
  if (data.mode !== 'static-read-only' || !Array.isArray(data.candidates)) return;
  const index = new Map(data.candidates.map((candidate) => [candidate.id, candidate]));
  const tabs = [...root.querySelectorAll('[data-lab-tab]')];
  const panels = [...root.querySelectorAll('[data-lab-panel]')];
  const selectTab = (tab) => {
    tabs.forEach((button) => {
      const selected = button.dataset.labTab === tab;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset.labPanel !== tab; });
  };
  tabs.forEach((button, position) => {
    button.addEventListener('click', () => selectTab(button.dataset.labTab));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let target = position;
      if (event.key === 'ArrowLeft') target = (position - 1 + tabs.length) % tabs.length;
      if (event.key === 'ArrowRight') target = (position + 1) % tabs.length;
      if (event.key === 'Home') target = 0;
      if (event.key === 'End') target = tabs.length - 1;
      tabs[target].focus();
      selectTab(tabs[target].dataset.labTab);
    });
  });

  const query = document.getElementById('device-filter');
  const kind = document.getElementById('device-kind-filter');
  const support = document.getElementById('device-support-filter');
  const clear = document.getElementById('device-filter-clear');
  const status = document.getElementById('device-filter-status');
  const empty = document.getElementById('device-filter-empty');
  const rows = [...root.querySelectorAll('[data-device-row]')];
  const details = [...root.querySelectorAll('[data-device-detail]')];
  const applyFilters = () => {
    const needle = query.value.trim().toLocaleLowerCase();
    let visible = 0;
    [...rows, ...details].forEach((item) => {
      const match = (!needle || item.dataset.search.includes(needle)) &&
        (kind.value === 'all' || item.dataset.kind === kind.value) &&
        (support.value === 'all' || item.dataset.support === support.value);
      item.hidden = !match;
      if (match && item.matches('[data-device-row]')) visible += 1;
    });
    empty.hidden = visible !== 0;
    status.textContent = `Showing ${visible} of ${rows.length} candidates.`;
  };
  [query, kind, support].forEach((control) => control.addEventListener('input', applyFilters));
  clear.addEventListener('click', () => {
    query.value = '';
    kind.value = 'all';
    support.value = 'all';
    applyFilters();
    query.focus();
  });

  const choices = [...root.querySelectorAll('[data-compare-id]')];
  const compareStatus = document.getElementById('compare-status');
  const compareHead = document.getElementById('comparison-head');
  const compareBody = document.getElementById('comparison-body');
  const fields = [
    ['Kind', 'kind'], ['HyperFlux route', 'support'], ['Pinned knowledge', 'knowledge'],
    ['Reviewed facts', 'facts'], ['Exact code records', 'records'],
    ['Physically qualified facts', 'qualified_facts'], ['Open gaps', 'gaps'],
    ['Exact profiles', 'profiles'], ['Pinned projects', 'sources'],
  ];
  const cell = (tag, value) => {
    const element = document.createElement(tag);
    element.textContent = Array.isArray(value) ? (value.join(', ') || 'None') : String(value);
    return element;
  };
  const renderComparison = () => {
    const selected = choices.filter((choice) => choice.checked).map((choice) => index.get(choice.dataset.compareId));
    compareHead.replaceChildren(cell('th', 'Field'), ...selected.map((candidate) => cell('th', candidate.name)));
    compareBody.replaceChildren(...fields.map(([label, key]) => {
      const row = document.createElement('tr');
      row.append(cell('th', label), ...selected.map((candidate) => cell('td', candidate[key])));
      return row;
    }));
    compareStatus.textContent = selected.length < 2
      ? `${selected.length} selected. Choose at least two devices.`
      : `${selected.length} devices ready to compare.`;
  };
  choices.forEach((choice) => choice.addEventListener('change', () => {
    const selected = choices.filter((item) => item.checked);
    if (selected.length > 3) {
      choice.checked = false;
      compareStatus.textContent = 'Comparison is limited to three devices.';
      return;
    }
    renderComparison();
  }));
  renderComparison();
})();
"""


DEVICE_LAB_CSS = """
.device-lab { min-width: 0; }
.lab-header { display: flex; align-items: start; justify-content: space-between; gap: 24px; }
.lab-header h1 { margin: 0 0 10px; font-size: 34px; line-height: 1.2; }
.lab-badge { display: inline-flex; min-height: 26px; align-items: center; padding: 2px 8px; border: 1px solid var(--line); border-radius: 4px; color: var(--muted); font-size: 12px; font-weight: 700; white-space: nowrap; }
.lab-badge--qualified { border-color: var(--teal); color: var(--teal); }
.lab-badge--research, .lab-badge--warning, .lab-badge--static { border-color: var(--yellow); color: var(--yellow); }
.lab-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 24px 0; border: 1px solid var(--line); }
.lab-metrics div { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }
.lab-metrics div:last-child { border-right: 0; }
.lab-metrics strong, .lab-metrics span { display: block; }
.lab-metrics strong { color: var(--teal); font-size: 24px; }
.lab-metrics span { color: var(--muted); }
.lab-tabs { display: flex; overflow-x: auto; border-bottom: 1px solid var(--line); }
.lab-tabs button { min-height: 42px; padding: 8px 14px; border: 0; border-bottom: 2px solid transparent; color: var(--muted); background: transparent; font: inherit; cursor: pointer; white-space: nowrap; }
.lab-tabs button[aria-selected="true"] { border-bottom-color: var(--teal); color: var(--ink); }
[data-lab-panel] { padding-top: 22px; }
.lab-toolbar { display: grid; grid-template-columns: minmax(220px, 2fr) repeat(2, minmax(150px, 1fr)) auto; gap: 10px; align-items: end; padding: 14px; border: 1px solid var(--line); background: var(--surface); }
.lab-toolbar label span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.lab-toolbar input, .lab-toolbar select, .lab-toolbar button { width: 100%; min-height: 40px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--bg); font: inherit; }
.lab-toolbar button { cursor: pointer; }
.lab-toolbar button:hover { border-color: var(--teal); }
.lab-filter-status { color: var(--muted); }
.lab-section { margin-top: 32px; }
.lab-section > h2, [data-lab-panel] > h2 { margin: 0 0 6px; padding-bottom: 8px; border-bottom: 1px solid var(--line); font-size: 22px; }
.lab-section > p, [data-lab-panel] > p { color: var(--muted); }
.lab-table-wrap { width: 100%; overflow: auto; border: 1px solid var(--line); }
.lab-table-wrap table { width: 100%; border-collapse: collapse; font-size: 13px; }
.lab-table-wrap th, .lab-table-wrap td { min-width: 88px; padding: 8px 10px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.lab-table-wrap tr:last-child > * { border-bottom: 0; }
.lab-table-wrap tr > *:last-child { border-right: 0; }
.lab-table-wrap thead th { position: sticky; top: 0; z-index: 1; background: var(--surface-strong); }
.lab-table-wrap code, .lab-device-detail code { display: block; margin-top: 3px; color: var(--lime); font-size: 12px; overflow-wrap: anywhere; }
.compare-choice { display: grid; grid-template-columns: 18px minmax(170px, 1fr); align-items: start; gap: 8px; font-weight: 700; }
.compare-choice input { width: 16px; height: 16px; margin: 2px 0 0; accent-color: var(--teal); }
.lab-empty { padding: 14px; border: 1px dashed var(--line); color: var(--muted); }
.lab-details { display: grid; gap: 8px; }
.lab-details > h2 { margin-bottom: 4px; }
.lab-device-detail { border: 1px solid var(--line); background: var(--surface); }
.lab-device-detail > summary { display: flex; min-height: 48px; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 12px; cursor: pointer; }
.lab-device-detail > summary span:first-child strong, .lab-device-detail > summary span:first-child small { display: block; }
.lab-device-detail > summary small, .lab-device-detail > summary span:last-child { color: var(--muted); }
.lab-detail-body { padding: 4px 14px 18px; border-top: 1px solid var(--line); }
.lab-detail-body h3, [data-lab-panel] > h3 { margin: 28px 0 8px; font-size: 17px; }
.lab-definitions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; margin: 14px 0; background: var(--line); }
.lab-definitions div { min-width: 0; padding: 10px; background: var(--bg); }
.lab-definitions dt { color: var(--muted); font-size: 12px; }
.lab-definitions dd { margin: 3px 0 0; overflow-wrap: anywhere; }
.lab-gap-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.lab-gap-list article { padding: 12px; border-left: 3px solid var(--yellow); background: var(--bg); }
.lab-gap-list header { display: flex; justify-content: space-between; gap: 12px; }
.lab-gap-list p { margin: 8px 0; }
.lab-gap-list small { color: var(--muted); }
.lab-conflicts { border-left: 3px solid var(--coral); }
.lab-fact-table th:first-child { min-width: 210px; }
.lab-matrix-wrap { max-height: 70vh; }
.lab-matrix th:first-child { position: sticky; left: 0; z-index: 2; min-width: 220px; background: var(--surface-strong); }
.lab-matrix thead th { min-width: 130px; }
.lab-matrix td { text-align: center; font-weight: 800; }
.matrix-qualified { color: var(--teal); background: color-mix(in srgb, var(--teal) 12%, transparent); }
.matrix-implemented { color: var(--cyan); background: color-mix(in srgb, var(--cyan) 10%, transparent); }
.matrix-documented { color: var(--yellow); background: color-mix(in srgb, var(--yellow) 8%, transparent); }
.matrix-absent { color: var(--muted); }
.lab-policy { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); }
.lab-policy div { padding: 12px; border-right: 1px solid var(--line); }
.lab-policy div:last-child { border-right: 0; }
.lab-policy strong, .lab-policy span { display: block; }
.lab-policy span { margin-top: 4px; color: var(--muted); }
.lab-technical { margin-top: 24px; border: 1px solid var(--line); }
.lab-technical > summary { min-height: 44px; padding: 9px 12px; cursor: pointer; font-weight: 700; }
.lab-tabs button:focus-visible, .lab-toolbar button:focus-visible, .lab-toolbar select:focus-visible, .compare-choice input:focus-visible { outline: 2px solid var(--yellow); outline-offset: 2px; }
@media (max-width: 900px) {
  .lab-metrics, .lab-policy { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .lab-metrics div:nth-child(2) { border-right: 0; }
  .lab-metrics div:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
  .lab-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .lab-definitions { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .lab-header { display: grid; }
  .lab-metrics, .lab-policy, .lab-toolbar, .lab-definitions, .lab-gap-list { grid-template-columns: 1fr; }
  .lab-metrics div, .lab-policy div { border-right: 0; border-bottom: 1px solid var(--line); }
  .lab-metrics div:last-child, .lab-policy div:last-child { border-bottom: 0; }
  .lab-device-detail > summary { align-items: start; flex-direction: column; }
}
"""
