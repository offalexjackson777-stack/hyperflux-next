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


def _support(candidate: dict[str, Any]) -> str:
    return (
        "Tested through HyperFlux"
        if candidate["hyperflux_support"] == "route-qualified"
        else "Research candidate"
    )


def _knowledge(candidate: dict[str, Any]) -> str:
    labels = {
        "cross-referenced": "Matched across pinned projects",
        "single-source": "One pinned project",
        "conflicted": "Sources disagree",
        "missing": "No exact pinned record",
    }
    return labels[candidate["knowledge_status"]]


def _badge(label: str, tone: str) -> str:
    return f'<span class="lab-badge lab-badge--{tone}">{escape(label)}</span>'


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
        *(setting["semantic_capability"] for setting in candidate["settings"]),
        *(gap["title"] for gap in candidate["knowledge_gaps"]),
    ]
    return " ".join(values).casefold()


def _candidate_row(candidate: dict[str, Any]) -> str:
    support = candidate["hyperflux_support"]
    tone = "qualified" if support == "route-qualified" else "research"
    coverage = candidate["coverage"]
    gap_count = coverage["open_gap_count"]
    gap_label = "gap" if gap_count == 1 else "gaps"
    return f"""<div class="lab-device-row" data-device-row data-kind="{escape(candidate['device_kind'])}" data-support="{escape(support)}" data-search="{escape(_candidate_search(candidate), quote=True)}">
  <label class="compare-choice" title="Add to comparison"><input type="checkbox" data-compare-id="{escape(candidate['candidate_id'])}"><span class="sr-only">Compare {escape(candidate['official_name'])}</span></label>
  <button type="button" data-device-select="{escape(candidate['candidate_id'])}"><span><strong>{escape(candidate['official_name'])}</strong><small>{escape(_label(candidate['device_kind']))} | {coverage['reviewed_fact_count']} facts | {gap_count} known {gap_label}</small></span>{_badge(_support(candidate), tone)}</button>
</div>"""


