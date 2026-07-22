# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json

from .atlas import AtlasNode, RepositoryAtlas
from .generators.atlas import impact_summary


@dataclass(frozen=True)
class AtlasPage:
    content: str
    search_records: tuple[dict[str, str], ...]


def _tags(values: tuple[str, ...]) -> str:
    return "".join(f"<span>{escape(value)}</span>" for value in values)


def _links(atlas: RepositoryAtlas, values: tuple[str, ...]) -> str:
    if not values:
        return '<span class="atlas-none">None</span>'
    return "".join(
        f'<a href="#atlas-{escape(value)}">{escape(atlas.by_id[value].title)}</a>'
        for value in values
    )


def _file_rows(node: AtlasNode) -> str:
    sources = "".join(
        f"<li><code>{escape(source)}</code></li>" for source in node.canonical_files
    )
    generated = "".join(
        f"<li><code>{escape(target)}</code></li>" for target in node.generated_files
    )
    return f"<tr><td><ul>{sources}</ul></td><td><ul>{generated}</ul></td></tr>"


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
        )
    ).lower()


def _detail(atlas: RepositoryAtlas, node: AtlasNode, search: str) -> str:
    used_by = atlas.used_by[node.id]
    workflow = "".join(f"<li>{escape(item)}</li>" for item in node.safe_change_workflow)
    limitations = "".join(f"<li>{escape(item)}</li>" for item in node.limitations)
    docs = "".join(f"<li><code>{escape(item)}</code></li>" for item in node.related_docs)
    impact = "".join(f"<li>{escape(item)}</li>" for item in impact_summary(atlas, node))
    return f"""<details class="atlas-detail" id="atlas-{escape(node.id)}" data-atlas-detail data-category="{escape(node.category)}" data-status="{escape(node.status)}" data-search="{escape(search)}">
  <summary><span><strong>{escape(node.title)}</strong><small><code>{escape(node.path)}</code></small></span><span class="atlas-status atlas-status--{escape(node.status)}">{escape(node.status)}</span></summary>
  <div class="atlas-detail-body">
    <p>{escape(node.purpose)}</p>
    <div class="atlas-relations"><section><h3>Depends on</h3><div>{_links(atlas, node.depends_on)}</div></section><section><h3>Used by</h3><div>{_links(atlas, used_by)}</div></section></div>
    <div class="atlas-boundaries"><section><h3>Owns</h3><div class="atlas-tags">{_tags(node.owns)}</div></section><section><h3>Must never own</h3><div class="atlas-tags atlas-tags--forbidden">{_tags(node.must_not_own)}</div></section></div>
    <div class="atlas-io"><section><h3>Inputs</h3><ul>{''.join(f'<li>{escape(item)}</li>' for item in node.inputs)}</ul></section><section><h3>Outputs</h3><ul>{''.join(f'<li>{escape(item)}</li>' for item in node.outputs)}</ul></section><section><h3>Public contracts</h3><ul>{''.join(f'<li>{escape(item)}</li>' for item in node.public_contracts)}</ul></section></div>
    <h3>Canonical sources to generated projections</h3>
    <div class="atlas-table-wrap"><table><thead><tr><th>Canonical authority</th><th>Generated projection</th></tr></thead><tbody>{_file_rows(node)}</tbody></table></div>
    <div class="atlas-io"><section><h3>Change impact</h3><ul>{impact}</ul></section><section><h3>Verification</h3><div class="atlas-tags">{_tags(node.verification)}</div></section><section><h3>Limitations</h3><ul>{limitations}</ul></section></div>
    <h3>Safe change workflow</h3><ol>{workflow}</ol>
    <details class="atlas-related"><summary>Related documentation</summary><ul>{docs}</ul></details>
  </div>
</details>"""


def _category_map(atlas: RepositoryAtlas) -> str:
    groups = []
    for category in sorted({node.category for node in atlas.nodes}):
        links = "".join(
            f'<a href="#atlas-{escape(node.id)}">{escape(node.title)}</a>'
            for node in atlas.nodes
            if node.category == category
        )
        groups.append(
            f'<section><h3>{escape(category.title())}</h3><div>{links}</div></section>'
        )
    return "".join(groups)


def _dependency_rows(atlas: RepositoryAtlas) -> str:
    rows = []
    for node in atlas.nodes:
        for dependency in node.depends_on:
            rows.append(
                f"<tr><td><a href=\"#atlas-{escape(dependency)}\">{escape(atlas.by_id[dependency].title)}</a></td>"
                f"<td aria-label=\"is used by\">&rarr;</td><td><a href=\"#atlas-{escape(node.id)}\">{escape(node.title)}</a></td></tr>"
            )
    return "".join(rows)


