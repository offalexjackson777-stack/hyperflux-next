# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
from typing import Any

from .atlas import AtlasNode, RepositoryAtlas
from .generators.atlas import impact_summary


@dataclass(frozen=True)
class AtlasPage:
    content: str
    search_records: tuple[dict[str, str], ...]


def _label(value: str) -> str:
    return value.replace("-", " ").title()


def _status(node: AtlasNode) -> str:
    labels = {
        "implemented": "Implemented",
        "generated": "Generated",
        "policy": "Policy boundary",
        "research-boundary": "Research boundary",
    }
    return labels[node.status]


def _search(node: AtlasNode) -> str:
    return " ".join(
        (
            node.id,
            node.title,
            node.path,
            node.category,
            node.status,
            node.purpose,
            *node.owns,
            *node.must_not_own,
            *node.inputs,
            *node.outputs,
            *node.public_contracts,
            *node.canonical_files,
            *node.generated_files,
            *node.verification,
        )
    ).casefold()


def _record(atlas: RepositoryAtlas, node: AtlasNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "title": node.title,
        "path": node.path,
        "category": node.category,
        "status": node.status,
        "status_label": _status(node),
        "purpose": node.purpose,
        "owns": node.owns,
        "must_not_own": node.must_not_own,
        "inputs": node.inputs,
        "outputs": node.outputs,
        "public_contracts": node.public_contracts,
        "canonical_files": node.canonical_files,
        "generated_files": node.generated_files,
        "verification": node.verification,
        "limitations": node.limitations,
        "safe_change_workflow": node.safe_change_workflow,
        "related_docs": node.related_docs,
        "depends_on": node.depends_on,
        "used_by": atlas.used_by[node.id],
        "impact": impact_summary(atlas, node),
        "search": _search(node),
    }


def _tags(values: tuple[str, ...], *, forbidden: bool = False) -> str:
    modifier = " atlas-tags--forbidden" if forbidden else ""
    return (
        f'<div class="atlas-tags{modifier}">'
        + "".join(f"<span>{escape(value)}</span>" for value in values)
        + "</div>"
    )


def _list(values: tuple[str, ...], *, ordered: bool = False) -> str:
    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(f"<li>{escape(value)}</li>" for value in values) + f"</{tag}>"


def _relations(atlas: RepositoryAtlas, values: tuple[str, ...]) -> str:
    if not values:
        return '<p class="atlas-none">No direct relationships.</p>'
    return '<div class="atlas-relation-list">' + "".join(
        f'<button type="button" data-atlas-select="{escape(identifier)}">'
        f'<strong>{escape(atlas.by_id[identifier].title)}</strong>'
        f'<small>{escape(atlas.by_id[identifier].path)}</small></button>'
        for identifier in values
    ) + "</div>"


def _lineage(node: AtlasNode) -> str:
    canonical = "".join(f"<li><code>{escape(item)}</code></li>" for item in node.canonical_files)
    generated = "".join(f"<li><code>{escape(item)}</code></li>" for item in node.generated_files)
    if not generated:
        generated = '<li class="atlas-none">No generated projection.</li>'
    return f"""<div class="atlas-lineage">
  <section><h3>Canonical authority</h3><ul>{canonical}</ul></section>
  <span aria-hidden="true">&rarr;</span>
  <section><h3>Generated projections</h3><ul>{generated}</ul></section>
</div>"""