def _fact_rows(candidate: dict[str, Any]) -> str:
    rows = []
    for fact in candidate["reviewed_facts"]:
        layers = fact["evidence_layers"]
        strongest = (
            "Physically verified"
            if layers["physical_qualification"]
            else "Mapped to HyperFlux"
            if layers["hyperflux_route_mapping"]
            else "Pinned Linux implementation"
            if layers["pinned_linux_implementation"]
            else "Product or upstream documentation"
        )
        rows.append(
            "<tr>"
            f'<th scope="row"><strong>{escape(fact["label"])}</strong><code>{escape(fact["semantic_capability"])}</code></th>'
            f'<td>{escape(_text(fact["value"]))}</td><td>{escape(strongest)}</td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="3">No reviewed facts.</td></tr>'


def _setting_rows(candidate: dict[str, Any]) -> str:
    rows = []
    for setting in candidate["settings"]:
        available = setting["control_state"] == "enabled"
        rows.append(
            "<tr>"
            f'<th scope="row"><strong>{escape(_label(setting["id"]))}</strong><code>{escape(setting["semantic_capability"])}</code></th>'
            f'<td>{escape(_label(setting["access"]))}</td>'
            f'<td>{"Available in the qualified profile" if available else "Known upstream; not available through HyperFlux"}</td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="3">No source-derived settings.</td></tr>'


def _gaps(candidate: dict[str, Any]) -> str:
    if not candidate["knowledge_gaps"]:
        return '<p class="lab-empty">No explicit knowledge gap is recorded.</p>'
    return '<div class="lab-gap-list">' + "".join(
        f'<article><strong>{escape(gap["title"])}</strong><p>{escape(gap["detail"])}</p>'
        f'<small>{escape(_label(gap["status"]))}</small></article>'
        for gap in candidate["knowledge_gaps"]
    ) + "</div>"


def _source_rows(candidate: dict[str, Any]) -> str:
    rows = []
    for record in candidate["source_records"]:
        identity = record["usb_identity"]
        vendor = identity["vendor_id"]
        usb = f"{vendor:#06x}" if isinstance(vendor, int) else "unknown"
        usb += f":{identity['product_id']:#06x}"
        location = record["source_location"]
        rows.append(
            "<tr>"
            f'<td>{escape(record["record_id"].split(":", 1)[0].title())}</td>'
            f'<td>{escape(record["model_name"])}</td><td>{escape(_label(record["source_route"]))}</td>'
            f'<td><code>{escape(usb)}</code></td><td><a href="{escape(record["source_url"], quote=True)}">{escape(location["path"])}:{location["line"]}</a></td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="5">No exact pinned implementation record.</td></tr>'


def _detail_html(candidate: dict[str, Any]) -> str:
    coverage = candidate["coverage"]
    support = candidate["hyperflux_support"]
    tone = "qualified" if support == "route-qualified" else "research"
    profiles = ", ".join(candidate["hyperflux_profile_ids"]) or "No qualified receiver profile"
    support_note = (
        "This exact profile has physical receiver-route evidence for the capabilities marked available below."
        if support == "route-qualified"
        else "This model is documented for research. Its upstream features do not authorize receiver writes."
    )
    return f"""<header class="lab-detail-header"><div><p class="page-kicker">{escape(_label(candidate['device_kind']))}</p><h2>{escape(candidate['official_name'])}</h2></div>{_badge(_support(candidate), tone)}</header>
<p class="lab-support-note">{escape(support_note)}</p>
<dl class="lab-definitions"><div><dt>Knowledge</dt><dd>{escape(_knowledge(candidate))}</dd></div><div><dt>HyperFlux profile</dt><dd><code>{escape(profiles)}</code></dd></div><div><dt>Reviewed</dt><dd>{escape(candidate['reviewed_on'])}</dd></div><div><dt>Facts</dt><dd>{coverage['reviewed_fact_count']}</dd></div><div><dt>Exact source records</dt><dd>{coverage['implementation_record_count']}</dd></div><div><dt>Known gaps</dt><dd>{coverage['open_gap_count']}</dd></div></dl>
<section><h3>Capabilities and evidence</h3><p>Each row shows the strongest evidence recorded for that fact.</p><div class="lab-table-wrap"><table><thead><tr><th>Capability</th><th>Recorded value</th><th>Strongest evidence</th></tr></thead><tbody>{_fact_rows(candidate)}</tbody></table></div></section>
<section><h3>Settings described by pinned projects</h3><p>Known upstream controls remain unavailable until the HyperFlux receiver route is implemented and qualified.</p><div class="lab-table-wrap"><table><thead><tr><th>Setting</th><th>Access</th><th>HyperFlux state</th></tr></thead><tbody>{_setting_rows(candidate)}</tbody></table></div></section>
<section><h3>Known limitations</h3>{_gaps(candidate)}</section>
<details class="lab-technical"><summary>Implementation sources</summary><div class="lab-table-wrap"><table><thead><tr><th>Project</th><th>Model record</th><th>Route</th><th>USB identity</th><th>Exact source</th></tr></thead><tbody>{_source_rows(candidate)}</tbody></table></div></details>"""


def _payload_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": candidate["candidate_id"],
        "name": candidate["official_name"],
        "kind": candidate["device_kind"],
        "support": _support(candidate),
        "support_id": candidate["hyperflux_support"],
        "knowledge": _knowledge(candidate),
        "reviewed_on": candidate["reviewed_on"],
        "profiles": candidate["hyperflux_profile_ids"],
        "sources": candidate["sources_present"],
        "coverage": candidate["coverage"],
        "facts": [
            {
                "label": fact["label"],
                "capability": fact["semantic_capability"],
                "value": _text(fact["value"]),
                "layers": fact["evidence_layers"],
            }
            for fact in candidate["reviewed_facts"]
        ],
        "settings": [
            {
                "id": setting["id"],
                "capability": setting["semantic_capability"],
                "access": setting["access"],
                "state": setting["control_state"],
            }
            for setting in candidate["settings"]
        ],
        "gaps": [
            {"title": gap["title"], "detail": gap["detail"], "status": gap["status"]}
            for gap in candidate["knowledge_gaps"]
        ],
        "records": [
            {
                "project": record["record_id"].split(":", 1)[0].title(),
                "model": record["model_name"],
                "route": _label(record["source_route"]),
                "usb": (
                    (f"{record['usb_identity']['vendor_id']:#06x}" if isinstance(record["usb_identity"]["vendor_id"], int) else "unknown")
                    + f":{record['usb_identity']['product_id']:#06x}"
                ),
                "location": f"{record['source_location']['path']}:{record['source_location']['line']}",
                "url": record["source_url"],
            }
            for record in candidate["source_records"]
        ],
    }


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
        f'<th scope="row"><a href="{escape(source["url"], quote=True)}">{escape(source["title"])}</a><code>{escape(source["id"])}</code></th>'
        f'<td>{escape(source["publisher"])}</td><td>{escape(_label(source["kind"]))}</td>'
        f'<td>{escape(source["revision"])}</td><td>{escape(source["retrieved_on"])}</td></tr>'
        for source in catalog["reviewed_sources"]
    )


def _upstream_rows(catalog: dict[str, Any]) -> str:
    return "".join(
        "<tr>"
        f'<th scope="row">{escape(upstream["upstream_id"])}</th><td>{upstream["record_count"]}</td>'
        f'<td><code>{escape(upstream["commit"])}</code></td><td>{escape(upstream["extractor"])}</td>'
        f'<td>{escape(upstream["license_expression"])}</td></tr>'
        for upstream in catalog["upstreams"]
    )


def render_device_lab(catalog: dict[str, Any]) -> DeviceLabPage:
    if catalog.get("schema") != "hyperflux-compiled-device-knowledge-v1":
        raise ModelError("Device Lab requires the compiled device-knowledge catalog")
    if catalog.get("policy", {}).get("source_knowledge_grants_transport_authority") is not False:
        raise ModelError("Device Lab refuses a catalog that escalates source knowledge")
    candidates = sorted(catalog["candidates"], key=lambda item: item["official_name"].casefold())
    qualified = [item for item in candidates if item["hyperflux_support"] == "route-qualified"]
    initial = qualified[0] if qualified else candidates[0]
    facts = sum(len(item["reviewed_facts"]) for item in candidates)
    gaps = sum(len(item["knowledge_gaps"]) for item in candidates)
    conflicts = sum(len(item["source_conflicts"]) for item in candidates)
    payload = json.dumps(
        {"mode": "static-read-only", "candidates": [_payload_candidate(item) for item in candidates]},
        ensure_ascii=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    rows = "".join(_candidate_row(item) for item in candidates)
    content = f"""<article class="device-lab" data-device-lab data-initial-device="{escape(initial['candidate_id'])}">
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="../index.html">Home</a><span>Device Lab</span></nav>
  <header class="page-hero page-hero--reference"><p class="page-kicker">Hardware evidence catalog</p><h1>Device Lab</h1><p class="lede">Explore what is tested through HyperFlux, what upstream projects document, and which capabilities still require implementation or physical evidence.</p></header>
  <div class="notice"><strong>Evidence catalog, not a control panel.</strong> Model research never authorizes hardware writes. Only exact qualified profiles expose receiver capabilities.</div>
  <section class="lab-metrics" aria-label="Device knowledge summary"><div><strong>{len(candidates)}</strong><span>reviewed models</span></div><div><strong>{len(qualified)}</strong><span>tested through HyperFlux</span></div><div><strong>{facts}</strong><span>reviewed device facts</span></div><div><strong>{gaps}</strong><span>known limitations</span></div></section>
  <nav class="lab-tabs" role="tablist" aria-label="Device Lab views"><button type="button" role="tab" id="lab-tab-inventory" aria-controls="lab-panel-inventory" aria-selected="true" tabindex="0" data-lab-tab="inventory">Models</button><button type="button" role="tab" id="lab-tab-compare" aria-controls="lab-panel-compare" aria-selected="false" tabindex="-1" data-lab-tab="compare">Compare</button><button type="button" role="tab" id="lab-tab-matrix" aria-controls="lab-panel-matrix" aria-selected="false" tabindex="-1" data-lab-tab="matrix">Capabilities</button><button type="button" role="tab" id="lab-tab-evidence" aria-controls="lab-panel-evidence" aria-selected="false" tabindex="-1" data-lab-tab="evidence">Sources</button></nav>
  <section role="tabpanel" id="lab-panel-inventory" aria-labelledby="lab-tab-inventory" data-lab-panel="inventory"><div class="lab-toolbar"><label><span>Find a model</span><input type="search" id="device-filter" placeholder="Model, capability, setting, or gap" autocomplete="off"></label><label><span>Device kind</span><select id="device-kind-filter"><option value="all">All kinds</option><option value="keyboard">Keyboards</option><option value="mouse">Mice</option></select></label><label><span>Evidence state</span><select id="device-support-filter"><option value="all">All models</option><option value="route-qualified">Tested through HyperFlux</option><option value="candidate-only">Research candidates</option></select></label><button type="button" id="device-filter-clear">Clear</button></div><p id="device-filter-status" class="lab-filter-status" role="status" aria-live="polite">Showing all {len(candidates)} models.</p><div class="lab-browser"><aside class="lab-device-list" aria-label="Reviewed device models">{rows}<p class="lab-empty" id="device-filter-empty" hidden>No models match the current filters.</p></aside><section class="lab-selected-detail" id="lab-selected-detail" tabindex="-1">{_detail_html(initial)}</section></div></section>
  <section role="tabpanel" id="lab-panel-compare" aria-labelledby="lab-tab-compare" data-lab-panel="compare" hidden><div class="section-intro"><p class="page-kicker">Side by side</p><h2>Compare reviewed models</h2><p>Select two or three models in the Models view. Comparison reports evidence; it does not imply compatibility.</p></div><p id="compare-status" role="status" aria-live="polite">No models selected.</p><div class="lab-table-wrap"><table id="comparison-table"><thead><tr id="comparison-head"><th>Field</th></tr></thead><tbody id="comparison-body"></tbody></table></div></section>
  <section role="tabpanel" id="lab-panel-matrix" aria-labelledby="lab-tab-matrix" data-lab-panel="matrix" hidden><div class="section-intro"><p class="page-kicker">Evidence heatmap</p><h2>Capability coverage</h2><p><strong>Q</strong> physically qualified, <strong>L</strong> pinned Linux implementation, <strong>D</strong> documented, and <strong>-</strong> absent from reviewed facts.</p></div>{_matrix(candidates)}</section>
  <section role="tabpanel" id="lab-panel-evidence" aria-labelledby="lab-tab-evidence" data-lab-panel="evidence" hidden><div class="section-intro"><p class="page-kicker">Reproducible provenance</p><h2>Sources and evidence policy</h2><p>Product documentation, pinned project records, HyperFlux route maps, and physical qualification remain separate evidence layers.</p></div><div class="lab-policy"><div><strong>Product</strong><span>Manufacturer material or labeled upstream reports.</span></div><div><strong>Pinned Linux</strong><span>Exact records parsed without executing upstream code.</span></div><div><strong>Route map</strong><span>Capabilities intersected with an exact HyperFlux profile.</span></div><div><strong>Physical</strong><span>Evidence attached to the exact routed capability.</span></div></div><h3>Reproducible imports</h3><div class="lab-table-wrap"><table><thead><tr><th>Project</th><th>Records</th><th>Revision</th><th>Extractor</th><th>License</th></tr></thead><tbody>{_upstream_rows(catalog)}</tbody></table></div><h3>Reviewed source ledger</h3><div class="lab-table-wrap"><table><thead><tr><th>Source</th><th>Publisher</th><th>Kind</th><th>Revision</th><th>Reviewed</th></tr></thead><tbody>{_source_ledger(catalog)}</tbody></table></div><p class="lab-source-note">{gaps} known limitations and {conflicts} selected-record conflicts remain explicit. Source knowledge never becomes transport authority automatically.</p></section>
  <script id="device-lab-data" type="application/json">{payload}</script>
</article>"""
    search_records = tuple(
        {
            "title": candidate["official_name"],
            "audience": "Device Lab",
            "summary": f"{_support(candidate)} | {_label(candidate['device_kind'])}",
            "url": f"devices/index.html#device-{candidate['candidate_id']}",
            "search": _candidate_search(candidate),
        }
        for candidate in candidates
    )
    return DeviceLabPage(content=content, search_records=search_records)


DEVICE_LAB_SCRIPT = r"""(() => {
  const root = document.querySelector('[data-device-lab]');
  const source = document.getElementById('device-lab-data');
  if (!root || !source) return;
  const data = JSON.parse(source.textContent || '{}');
  if (data.mode !== 'static-read-only' || !Array.isArray(data.candidates)) return;
  const index = new Map(data.candidates.map((candidate) => [candidate.id, candidate]));
  const escapeHtml = (value) => String(value).replace(/[&<>\"']/g, (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[character]));
  const label = (value) => String(value).replaceAll('-', ' ').replaceAll('.', ' / ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  const strongest = (layers) => layers.physical_qualification ? 'Physically verified' : layers.hyperflux_route_mapping ? 'Mapped to HyperFlux' : layers.pinned_linux_implementation ? 'Pinned Linux implementation' : 'Product or upstream documentation';
  const rows = (values, render, empty, columns) => values.length ? values.map(render).join('') : `<tr><td colspan="${columns}">${empty}</td></tr>`;
  const detail = document.getElementById('lab-selected-detail');
  const deviceRows = [...root.querySelectorAll('[data-device-row]')];
  const selectButtons = [...root.querySelectorAll('[data-device-select]')];
  let selectedId = root.dataset.initialDevice;
  const renderDetail = (candidate, updateHash = true) => {
    const qualified = candidate.support_id === 'route-qualified';
    const profiles = candidate.profiles.join(', ') || 'No qualified receiver profile';
    const supportNote = qualified ? 'This exact profile has physical receiver-route evidence for the capabilities marked available below.' : 'This model is documented for research. Its upstream features do not authorize receiver writes.';
    const factRows = rows(candidate.facts, (fact) => `<tr><th scope="row"><strong>${escapeHtml(fact.label)}</strong><code>${escapeHtml(fact.capability)}</code></th><td>${escapeHtml(fact.value)}</td><td>${escapeHtml(strongest(fact.layers))}</td></tr>`, 'No reviewed facts.', 3);
    const settingRows = rows(candidate.settings, (setting) => `<tr><th scope="row"><strong>${escapeHtml(label(setting.id))}</strong><code>${escapeHtml(setting.capability)}</code></th><td>${escapeHtml(label(setting.access))}</td><td>${setting.state === 'enabled' ? 'Available in the qualified profile' : 'Known upstream; not available through HyperFlux'}</td></tr>`, 'No source-derived settings.', 3);
    const gaps = candidate.gaps.length ? `<div class="lab-gap-list">${candidate.gaps.map((gap) => `<article><strong>${escapeHtml(gap.title)}</strong><p>${escapeHtml(gap.detail)}</p><small>${escapeHtml(label(gap.status))}</small></article>`).join('')}</div>` : '<p class="lab-empty">No explicit knowledge gap is recorded.</p>';
    const sourceRows = rows(candidate.records, (record) => `<tr><td>${escapeHtml(record.project)}</td><td>${escapeHtml(record.model)}</td><td>${escapeHtml(record.route)}</td><td><code>${escapeHtml(record.usb)}</code></td><td><a href="${escapeHtml(record.url)}">${escapeHtml(record.location)}</a></td></tr>`, 'No exact pinned implementation record.', 5);
    detail.innerHTML = `<header class="lab-detail-header"><div><p class="page-kicker">${escapeHtml(label(candidate.kind))}</p><h2>${escapeHtml(candidate.name)}</h2></div><span class="lab-badge lab-badge--${qualified ? 'qualified' : 'research'}">${escapeHtml(candidate.support)}</span></header><p class="lab-support-note">${escapeHtml(supportNote)}</p><dl class="lab-definitions"><div><dt>Knowledge</dt><dd>${escapeHtml(candidate.knowledge)}</dd></div><div><dt>HyperFlux profile</dt><dd><code>${escapeHtml(profiles)}</code></dd></div><div><dt>Reviewed</dt><dd>${escapeHtml(candidate.reviewed_on)}</dd></div><div><dt>Facts</dt><dd>${candidate.coverage.reviewed_fact_count}</dd></div><div><dt>Exact source records</dt><dd>${candidate.coverage.implementation_record_count}</dd></div><div><dt>Known gaps</dt><dd>${candidate.coverage.open_gap_count}</dd></div></dl><section><h3>Capabilities and evidence</h3><p>Each row shows the strongest evidence recorded for that fact.</p><div class="lab-table-wrap"><table><thead><tr><th>Capability</th><th>Recorded value</th><th>Strongest evidence</th></tr></thead><tbody>${factRows}</tbody></table></div></section><section><h3>Settings described by pinned projects</h3><p>Known upstream controls remain unavailable until the HyperFlux receiver route is implemented and qualified.</p><div class="lab-table-wrap"><table><thead><tr><th>Setting</th><th>Access</th><th>HyperFlux state</th></tr></thead><tbody>${settingRows}</tbody></table></div></section><section><h3>Known limitations</h3>${gaps}</section><details class="lab-technical"><summary>Implementation sources</summary><div class="lab-table-wrap"><table><thead><tr><th>Project</th><th>Model record</th><th>Route</th><th>USB identity</th><th>Exact source</th></tr></thead><tbody>${sourceRows}</tbody></table></div></details>`;
    selectedId = candidate.id;
    selectButtons.forEach((button) => button.setAttribute('aria-current', String(button.dataset.deviceSelect === candidate.id)));
    if (updateHash) history.replaceState(null, '', `#device-${candidate.id}`);
  };
  selectButtons.forEach((button) => button.addEventListener('click', () => renderDetail(index.get(button.dataset.deviceSelect))));

  const tabs = [...root.querySelectorAll('[data-lab-tab]')];
  const panels = [...root.querySelectorAll('[data-lab-panel]')];
  const selectTab = (tab) => {
    tabs.forEach((button) => { const selected = button.dataset.labTab === tab; button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; });
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
      tabs[target].focus(); selectTab(tabs[target].dataset.labTab);
    });
  });

  const query = document.getElementById('device-filter');
  const kind = document.getElementById('device-kind-filter');
  const support = document.getElementById('device-support-filter');
  const clear = document.getElementById('device-filter-clear');
  const status = document.getElementById('device-filter-status');
  const empty = document.getElementById('device-filter-empty');
  const applyFilters = () => {
    const needle = query.value.trim().toLocaleLowerCase(); const visibleRows = [];
    deviceRows.forEach((item) => {
      const match = (!needle || item.dataset.search.includes(needle)) && (kind.value === 'all' || item.dataset.kind === kind.value) && (support.value === 'all' || item.dataset.support === support.value);
      item.hidden = !match; if (match) visibleRows.push(item);
    });
    const preferred = HyperFluxPortal.preferredVisible({items: visibleRows, selectedId, needle, id: (item) => item.querySelector('[data-device-select]').dataset.deviceSelect, title: (item) => index.get(item.querySelector('[data-device-select]').dataset.deviceSelect).name});
    if (preferred) renderDetail(index.get(preferred.querySelector('[data-device-select]').dataset.deviceSelect));
    detail.hidden = visibleRows.length === 0;
    empty.hidden = visibleRows.length !== 0; status.textContent = `Showing ${visibleRows.length} of ${deviceRows.length} models.`;
  };
  [query, kind, support].forEach((control) => control.addEventListener('input', applyFilters));
  clear.addEventListener('click', () => { query.value = ''; kind.value = 'all'; support.value = 'all'; applyFilters(); query.focus(); });

  const choices = [...root.querySelectorAll('[data-compare-id]')];
  const compareStatus = document.getElementById('compare-status');
  const compareHead = document.getElementById('comparison-head');
  const compareBody = document.getElementById('comparison-body');
  const fields = [['Kind', 'kind'], ['HyperFlux evidence', 'support'], ['Pinned knowledge', 'knowledge'], ['Reviewed facts', 'facts'], ['Exact source records', 'records'], ['Physically qualified facts', 'qualified_facts'], ['Known gaps', 'gaps'], ['Exact profiles', 'profiles'], ['Pinned projects', 'sources']];
  const comparisonValue = (candidate, key) => ({facts:candidate.coverage.reviewed_fact_count,records:candidate.coverage.implementation_record_count,qualified_facts:candidate.coverage.physically_qualified_fact_count,gaps:candidate.coverage.open_gap_count}[key] ?? candidate[key]);
  const cell = (tag, value) => { const element = document.createElement(tag); element.textContent = Array.isArray(value) ? (value.join(', ') || 'None') : String(value); return element; };
  const renderComparison = () => {
    const selected = choices.filter((choice) => choice.checked).map((choice) => index.get(choice.dataset.compareId));
    compareHead.replaceChildren(cell('th', 'Field'), ...selected.map((candidate) => cell('th', candidate.name)));
    compareBody.replaceChildren(...fields.map(([field, key]) => { const row = document.createElement('tr'); row.append(cell('th', field), ...selected.map((candidate) => cell('td', comparisonValue(candidate, key)))); return row; }));
    compareStatus.textContent = selected.length < 2 ? `${selected.length} selected. Choose at least two models.` : `${selected.length} models ready to compare.`;
  };
  choices.forEach((choice) => choice.addEventListener('change', () => { if (choices.filter((item) => item.checked).length > 3) { choice.checked = false; compareStatus.textContent = 'Comparison is limited to three models.'; return; } renderComparison(); }));
  renderComparison();
  const requested = location.hash.startsWith('#device-') ? location.hash.slice(8) : '';
  if (index.has(requested)) renderDetail(index.get(requested), false);
})();
"""


DEVICE_LAB_CSS = """
.device-lab { min-width: 0; }
.lab-badge { display: inline-flex; min-height: 25px; align-items: center; padding: 2px 7px; border: 1px solid var(--line); border-radius: 3px; color: var(--muted); font-size: 11px; font-weight: 700; white-space: nowrap; }
.lab-badge--qualified { border-color: var(--teal); color: var(--teal); }
.lab-badge--research { border-color: var(--yellow); color: var(--yellow); }
.lab-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 28px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.lab-metrics div { padding: 16px 18px; border-right: 1px solid var(--line); }
.lab-metrics div:last-child { border-right: 0; }
.lab-metrics strong, .lab-metrics span { display: block; }
.lab-metrics strong { color: var(--teal); font: 700 26px/1 var(--display-font); }
.lab-metrics span { margin-top: 6px; color: var(--muted); font-size: 13px; }
.lab-tabs { display: flex; max-width: 100%; overflow-x: auto; border-bottom: 1px solid var(--line); }
.lab-tabs button { min-height: 43px; padding: 8px 15px; border: 0; border-bottom: 2px solid transparent; color: var(--muted); background: transparent; cursor: pointer; white-space: nowrap; }
.lab-tabs button[aria-selected="true"] { border-bottom-color: var(--teal); color: var(--ink); }
[data-lab-panel] { padding-top: 22px; }
.lab-toolbar { display: grid; grid-template-columns: minmax(220px, 2fr) repeat(2, minmax(150px, 1fr)) auto; gap: 10px; align-items: end; padding-bottom: 16px; border-bottom: 1px solid var(--line); }
.lab-toolbar label span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.lab-toolbar input, .lab-toolbar select, .lab-toolbar button { width: 100%; min-height: 40px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); }
.lab-toolbar button { cursor: pointer; }
.lab-filter-status, .lab-source-note { color: var(--muted); }
.lab-browser { display: grid; grid-template-columns: minmax(270px, 350px) minmax(0, 1fr); min-height: 680px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.lab-device-list { min-width: 0; max-height: 760px; overflow-y: auto; padding: 12px 14px 12px 0; border-right: 1px solid var(--line); scrollbar-width: thin; }
.lab-device-row { display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: center; border-bottom: 1px solid var(--line-soft); }
.compare-choice { display: grid; place-items: center; }
.compare-choice input { width: 16px; height: 16px; accent-color: var(--teal); }
.lab-device-row > button { display: flex; min-width: 0; min-height: 68px; align-items: center; justify-content: space-between; gap: 8px; padding: 9px 8px; border: 0; border-left: 2px solid transparent; color: var(--muted); background: transparent; text-align: left; cursor: pointer; }
.lab-device-row > button span:first-child { min-width: 0; }
.lab-device-row > button strong, .lab-device-row > button small { display: block; }
.lab-device-row > button strong { color: var(--ink); }
.lab-device-row > button small { margin-top: 4px; color: var(--muted); font-size: 11px; }
.lab-device-row > button:hover, .lab-device-row > button[aria-current="true"] { border-left-color: var(--teal); background: var(--surface); }
.lab-selected-detail { min-width: 0; padding: 24px 0 46px 30px; }
.lab-detail-header { display: flex; align-items: start; justify-content: space-between; gap: 16px; }
.lab-detail-header h2 { margin: 3px 0 0; font: 700 27px/1.2 var(--display-font); }
.lab-support-note { max-width: 780px; color: var(--muted); }
.lab-definitions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 20px 0 30px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.lab-definitions div { min-width: 0; padding: 11px 12px; border-right: 1px solid var(--line); }
.lab-definitions div:nth-child(3n) { border-right: 0; }
.lab-definitions div:nth-child(-n + 3) { border-bottom: 1px solid var(--line); }
.lab-definitions dt { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.lab-definitions dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.lab-selected-detail h3, [data-lab-panel] > h3 { margin: 34px 0 7px; font: 700 17px/1.3 var(--display-font); }
.lab-selected-detail section > p { color: var(--muted); }
.lab-table-wrap { width: 100%; overflow: auto; border: 1px solid var(--line); }
.lab-table-wrap table { width: 100%; min-width: 620px; border-collapse: collapse; font-size: 13px; }
.lab-table-wrap th, .lab-table-wrap td { padding: 8px 10px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
.lab-table-wrap tr:last-child > * { border-bottom: 0; }
.lab-table-wrap tr > *:last-child { border-right: 0; }
.lab-table-wrap thead th { position: sticky; top: 0; z-index: 1; background: var(--surface-strong); }
.lab-table-wrap th strong, .lab-table-wrap th code { display: block; }
.lab-table-wrap th code { margin-top: 3px; font-size: 11px; }
.lab-gap-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.lab-gap-list article { padding: 12px 14px; border-left: 3px solid var(--yellow); background: var(--surface); }
.lab-gap-list p { margin: 6px 0; color: var(--muted); }
.lab-gap-list small { color: var(--yellow); }
.lab-empty { padding: 14px; border: 1px dashed var(--line); color: var(--muted); }
.lab-technical { margin-top: 30px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.lab-technical > summary { min-height: 44px; padding: 10px 0; cursor: pointer; font-weight: 700; }
.lab-policy { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 30px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.lab-policy div { padding: 13px; border-right: 1px solid var(--line); }
.lab-policy div:last-child { border-right: 0; }
.lab-policy strong, .lab-policy span { display: block; }
.lab-policy span { margin-top: 4px; color: var(--muted); font-size: 12px; }
.lab-matrix-wrap { max-height: 70vh; }
.lab-matrix th:first-child { position: sticky; left: 0; z-index: 2; min-width: 220px; background: var(--surface-strong); }
.lab-matrix thead th { min-width: 130px; }
.lab-matrix td { text-align: center; font-weight: 800; }
.matrix-qualified { color: var(--teal); background: color-mix(in srgb, var(--teal) 12%, transparent); }
.matrix-implemented { color: var(--cyan); background: color-mix(in srgb, var(--cyan) 10%, transparent); }
.matrix-documented { color: var(--yellow); background: color-mix(in srgb, var(--yellow) 8%, transparent); }
.matrix-absent { color: var(--muted); }
@media (max-width: 980px) {
  .lab-browser { grid-template-columns: 1fr; }
  .lab-device-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 360px; padding-right: 0; border-right: 0; border-bottom: 1px solid var(--line); }
  .lab-selected-detail { padding-left: 0; }
}
@media (max-width: 760px) {
  .lab-metrics, .lab-policy { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .lab-metrics div:nth-child(2), .lab-policy div:nth-child(2) { border-right: 0; }
  .lab-metrics div:nth-child(-n + 2), .lab-policy div:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
  .lab-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .lab-metrics, .lab-policy, .lab-toolbar, .lab-device-list, .lab-definitions, .lab-gap-list { grid-template-columns: 1fr; }
  .lab-metrics div, .lab-policy div, .lab-metrics div:nth-child(2), .lab-policy div:nth-child(2) { border-right: 0; border-bottom: 1px solid var(--line); }
  .lab-metrics div:last-child, .lab-policy div:last-child { border-bottom: 0; }
  .lab-definitions div, .lab-definitions div:nth-child(3n) { border-right: 0; border-bottom: 1px solid var(--line); }
  .lab-definitions div:last-child { border-bottom: 0; }
  .lab-detail-header { display: grid; }
}
"""
