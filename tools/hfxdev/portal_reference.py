# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import re
from typing import Callable

from .model import ModelError


HEADING_TWO = re.compile(r"^## (?P<title>.+)$")
HEADING_THREE = re.compile(r"^### (?P<title>.+)$")
NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ReferenceEntry:
    id: str
    group_id: str
    group_title: str
    title: str
    markdown: str


@dataclass(frozen=True)
class ReferenceDocument:
    introduction: str
    entries: tuple[ReferenceEntry, ...]

    @property
    def groups(self) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for entry in self.entries:
            item = (entry.group_id, entry.group_title)
            if item not in result:
                result.append(item)
        return tuple(result)


def _slug(value: str) -> str:
    slug = NON_IDENTIFIER.sub("-", value.casefold()).strip("-")
    if not slug:
        raise ModelError(f"reference heading has no usable identifier: {value}")
    return slug


def parse_reference(source: str) -> ReferenceDocument:
    introduction: list[str] = []
    groups: list[tuple[str, list[str], list[tuple[str, list[str]]]]] = []
    current_group: tuple[str, list[str], list[tuple[str, list[str]]]] | None = None
    current_entry: tuple[str, list[str]] | None = None
    in_fence = False
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        h2 = None if in_fence else HEADING_TWO.fullmatch(stripped)
        if h2 is not None:
            current_group = (h2.group("title"), [], [])
            groups.append(current_group)
            current_entry = None
            continue
        h3 = None if in_fence else HEADING_THREE.fullmatch(stripped)
        if h3 is not None:
            if current_group is None:
                raise ModelError("reference level-three heading appears before a group")
            current_entry = (h3.group("title"), [])
            current_group[2].append(current_entry)
            continue
        if current_entry is not None:
            current_entry[1].append(raw)
        elif current_group is not None:
            current_group[1].append(raw)
        else:
            introduction.append(raw)

    entries: list[ReferenceEntry] = []
    identifiers: set[str] = set()
    for group_title, group_intro, group_entries in groups:
        group_id = _slug(group_title)
        if group_entries and "\n".join(group_intro).strip():
            group_entries.insert(0, ("Overview", group_intro))
        elif not group_entries:
            group_entries.append((group_title, group_intro))
        for title, lines in group_entries:
            base = f"{group_id}-{_slug(title)}"
            identifier = base
            suffix = 2
            while identifier in identifiers:
                identifier = f"{base}-{suffix}"
                suffix += 1
            identifiers.add(identifier)
            entries.append(
                ReferenceEntry(
                    id=identifier,
                    group_id=group_id,
                    group_title=group_title,
                    title=title,
                    markdown="\n".join(lines).strip(),
                )
            )
    if not entries:
        raise ModelError("reference document contains no navigable sections")
    return ReferenceDocument(
        introduction="\n".join(introduction).strip(), entries=tuple(entries)
    )