def _detail_html(atlas: RepositoryAtlas, node: AtlasNode) -> str:
    return f"""<article class="atlas-selected" id="atlas-{escape(node.id)}" data-atlas-selected-record="{escape(node.id)}">
  <header class="atlas-detail-header"><div><p class="page-kicker">{escape(_label(node.category))} / {escape(_status(node))}</p><h2>{escape(node.title)}</h2><code>{escape(node.path)}</code></div><span class="atlas-status atlas-status--{escape(node.status)}">{escape(_status(node))}</span></header>
  <p class="atlas-purpose">{escape(node.purpose)}</p>
  <dl class="atlas-definitions"><div><dt>Direct dependencies</dt><dd>{len(node.depends_on)}</dd></div><div><dt>Direct consumers</dt><dd>{len(atlas.used_by[node.id])}</dd></div><div><dt>Canonical sources</dt><dd>{len(node.canonical_files)}</dd></div><div><dt>Generated outputs</dt><dd>{len(node.generated_files)}</dd></div></dl>
  <section class="atlas-section"><div class="atlas-section-heading"><p>01</p><div><h3>Responsibility boundary</h3><p>What belongs here, and what must stay elsewhere.</p></div></div><div class="atlas-two"><section><h4>Owns</h4>{_tags(node.owns)}</section><section><h4>Must never own</h4>{_tags(node.must_not_own, forbidden=True)}</section></div></section>
  <section class="atlas-section"><div class="atlas-section-heading"><p>02</p><div><h3>Dependency direction</h3><p>Follow incoming and outgoing architecture relationships.</p></div></div><div class="atlas-two"><section><h4>Depends on</h4>{_relations(atlas, node.depends_on)}</section><section><h4>Used by</h4>{_relations(atlas, atlas.used_by[node.id])}</section></div></section>
  <section class="atlas-section"><div class="atlas-section-heading"><p>03</p><div><h3>Contracts and data flow</h3><p>The boundary this subsystem accepts and exposes.</p></div></div><div class="atlas-three"><section><h4>Inputs</h4>{_list(node.inputs)}</section><section><h4>Outputs</h4>{_list(node.outputs)}</section><section><h4>Public contracts</h4>{_list(node.public_contracts)}</section></div></section>
  <section class="atlas-section"><div class="atlas-section-heading"><p>04</p><div><h3>Source lineage</h3><p>Change canonical inputs; regenerate projections.</p></div></div>{_lineage(node)}</section>
  <section class="atlas-section"><div class="atlas-section-heading"><p>05</p><div><h3>Change safely</h3><p>Expected impact, verification, and known limits.</p></div></div><div class="atlas-three"><section><h4>Likely impact</h4>{_list(impact_summary(atlas, node))}</section><section><h4>Verification</h4>{_tags(node.verification)}</section><section><h4>Known limitations</h4>{_list(node.limitations)}</section></div><h4>Recommended workflow</h4>{_list(node.safe_change_workflow, ordered=True)}</section>
  <details class="atlas-related"><summary>Related documentation</summary>{_list(node.related_docs)}</details>
</article>"""


def _node_row(atlas: RepositoryAtlas, node: AtlasNode) -> str:
    return f"""<button type="button" class="atlas-node" data-atlas-node data-atlas-select="{escape(node.id)}" data-category="{escape(node.category)}" data-status="{escape(node.status)}" data-search="{escape(_search(node), quote=True)}">
  <span><strong>{escape(node.title)}</strong><small><code>{escape(node.path)}</code></small></span>
  <span><small>{escape(_label(node.category))}</small><small>{len(node.depends_on)} in / {len(atlas.used_by[node.id])} out</small></span>
</button>"""