def render_repository_atlas(atlas: RepositoryAtlas) -> AtlasPage:
    categories = sorted({node.category for node in atlas.nodes})
    generated = sum(len(node.generated_files) for node in atlas.nodes)
    verification = len({item for node in atlas.nodes for item in node.verification})
    records = [
        {
            "id": node.id,
            "title": node.title,
            "path": node.path,
            "category": node.category,
            "status": node.status,
            "summary": node.purpose,
            "search": _search(node),
        }
        for node in atlas.nodes
    ]
    payload = json.dumps(records, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    rows = "".join(
        f'<tr data-atlas-row data-category="{escape(node.category)}" data-status="{escape(node.status)}" data-search="{escape(records[index]["search"])}"><td><a href="#atlas-{escape(node.id)}"><strong>{escape(node.title)}</strong><small><code>{escape(node.path)}</code></small></a></td><td>{escape(node.category)}</td><td><span class="atlas-status atlas-status--{escape(node.status)}">{escape(node.status)}</span></td><td>{len(node.depends_on)}</td><td>{len(atlas.used_by[node.id])}</td><td>{escape(', '.join(node.verification))}</td></tr>'
        for index, node in enumerate(atlas.nodes)
    )
    details = "".join(
        _detail(atlas, node, records[index]["search"])
        for index, node in enumerate(atlas.nodes)
    )
    options = "".join(
        f'<option value="{escape(category)}">{escape(category.title())}</option>'
        for category in categories
    )
    content = f"""<article class="repository-atlas" data-repository-atlas>
  <p class="breadcrumb">Architecture / Repository Atlas</p>
  <header class="atlas-header"><div><h1>Repository Atlas</h1><p class="lede">A generated ownership and dependency map for changing HyperFlux Next without creating a second source of truth.</p></div><span class="atlas-lock">Public pre-release</span></header>
  <div class="notice"><strong>One graph, many views.</strong> This page, folder READMEs, dependency diagrams, source lineage, and change-impact guidance all come from <code>architecture/repository-atlas.json</code>.</div>
  <section class="atlas-metrics" aria-label="Repository Atlas summary"><div><strong>{len(atlas.nodes)}</strong><span>subsystems</span></div><div><strong>{len(categories)}</strong><span>ownership categories</span></div><div><strong>{generated}</strong><span>generated projections</span></div><div><strong>{verification}</strong><span>verification nodes referenced</span></div></section>
  <section class="atlas-toolbar" aria-label="Filter repository subsystems">
    <label><span>Search</span><input id="atlas-filter" type="search" placeholder="Subsystem, path, owner, or contract" autocomplete="off"></label>
    <label><span>Category</span><select id="atlas-category"><option value="all">All categories</option>{options}</select></label>
    <label><span>Status</span><select id="atlas-status"><option value="all">All states</option><option value="implemented">Implemented</option><option value="generated">Generated</option><option value="policy">Policy</option><option value="research-boundary">Research boundary</option></select></label>
    <button type="button" id="atlas-command" title="Open subsystem palette (Ctrl+K)">Jump to subsystem</button>
  </section>
  <p id="atlas-filter-status" class="atlas-filter-status" role="status" aria-live="polite">Showing all {len(atlas.nodes)} subsystems.</p>
  <section aria-labelledby="atlas-index-heading"><h2 id="atlas-index-heading">Subsystem index</h2><div class="atlas-table-wrap"><table><thead><tr><th>Subsystem</th><th>Category</th><th>Status</th><th>Depends on</th><th>Used by</th><th>Verification</th></tr></thead><tbody>{rows}</tbody></table></div><p id="atlas-empty" class="atlas-empty" hidden>No subsystems match the current filters.</p></section>
  <section aria-labelledby="atlas-map-heading"><h2 id="atlas-map-heading">Architecture and dependency map</h2><p>Subsystems are grouped by authority. The edge ledger below is generated from the same dependency list; arrows run from dependency to direct consumer.</p><div class="atlas-category-map">{_category_map(atlas)}</div><details class="atlas-edge-ledger"><summary>Show all {sum(len(node.depends_on) for node in atlas.nodes)} dependency edges</summary><div class="atlas-table-wrap"><table><thead><tr><th>Dependency</th><th></th><th>Direct consumer</th></tr></thead><tbody>{_dependency_rows(atlas)}</tbody></table></div></details></section>
  <section class="atlas-details" aria-labelledby="atlas-details-heading"><h2 id="atlas-details-heading">Subsystem contracts</h2>{details}</section>
  <dialog id="atlas-palette" class="atlas-palette" aria-labelledby="atlas-palette-title"><form method="dialog"><header><h2 id="atlas-palette-title">Jump to subsystem</h2><button value="cancel" aria-label="Close command palette" title="Close">&times;</button></header><label><span class="sr-only">Filter subsystems</span><input id="atlas-palette-filter" type="search" placeholder="Type a subsystem or path" autocomplete="off"></label><div id="atlas-palette-results" class="atlas-palette-results"></div></form></dialog>
  <script id="atlas-data" type="application/json">{payload}</script>
</article>"""
    search_records = tuple(
        {
            "title": node.title,
            "audience": "Repository Atlas",
            "summary": f"{node.category} | {node.path} | {node.status}",
            "url": f"atlas/index.html#atlas-{node.id}",
            "search": records[index]["search"],
        }
        for index, node in enumerate(atlas.nodes)
    )
    return AtlasPage(content=content, search_records=search_records)


ATLAS_SCRIPT = """(() => {
  const root = document.querySelector('[data-repository-atlas]');
  const source = document.getElementById('atlas-data');
  if (!root || !source) return;
  const records = JSON.parse(source.textContent || '[]');
  const query = document.getElementById('atlas-filter');
  const category = document.getElementById('atlas-category');
  const status = document.getElementById('atlas-status');
  const rows = [...root.querySelectorAll('[data-atlas-row]')];
  const details = [...root.querySelectorAll('[data-atlas-detail]')];
  const message = document.getElementById('atlas-filter-status');
  const empty = document.getElementById('atlas-empty');
  const apply = () => {
    const needle = query.value.trim().toLocaleLowerCase();
    let visible = 0;
    [...rows, ...details].forEach((item) => {
      const match = (!needle || item.dataset.search.includes(needle)) &&
        (category.value === 'all' || item.dataset.category === category.value) &&
        (status.value === 'all' || item.dataset.status === status.value);
      item.hidden = !match;
      if (match && item.matches('[data-atlas-row]')) visible += 1;
    });
    message.textContent = `Showing ${visible} of ${rows.length} subsystems.`;
    empty.hidden = visible !== 0;
  };
  [query, category, status].forEach((control) => control.addEventListener('input', apply));

  const dialog = document.getElementById('atlas-palette');
  const open = document.getElementById('atlas-command');
  const paletteFilter = document.getElementById('atlas-palette-filter');
  const results = document.getElementById('atlas-palette-results');
  const renderPalette = () => {
    const needle = paletteFilter.value.trim().toLocaleLowerCase();
    const matches = records.filter((record) => !needle || record.search.includes(needle)).slice(0, 10);
    results.replaceChildren(...matches.map((record) => {
      const button = document.createElement('button');
      button.type = 'button';
      const title = document.createElement('strong');
      title.textContent = record.title;
      const path = document.createElement('small');
      path.textContent = `${record.path} · ${record.category}`;
      button.append(title, path);
      button.addEventListener('click', () => {
        dialog.close();
        const target = document.getElementById(`atlas-${record.id}`);
        target.hidden = false;
        target.open = true;
        target.scrollIntoView({block: 'start'});
        target.querySelector('summary').focus();
      });
      return button;
    }));
  };
  const openPalette = () => { dialog.showModal(); paletteFilter.value = ''; renderPalette(); paletteFilter.focus(); };
  open.addEventListener('click', openPalette);
  paletteFilter.addEventListener('input', renderPalette);
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === 'k') {
      event.preventDefault();
      if (!dialog.open) openPalette();
    }
  });
})();
"""


ATLAS_CSS = """
.repository-atlas { min-width: 0; }
.atlas-header { display: flex; align-items: start; justify-content: space-between; gap: 24px; }
.atlas-header h1 { margin: 0 0 10px; font-size: 34px; line-height: 1.2; }
.atlas-lock { flex: 0 0 auto; padding: 3px 8px; border: 1px solid var(--yellow); border-radius: 4px; color: var(--yellow); font-size: 12px; font-weight: 700; }
.atlas-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 24px 0; border: 1px solid var(--line); }
.atlas-metrics div { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line); }
.atlas-metrics div:last-child { border-right: 0; }
.atlas-metrics strong, .atlas-metrics span { display: block; }
.atlas-metrics strong { color: var(--lime); font-size: 24px; }
.atlas-metrics span { color: var(--muted); }
.atlas-toolbar { display: grid; grid-template-columns: minmax(240px, 2fr) repeat(2, minmax(150px, 1fr)) auto; gap: 10px; align-items: end; padding: 14px; border: 1px solid var(--line); background: var(--surface); }
.atlas-toolbar label span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.atlas-toolbar input, .atlas-toolbar select, .atlas-toolbar button, .atlas-palette input, .atlas-palette button { min-height: 40px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--bg); font: inherit; }
.atlas-toolbar button, .atlas-palette button { cursor: pointer; }
.atlas-filter-status, .atlas-none { color: var(--muted); }
.atlas-table-wrap { width: 100%; overflow: auto; border: 1px solid var(--line); }
.atlas-table-wrap table { width: 100%; border-collapse: collapse; font-size: 13px; }
.atlas-table-wrap th, .atlas-table-wrap td { padding: 8px 10px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.atlas-table-wrap tr:last-child > * { border-bottom: 0; }
.atlas-table-wrap tr > *:last-child { border-right: 0; }
.atlas-table-wrap thead th { background: var(--surface-strong); }
.atlas-table-wrap td a strong, .atlas-table-wrap td a small { display: block; }
.atlas-table-wrap code, .atlas-detail code { color: var(--lime); overflow-wrap: anywhere; }
.atlas-status { display: inline-flex; padding: 1px 6px; border: 1px solid var(--line); border-radius: 4px; font-size: 12px; white-space: nowrap; }
.atlas-status--implemented { border-color: var(--teal); color: var(--teal); }
.atlas-status--generated { border-color: var(--cyan); color: var(--cyan); }
.atlas-status--policy { border-color: var(--yellow); color: var(--yellow); }
.atlas-status--research-boundary { border-color: var(--coral); color: var(--coral); }
.atlas-empty { padding: 14px; border: 1px dashed var(--line); color: var(--muted); }
.atlas-category-map { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; border: 1px solid var(--line); background: var(--line); }
.atlas-category-map section { min-width: 0; padding: 12px; background: var(--surface); }
.atlas-category-map h3 { margin: 0 0 8px; font-size: 14px; }
.atlas-category-map section div { display: flex; flex-wrap: wrap; gap: 6px; }
.atlas-category-map a, .atlas-relations a { padding: 2px 6px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); text-decoration: none; }
.atlas-edge-ledger { margin-top: 12px; border: 1px solid var(--line); }
.atlas-edge-ledger > summary, .atlas-related > summary { min-height: 42px; padding: 8px 11px; cursor: pointer; font-weight: 700; }
.atlas-details { margin-top: 36px; }
.atlas-detail { margin-top: 8px; border: 1px solid var(--line); background: var(--surface); }
.atlas-detail > summary { display: flex; min-height: 52px; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 12px; cursor: pointer; }
.atlas-detail > summary strong, .atlas-detail > summary small { display: block; }
.atlas-detail-body { padding: 4px 14px 18px; border-top: 1px solid var(--line); }
.atlas-relations, .atlas-boundaries, .atlas-io { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.atlas-io { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.atlas-relations section, .atlas-boundaries section, .atlas-io section { min-width: 0; padding: 12px; border: 1px solid var(--line); background: var(--bg); }
.atlas-relations h3, .atlas-boundaries h3, .atlas-io h3 { margin: 0 0 8px; font-size: 15px; }
.atlas-relations section div, .atlas-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.atlas-tags span { padding: 2px 6px; border: 1px solid var(--teal); border-radius: 4px; }
.atlas-tags--forbidden span { border-color: var(--coral); }
.atlas-detail-body li { margin: 4px 0; }
.atlas-related { margin-top: 16px; border: 1px solid var(--line); }
.atlas-palette { width: min(620px, calc(100% - 32px)); padding: 0; border: 1px solid var(--line); border-radius: 6px; color: var(--ink); background: var(--surface); }
.atlas-palette::backdrop { background: rgb(0 0 0 / 65%); }
.atlas-palette form { padding: 14px; }
.atlas-palette header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.atlas-palette header h2 { margin: 0; font-size: 19px; }
.atlas-palette header button { width: 40px; padding: 0; font-size: 22px; }
.atlas-palette input { width: 100%; margin: 12px 0; }
.atlas-palette-results { display: grid; gap: 4px; max-height: 420px; overflow: auto; }
.atlas-palette-results button { height: auto; text-align: left; }
.atlas-palette-results strong, .atlas-palette-results small { display: block; }
.atlas-palette-results small { color: var(--muted); }
@media (max-width: 900px) {
  .atlas-metrics, .atlas-category-map, .atlas-relations, .atlas-boundaries { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .atlas-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .atlas-io { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .atlas-header { display: grid; }
  .atlas-metrics, .atlas-category-map, .atlas-toolbar, .atlas-relations, .atlas-boundaries { grid-template-columns: 1fr; }
  .atlas-metrics div { border-right: 0; border-bottom: 1px solid var(--line); }
  .atlas-metrics div:last-child { border-bottom: 0; }
  .atlas-detail > summary { align-items: start; flex-direction: column; }
}
"""