def render_reference_browser(
    document: ReferenceDocument,
    *,
    title: str,
    summary: str,
    render_markdown: Callable[[str], str],
) -> str:
    rendered = []
    for entry in document.entries:
        rendered.append(
            {
                "id": entry.id,
                "group_id": entry.group_id,
                "group": entry.group_title,
                "title": entry.title,
                "search": f"{entry.group_title} {entry.title} {entry.markdown}".casefold(),
                "html": render_markdown(entry.markdown),
            }
        )
    payload = json.dumps(rendered, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    groups = "".join(
        f'<option value="{escape(identifier)}">{escape(label)}</option>'
        for identifier, label in document.groups
    )
    list_items = "".join(
        '<button type="button" class="reference-item" '
        f'data-reference-entry="{escape(entry["id"])}" '
        f'data-reference-group="{escape(entry["group_id"])}" '
        f'data-search="{escape(entry["search"], quote=True)}">'
        f'<span>{escape(entry["title"])}</span><small>{escape(entry["group"])}</small></button>'
        for entry in rendered
    )
    first = rendered[0]
    intro = render_markdown(document.introduction)
    return f"""<article class="reference-browser" data-reference-browser>
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="architecture.html">Develop</a><span>{escape(title)}</span></nav>
  <header class="page-hero page-hero--reference"><p class="page-kicker">Generated API reference</p><h1>{escape(title)}</h1><p class="lede">{escape(summary)}</p></header>
  <section class="reference-intro">{intro}</section>
  <div class="reference-workbench">
    <aside class="reference-index" aria-label="Reference index"><div class="reference-controls"><label><span>Find a symbol</span><input id="reference-filter" type="search" placeholder="Method, record, field, or type" autocomplete="off"></label><label><span>Group</span><select id="reference-group"><option value="all">All groups</option>{groups}</select></label></div><p id="reference-status" role="status" aria-live="polite">Showing all {len(rendered)} entries.</p><div class="reference-items">{list_items}</div></aside>
    <section class="reference-detail" id="reference-detail" tabindex="-1"><p class="page-kicker" id="reference-detail-group">{escape(first['group'])}</p><h2 id="reference-detail-title">{escape(first['title'])}</h2><div id="reference-detail-body" class="document">{first['html']}</div></section>
  </div>
  <noscript><p class="notice">This reference browser needs JavaScript to switch symbols. The generated Markdown remains available in the repository.</p></noscript>
  <script id="reference-data" type="application/json">{payload}</script>
</article>"""


REFERENCE_SCRIPT = """(() => {
  const root = document.querySelector('[data-reference-browser]');
  const source = document.getElementById('reference-data');
  if (!root || !source) return;
  const records = JSON.parse(source.textContent || '[]');
  const byId = new Map(records.map((record) => [record.id, record]));
  const filter = document.getElementById('reference-filter');
  const group = document.getElementById('reference-group');
  const status = document.getElementById('reference-status');
  const items = [...root.querySelectorAll('[data-reference-entry]')];
  const detail = document.getElementById('reference-detail');
  const detailGroup = document.getElementById('reference-detail-group');
  const detailTitle = document.getElementById('reference-detail-title');
  const detailBody = document.getElementById('reference-detail-body');
  let selectedId = records[0]?.id;
  const select = (id, updateHash = true) => {
    const record = byId.get(id);
    if (!record) return;
    items.forEach((item) => item.setAttribute('aria-current', String(item.dataset.referenceEntry === id)));
    detailGroup.textContent = record.group;
    detailTitle.textContent = record.title;
    detailBody.innerHTML = record.html;
    selectedId = id;
    if (updateHash) history.replaceState(null, '', `#ref-${id}`);
  };
  const apply = () => {
    const needle = filter.value.trim().toLocaleLowerCase();
    const visibleItems = [];
    items.forEach((item) => {
      const matches = (!needle || item.dataset.search.includes(needle)) &&
        (group.value === 'all' || item.dataset.referenceGroup === group.value);
      item.hidden = !matches;
      if (matches) visibleItems.push(item);
    });
    const preferred = HyperFluxPortal.preferredVisible({items: visibleItems, selectedId, needle, id: (item) => item.dataset.referenceEntry, title: (item) => byId.get(item.dataset.referenceEntry).title});
    if (preferred) select(preferred.dataset.referenceEntry);
    detail.hidden = visibleItems.length === 0;
    status.textContent = `Showing ${visibleItems.length} of ${items.length} entries.`;
  };
  items.forEach((item) => item.addEventListener('click', () => select(item.dataset.referenceEntry)));
  filter.addEventListener('input', apply);
  group.addEventListener('input', apply);
  const requested = location.hash.startsWith('#ref-') ? location.hash.slice(5) : '';
  if (byId.has(requested)) select(requested, false);
  else if (records.length) select(records[0].id, false);
  addEventListener('hashchange', () => {
    const id = location.hash.startsWith('#ref-') ? location.hash.slice(5) : '';
    if (byId.has(id)) select(id, false);
  });
})();
"""


REFERENCE_CSS = """
.reference-browser { min-width: 0; }
.reference-intro { max-width: 880px; margin: 22px 0 28px; color: var(--muted); }
.reference-intro > :first-child { margin-top: 0; }
.reference-workbench { display: grid; grid-template-columns: minmax(230px, 300px) minmax(0, 1fr); min-height: 620px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.reference-index { min-width: 0; padding: 18px 18px 18px 0; border-right: 1px solid var(--line); }
.reference-controls { display: grid; gap: 10px; }
.reference-controls label span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.reference-controls input, .reference-controls select { width: 100%; min-height: 40px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); font: inherit; }
.reference-index > p { color: var(--muted); font-size: 13px; }
.reference-items { display: grid; max-height: 640px; overflow-y: auto; scrollbar-width: thin; }
.reference-item { min-height: 48px; padding: 8px 10px; border: 0; border-left: 2px solid transparent; color: var(--muted); background: transparent; font: inherit; text-align: left; cursor: pointer; }
.reference-item span, .reference-item small { display: block; }
.reference-item small { margin-top: 2px; color: var(--muted); font-size: 11px; }
.reference-item:hover, .reference-item[aria-current="true"] { border-left-color: var(--teal); color: var(--ink); background: var(--surface); }
.reference-detail { min-width: 0; padding: 24px 0 48px 34px; }
.reference-detail h2 { margin: 2px 0 18px; font-size: 27px; }
.reference-detail .document table { display: table; }
@media (max-width: 800px) {
  .reference-workbench { grid-template-columns: 1fr; }
  .reference-index { padding-right: 0; border-right: 0; border-bottom: 1px solid var(--line); }
  .reference-items { grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 280px; }
  .reference-detail { padding-left: 0; }
}
@media (max-width: 520px) { .reference-items { grid-template-columns: 1fr; } }
"""