def render_repository_atlas(atlas: RepositoryAtlas) -> AtlasPage:
    categories = sorted({node.category for node in atlas.nodes})
    generated = sum(len(node.generated_files) for node in atlas.nodes)
    verification = len({item for node in atlas.nodes for item in node.verification})
    edges = sum(len(node.depends_on) for node in atlas.nodes)
    records = [_record(atlas, node) for node in atlas.nodes]
    payload = json.dumps(records, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    options = "".join(
        f'<option value="{escape(category)}">{escape(_label(category))}</option>'
        for category in categories
    )
    rows = "".join(_node_row(atlas, node) for node in atlas.nodes)
    initial = atlas.nodes[0]
    content = f"""<article class="repository-atlas" data-repository-atlas>
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="../index.html">Home</a><span>Repository Atlas</span></nav>
  <header class="page-hero page-hero--reference"><p class="page-kicker">Generated architecture map</p><h1>Repository Atlas</h1><p class="lede">Find the owner of a change, understand its dependencies, and follow the repository's canonical source-to-projection path.</p></header>
  <div class="notice"><strong>One architecture record.</strong> This browser and the generated folder guides come from <code>architecture/repository-atlas.json</code>. Change the canonical record, then regenerate its views.</div>
  <section class="atlas-metrics" aria-label="Repository Atlas summary"><div><strong>{len(atlas.nodes)}</strong><span>subsystems</span></div><div><strong>{len(categories)}</strong><span>responsibility areas</span></div><div><strong>{edges}</strong><span>dependency edges</span></div><div><strong>{verification}</strong><span>verification nodes</span></div><div><strong>{generated}</strong><span>generated projections</span></div></section>
  <section class="atlas-toolbar" aria-label="Find a repository subsystem"><label><span>Find a subsystem</span><input id="atlas-filter" type="search" placeholder="Name, path, contract, or responsibility" autocomplete="off"></label><label><span>Responsibility area</span><select id="atlas-category"><option value="all">All areas</option>{options}</select></label><label><span>Boundary type</span><select id="atlas-status"><option value="all">All types</option><option value="implemented">Implemented</option><option value="generated">Generated</option><option value="policy">Policy boundary</option><option value="research-boundary">Research boundary</option></select></label><button type="button" id="atlas-clear">Clear</button></section>
  <p id="atlas-filter-status" class="atlas-filter-status" role="status" aria-live="polite">Showing all {len(atlas.nodes)} subsystems.</p>
  <div class="atlas-browser"><aside class="atlas-node-list" aria-label="Repository subsystems">{rows}<p id="atlas-empty" class="atlas-empty" hidden>No subsystem matches these filters.</p></aside><section class="atlas-detail" id="atlas-detail" tabindex="-1">{_detail_html(atlas, initial)}</section></div>
  <details class="atlas-technical"><summary>How to read this map</summary><div class="atlas-three"><section><h3>Dependency</h3><p>A canonical input this subsystem consumes.</p></section><section><h3>Consumer</h3><p>A subsystem directly affected by this subsystem's public output.</p></section><section><h3>Projection</h3><p>Generated output that must not become an independent source of truth.</p></section></div><p>The graph currently contains {edges} directed edges. Select a relationship inside any subsystem to follow it without losing the current filters.</p></details>
  <script id="atlas-data" type="application/json">{payload}</script>
</article>"""
    search_records = tuple(
        {
            "title": node.title,
            "audience": "Repository Atlas",
            "summary": f"{_label(node.category)} | {node.path} | {_status(node)}",
            "url": f"atlas/index.html#atlas-{node.id}",
            "search": _search(node),
        }
        for node in atlas.nodes
    )
    return AtlasPage(content=content, search_records=search_records)


ATLAS_SCRIPT = r"""(() => {
  const root = document.querySelector('[data-repository-atlas]');
  const source = document.getElementById('atlas-data');
  if (!root || !source) return;
  const records = JSON.parse(source.textContent || '[]');
  const byId = new Map(records.map((record) => [record.id, record]));
  const query = document.getElementById('atlas-filter');
  const category = document.getElementById('atlas-category');
  const status = document.getElementById('atlas-status');
  const clear = document.getElementById('atlas-clear');
  const nodes = [...root.querySelectorAll('[data-atlas-node]')];
  const message = document.getElementById('atlas-filter-status');
  const empty = document.getElementById('atlas-empty');
  const detail = document.getElementById('atlas-detail');
  const safe = (value) => String(value).replace(/[&<>"']/g, (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  const label = (value) => String(value).replaceAll('-', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  const list = (values, ordered = false) => {
    const tag = ordered ? 'ol' : 'ul';
    return `<${tag}>${values.map((value) => `<li>${safe(value)}</li>`).join('')}</${tag}>`;
  };
  const tags = (values, forbidden = false) => `<div class="atlas-tags${forbidden ? ' atlas-tags--forbidden' : ''}">${values.map((value) => `<span>${safe(value)}</span>`).join('')}</div>`;
  const relations = (values) => values.length ? `<div class="atlas-relation-list">${values.map((identifier) => {
    const related = byId.get(identifier);
    return `<button type="button" data-atlas-select="${safe(identifier)}"><strong>${safe(related.title)}</strong><small>${safe(related.path)}</small></button>`;
  }).join('')}</div>` : '<p class="atlas-none">No direct relationships.</p>';
  const codeList = (values) => `<ul>${values.map((value) => `<li><code>${safe(value)}</code></li>`).join('')}</ul>`;
  const render = (record, focus = false) => {
    const statusLabel = record.status_label;
    detail.innerHTML = `<article class="atlas-selected" id="atlas-${safe(record.id)}" data-atlas-selected-record="${safe(record.id)}">
      <header class="atlas-detail-header"><div><p class="page-kicker">${safe(label(record.category))} / ${safe(statusLabel)}</p><h2>${safe(record.title)}</h2><code>${safe(record.path)}</code></div><span class="atlas-status atlas-status--${safe(record.status)}">${safe(statusLabel)}</span></header>
      <p class="atlas-purpose">${safe(record.purpose)}</p>
      <dl class="atlas-definitions"><div><dt>Direct dependencies</dt><dd>${record.depends_on.length}</dd></div><div><dt>Direct consumers</dt><dd>${record.used_by.length}</dd></div><div><dt>Canonical sources</dt><dd>${record.canonical_files.length}</dd></div><div><dt>Generated outputs</dt><dd>${record.generated_files.length}</dd></div></dl>
      <section class="atlas-section"><div class="atlas-section-heading"><p>01</p><div><h3>Responsibility boundary</h3><p>What belongs here, and what must stay elsewhere.</p></div></div><div class="atlas-two"><section><h4>Owns</h4>${tags(record.owns)}</section><section><h4>Must never own</h4>${tags(record.must_not_own, true)}</section></div></section>
      <section class="atlas-section"><div class="atlas-section-heading"><p>02</p><div><h3>Dependency direction</h3><p>Follow incoming and outgoing architecture relationships.</p></div></div><div class="atlas-two"><section><h4>Depends on</h4>${relations(record.depends_on)}</section><section><h4>Used by</h4>${relations(record.used_by)}</section></div></section>
      <section class="atlas-section"><div class="atlas-section-heading"><p>03</p><div><h3>Contracts and data flow</h3><p>The boundary this subsystem accepts and exposes.</p></div></div><div class="atlas-three"><section><h4>Inputs</h4>${list(record.inputs)}</section><section><h4>Outputs</h4>${list(record.outputs)}</section><section><h4>Public contracts</h4>${list(record.public_contracts)}</section></div></section>
      <section class="atlas-section"><div class="atlas-section-heading"><p>04</p><div><h3>Source lineage</h3><p>Change canonical inputs; regenerate projections.</p></div></div><div class="atlas-lineage"><section><h3>Canonical authority</h3>${codeList(record.canonical_files)}</section><span aria-hidden="true">&rarr;</span><section><h3>Generated projections</h3>${record.generated_files.length ? codeList(record.generated_files) : '<p class="atlas-none">No generated projection.</p>'}</section></div></section>
      <section class="atlas-section"><div class="atlas-section-heading"><p>05</p><div><h3>Change safely</h3><p>Expected impact, verification, and known limits.</p></div></div><div class="atlas-three"><section><h4>Likely impact</h4>${list(record.impact)}</section><section><h4>Verification</h4>${tags(record.verification)}</section><section><h4>Known limitations</h4>${list(record.limitations)}</section></div><h4>Recommended workflow</h4>${list(record.safe_change_workflow, true)}</section>
      <details class="atlas-related"><summary>Related documentation</summary>${codeList(record.related_docs)}</details>
    </article>`;
    nodes.forEach((node) => node.setAttribute('aria-current', node.dataset.atlasSelect === record.id ? 'true' : 'false'));
    history.replaceState(null, '', `#atlas-${record.id}`);
    if (focus) detail.focus({preventScroll: true});
  };
  const select = (identifier, focus = false) => {
    const record = byId.get(identifier);
    if (record) render(record, focus);
  };
  const apply = () => {
    const needle = query.value.trim().toLocaleLowerCase();
    const visible = nodes.filter((node) => {
      const match = (!needle || node.dataset.search.includes(needle)) &&
        (category.value === 'all' || node.dataset.category === category.value) &&
        (status.value === 'all' || node.dataset.status === status.value);
      node.hidden = !match;
      return match;
    });
    message.textContent = visible.length === nodes.length ? `Showing all ${nodes.length} subsystems.` : `Showing ${visible.length} of ${nodes.length} subsystems.`;
    empty.hidden = visible.length !== 0;
    const selected = detail.querySelector('[data-atlas-selected-record]')?.dataset.atlasSelectedRecord;
    const preferred = HyperFluxPortal.preferredVisible({items: visible, selectedId: selected, needle, id: (node) => node.dataset.atlasSelect, title: (node) => byId.get(node.dataset.atlasSelect).title});
    if (preferred) select(preferred.dataset.atlasSelect);
    detail.hidden = visible.length === 0;
  };
  root.addEventListener('click', (event) => {
    const target = event.target.closest('[data-atlas-select]');
    if (!target) return;
    select(target.dataset.atlasSelect, target.closest('.atlas-detail') !== null);
  });
  [query, category, status].forEach((control) => control.addEventListener('input', apply));
  clear.addEventListener('click', () => { query.value = ''; category.value = 'all'; status.value = 'all'; apply(); query.focus(); });
  const fromHash = () => select(location.hash.startsWith('#atlas-') ? location.hash.slice(7) : records[0]?.id);
  addEventListener('hashchange', fromHash);
  fromHash();
})();
"""


ATLAS_CSS = """
.repository-atlas { min-width: 0; }
.atlas-metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin: 26px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.atlas-metrics div { min-width: 0; padding: 15px 16px; border-right: 1px solid var(--line); }
.atlas-metrics div:last-child { border-right: 0; }
.atlas-metrics strong, .atlas-metrics span { display: block; }
.atlas-metrics strong { color: var(--lime); font: 700 25px/1 var(--display-font); }
.atlas-metrics span { margin-top: 6px; color: var(--muted); font-size: 12px; }
.atlas-toolbar { display: grid; grid-template-columns: minmax(250px, 2fr) repeat(2, minmax(150px, 1fr)) auto; gap: 10px; align-items: end; padding-bottom: 16px; border-bottom: 1px solid var(--line); }
.atlas-toolbar label span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.atlas-toolbar input, .atlas-toolbar select, .atlas-toolbar button { width: 100%; min-height: 40px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); font: inherit; }
.atlas-toolbar button { cursor: pointer; }
.atlas-filter-status, .atlas-none { color: var(--muted); }
.atlas-browser { display: grid; grid-template-columns: minmax(285px, 355px) minmax(0, 1fr); min-height: 760px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.atlas-node-list { min-width: 0; max-height: calc(100vh - 170px); overflow-y: auto; padding-right: 14px; border-right: 1px solid var(--line); scrollbar-width: thin; }
.atlas-node { display: grid; width: 100%; min-height: 70px; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 10px; padding: 10px 8px; border: 0; border-bottom: 1px solid var(--line-soft); border-left: 2px solid transparent; color: var(--ink); background: transparent; text-align: left; cursor: pointer; }
.atlas-node > span { min-width: 0; }
.atlas-node > span:last-child { text-align: right; }
.atlas-node strong, .atlas-node small { display: block; overflow-wrap: anywhere; }
.atlas-node small { margin-top: 3px; color: var(--muted); font-size: 11px; }
.atlas-node:hover, .atlas-node[aria-current="true"] { border-left-color: var(--teal); background: var(--surface); }
.atlas-detail { min-width: 0; padding: 28px 0 55px 34px; }
.atlas-detail-header { display: flex; align-items: start; justify-content: space-between; gap: 16px; }
.atlas-detail-header h2 { margin: 4px 0 8px; font: 700 28px/1.2 var(--display-font); }
.atlas-detail-header code { color: var(--lime); overflow-wrap: anywhere; }
.atlas-status { display: inline-flex; flex: 0 0 auto; padding: 3px 7px; border: 1px solid var(--line); border-radius: 4px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.atlas-status--implemented { border-color: var(--teal); color: var(--teal); }
.atlas-status--generated { border-color: var(--cyan); color: var(--cyan); }
.atlas-status--policy { border-color: var(--yellow); color: var(--yellow); }
.atlas-status--research-boundary { border-color: var(--coral); color: var(--coral); }
.atlas-purpose { max-width: 850px; color: var(--muted); font-size: 16px; }
.atlas-definitions { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 22px 0 34px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.atlas-definitions div { min-width: 0; padding: 11px 12px; border-right: 1px solid var(--line); }
.atlas-definitions div:last-child { border-right: 0; }
.atlas-definitions dt { color: var(--muted); font-size: 10px; text-transform: uppercase; }
.atlas-definitions dd { margin: 4px 0 0; font: 700 18px/1 var(--display-font); }
.atlas-section { margin-top: 42px; padding-top: 22px; border-top: 1px solid var(--line); }
.atlas-section-heading { display: flex; gap: 12px; align-items: start; margin-bottom: 14px; }
.atlas-section-heading > p { margin: 0; color: var(--cyan); font: 700 12px/1.5 var(--display-font); }
.atlas-section-heading h3 { margin: 0; font: 700 18px/1.2 var(--display-font); }
.atlas-section-heading div p { margin: 4px 0 0; color: var(--muted); }
.atlas-section h4 { margin: 18px 0 8px; font-size: 13px; }
.atlas-two, .atlas-three { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.atlas-three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.atlas-two > section, .atlas-three > section { min-width: 0; padding: 0 14px 14px; border-left: 1px solid var(--line); }
.atlas-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.atlas-tags span { padding: 2px 6px; border: 1px solid var(--teal); border-radius: 4px; }
.atlas-tags--forbidden span { border-color: var(--coral); }
.atlas-relation-list { display: grid; gap: 5px; }
.atlas-relation-list button { display: block; width: 100%; padding: 8px; border: 1px solid var(--line); border-left: 2px solid var(--cyan); color: var(--ink); background: transparent; text-align: left; cursor: pointer; }
.atlas-relation-list strong, .atlas-relation-list small { display: block; }
.atlas-relation-list small { margin-top: 3px; color: var(--muted); }
.atlas-lineage { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: stretch; gap: 12px; }
.atlas-lineage > section { min-width: 0; padding: 12px 14px; border: 1px solid var(--line); background: var(--surface); }
.atlas-lineage > span { align-self: center; color: var(--yellow); font-size: 22px; }
.atlas-lineage h3 { margin: 0 0 8px; font-size: 13px; }
.atlas-lineage code { color: var(--lime); overflow-wrap: anywhere; }
.atlas-selected li { margin: 5px 0; }
.atlas-related, .atlas-technical { margin-top: 28px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.atlas-related > summary, .atlas-technical > summary { min-height: 44px; padding: 10px 0; cursor: pointer; font-weight: 700; }
.atlas-technical { margin-top: 26px; padding-bottom: 8px; }
.atlas-empty { padding: 14px; border: 1px dashed var(--line); color: var(--muted); }
@media (max-width: 1080px) {
  .atlas-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .atlas-metrics div:nth-child(3) { border-right: 0; }
  .atlas-metrics div:nth-child(-n + 3) { border-bottom: 1px solid var(--line); }
  .atlas-three { grid-template-columns: 1fr; }
}
@media (max-width: 900px) {
  .atlas-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .atlas-browser { grid-template-columns: 1fr; }
  .atlas-node-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 390px; padding-right: 0; border-right: 0; border-bottom: 1px solid var(--line); }
  .atlas-detail { padding-left: 0; }
}
@media (max-width: 600px) {
  .atlas-metrics, .atlas-toolbar, .atlas-node-list, .atlas-definitions, .atlas-two { grid-template-columns: 1fr; }
  .atlas-metrics div, .atlas-metrics div:nth-child(3) { border-right: 0; border-bottom: 1px solid var(--line); }
  .atlas-metrics div:last-child { border-bottom: 0; }
  .atlas-detail-header { display: grid; }
  .atlas-definitions div { border-right: 0; border-bottom: 1px solid var(--line); }
  .atlas-definitions div:last-child { border-bottom: 0; }
  .atlas-lineage { grid-template-columns: 1fr; }
  .atlas-lineage > span { justify-self: center; transform: rotate(90deg); }
}
"""
