# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from html import escape
import json
from typing import Any

from .portal_book import DesignBook


STATUS_LABELS = {
    "software-verified": "Software verified",
    "policy-defined": "Policy defined",
    "partially-implemented": "Partially implemented",
    "blocked-by-physical-evidence": "Needs physical evidence",
    "publication-locked": "Publication locked",
}


def _status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value.replace("-", " ").title())


def render_coverage_browser(entries: tuple[Any, ...], book: DesignBook) -> str:
    chapter_by_section = {
        section.number: chapter for chapter in book.chapters for section in chapter.sections
    }
    records = []
    for entry in entries:
        chapter = chapter_by_section[entry.section]
        remaining = list(entry.remaining) or ["No remaining work is recorded for this section."]
        records.append(
            {
                "id": f"section-{entry.section}",
                "section": entry.section,
                "title": entry.title,
                "chapter": f"Chapter {chapter.roman}: {chapter.title}",
                "chapter_url": f"../developers/design-book/{chapter.slug}.html#section-{entry.section}",
                "status": entry.status,
                "status_label": _status_label(entry.status),
                "owner": entry.owner,
                "release_blocking": entry.release_blocking,
                "physical_evidence_required": entry.physical_evidence_required,
                "evidence": list(entry.evidence),
                "remaining": remaining,
                "search": f"{entry.section} {entry.title} {chapter.title} {entry.owner} {entry.status} {' '.join(entry.remaining)}".casefold(),
            }
        )
    verified = sum(entry.status == "software-verified" for entry in entries)
    attention = sum(entry.status != "software-verified" for entry in entries)
    blocking = sum(entry.release_blocking for entry in entries)
    physical = sum(entry.physical_evidence_required for entry in entries)
    options = "".join(
        f'<option value="{escape(status)}">{escape(_status_label(status))}</option>'
        for status in STATUS_LABELS
        if any(entry.status == status for entry in entries)
    )
    list_items = "".join(
        '<button type="button" class="coverage-item" '
        f'data-coverage-entry="{escape(record["id"])}" '
        f'data-coverage-status="{escape(record["status"])}" '
        f'data-coverage-blocking="{str(record["release_blocking"]).lower()}" '
        f'data-search="{escape(record["search"], quote=True)}">'
        f'<span class="coverage-number">{record["section"]}</span><span><strong>{escape(record["title"])}</strong><small>{escape(record["status_label"])}</small></span></button>'
        for record in records
    )
    first = next((record for record in records if record["release_blocking"]), records[0])
    payload = json.dumps(records, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    return f"""<article class="coverage-browser" data-coverage-browser>
  <nav class="breadcrumb" aria-label="Breadcrumb"><a href="release-gates.html">Maintain</a><span>Design coverage</span></nav>
  <header class="page-hero page-hero--ledger"><p class="page-kicker">Implementation ledger</p><h1>Design coverage</h1><p class="lede">A focused view of what still needs work, with completed sections available on demand instead of repeated in one enormous report.</p></header>
  <section class="coverage-summary" aria-label="Coverage summary"><div><strong>{verified}</strong><span>software verified</span></div><div><strong>{attention}</strong><span>need attention</span></div><div><strong>{blocking}</strong><span>release blocking</span></div><div><strong>{physical}</strong><span>need hardware evidence</span></div></section>
  <div class="coverage-progress" aria-label="{verified} of {len(entries)} sections software verified"><span style="width:{verified / len(entries) * 100:.2f}%"></span></div>
  <div class="coverage-workbench">
    <aside class="coverage-index" aria-label="Design sections"><div class="coverage-controls"><label><span>Find a section</span><input id="coverage-filter" type="search" placeholder="Section, owner, chapter, or gap" autocomplete="off"></label><label><span>Status</span><select id="coverage-status"><option value="attention">Needs attention</option><option value="all">All statuses</option>{options}</select></label><label class="coverage-check"><input id="coverage-blocking" type="checkbox"><span>Release blocking only</span></label></div><p id="coverage-filter-status" role="status" aria-live="polite"></p><div class="coverage-items">{list_items}</div></aside>
    <section class="coverage-detail" id="coverage-detail" tabindex="-1"><p class="page-kicker" id="coverage-detail-chapter">{escape(first['chapter'])}</p><h2 id="coverage-detail-title">{first['section']}. {escape(first['title'])}</h2><div id="coverage-detail-body"></div></section>
  </div>
  <script id="coverage-data" type="application/json">{payload}</script>
</article>"""


COVERAGE_SCRIPT = """(() => {
  const root = document.querySelector('[data-coverage-browser]');
  const source = document.getElementById('coverage-data');
  if (!root || !source) return;
  const records = JSON.parse(source.textContent || '[]');
  const byId = new Map(records.map((record) => [record.id, record]));
  const items = [...root.querySelectorAll('[data-coverage-entry]')];
  const query = document.getElementById('coverage-filter');
  const status = document.getElementById('coverage-status');
  const blocking = document.getElementById('coverage-blocking');
  const message = document.getElementById('coverage-filter-status');
  const detail = document.getElementById('coverage-detail');
  const chapter = document.getElementById('coverage-detail-chapter');
  const title = document.getElementById('coverage-detail-title');
  const body = document.getElementById('coverage-detail-body');
  let selectedId = records[0]?.id;
  const list = (values) => `<ul>${values.map((value) => `<li>${escapeHtml(value)}</li>`).join('')}</ul>`;
  const escapeHtml = (value) => String(value).replace(/[&<>\"']/g, (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[character]));
  const select = (id, updateHash = true) => {
    const record = byId.get(id);
    if (!record) return;
    items.forEach((item) => item.setAttribute('aria-current', String(item.dataset.coverageEntry === id)));
    chapter.textContent = record.chapter;
    title.textContent = `${record.section}. ${record.title}`;
    body.innerHTML = `<div class="coverage-facts"><div><span>Status</span><strong>${escapeHtml(record.status_label)}</strong></div><div><span>Owner</span><strong>${escapeHtml(record.owner)}</strong></div><div><span>Release</span><strong>${record.release_blocking ? 'Blocking' : 'Not blocking'}</strong></div><div><span>Hardware</span><strong>${record.physical_evidence_required ? 'Evidence required' : 'Not required'}</strong></div></div><section><h3>What remains</h3>${list(record.remaining)}</section><section><h3>Evidence sources</h3>${list(record.evidence)}</section><a class="text-link" href="${escapeHtml(record.chapter_url)}">Read this design section</a>`;
    selectedId = id;
    if (updateHash) history.replaceState(null, '', `#coverage-${id}`);
  };
  const apply = () => {
    const needle = query.value.trim().toLocaleLowerCase();
    const visibleItems = [];
    items.forEach((item) => {
      const statusMatch = status.value === 'all' ||
        (status.value === 'attention' ? item.dataset.coverageStatus !== 'software-verified' : item.dataset.coverageStatus === status.value);
      const match = (!needle || item.dataset.search.includes(needle)) && statusMatch &&
        (!blocking.checked || item.dataset.coverageBlocking === 'true');
      item.hidden = !match;
      if (match) visibleItems.push(item);
    });
    const preferred = HyperFluxPortal.preferredVisible({items: visibleItems, selectedId, needle, id: (item) => item.dataset.coverageEntry, title: (item) => byId.get(item.dataset.coverageEntry).title});
    if (preferred) select(preferred.dataset.coverageEntry);
    detail.hidden = visibleItems.length === 0;
    message.textContent = `Showing ${visibleItems.length} of ${items.length} sections.`;
  };
  items.forEach((item) => item.addEventListener('click', () => select(item.dataset.coverageEntry)));
  [query, status, blocking].forEach((control) => control.addEventListener('input', apply));
  const requested = location.hash.startsWith('#coverage-') ? location.hash.slice(10) : '';
  select(byId.has(requested) ? requested : records.find((record) => record.release_blocking)?.id || records[0].id, false);
  apply();
  addEventListener('hashchange', () => {
    const id = location.hash.startsWith('#coverage-') ? location.hash.slice(10) : '';
    if (byId.has(id)) select(id, false);
  });
})();
"""


COVERAGE_CSS = """
.coverage-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 26px 0 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.coverage-summary div { padding: 16px 18px; border-right: 1px solid var(--line); }
.coverage-summary div:last-child { border-right: 0; }
.coverage-summary strong, .coverage-summary span { display: block; }
.coverage-summary strong { font: 700 26px/1 var(--display-font); }
.coverage-summary span { margin-top: 6px; color: var(--muted); font-size: 13px; }
.coverage-progress { height: 5px; margin-bottom: 28px; background: var(--surface-strong); }
.coverage-progress span { display: block; height: 100%; background: var(--teal); }
.coverage-workbench { display: grid; grid-template-columns: minmax(260px, 340px) minmax(0, 1fr); min-height: 640px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.coverage-index { min-width: 0; padding: 18px 18px 18px 0; border-right: 1px solid var(--line); }
.coverage-controls { display: grid; gap: 10px; }
.coverage-controls label > span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.coverage-controls input[type="search"], .coverage-controls select { width: 100%; min-height: 40px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); font: inherit; }
.coverage-controls .coverage-check { display: flex; align-items: center; gap: 8px; min-height: 36px; }
.coverage-controls .coverage-check span { margin: 0; }
.coverage-index > p { color: var(--muted); font-size: 13px; }
.coverage-items { display: grid; max-height: 610px; overflow-y: auto; scrollbar-width: thin; }
.coverage-item { display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 8px; min-height: 54px; align-items: center; padding: 7px 9px; border: 0; border-left: 2px solid transparent; color: var(--muted); background: transparent; font: inherit; text-align: left; cursor: pointer; }
.coverage-item strong, .coverage-item small { display: block; }
.coverage-item small { margin-top: 2px; color: var(--muted); font-size: 11px; }
.coverage-item:hover, .coverage-item[aria-current="true"] { border-left-color: var(--yellow); color: var(--ink); background: var(--surface); }
.coverage-number { color: var(--yellow); font: 700 14px/1 var(--display-font); }
.coverage-detail { min-width: 0; padding: 24px 0 48px 34px; }
.coverage-detail h2 { margin: 2px 0 22px; font-size: 27px; }
.coverage-detail h3 { margin-top: 28px; font-size: 16px; }
.coverage-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.coverage-facts div { padding: 12px; border-right: 1px solid var(--line); }
.coverage-facts div:last-child { border-right: 0; }
.coverage-facts span, .coverage-facts strong { display: block; }
.coverage-facts span { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.coverage-facts strong { margin-top: 4px; font-size: 13px; }
@media (max-width: 820px) {
  .coverage-workbench { grid-template-columns: 1fr; }
  .coverage-index { padding-right: 0; border-right: 0; border-bottom: 1px solid var(--line); }
  .coverage-items { grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 320px; }
  .coverage-detail { padding-left: 0; }
}
@media (max-width: 580px) {
  .coverage-summary, .coverage-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .coverage-summary div:nth-child(2), .coverage-facts div:nth-child(2) { border-right: 0; }
  .coverage-summary div:nth-child(-n + 2), .coverage-facts div:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
  .coverage-items { grid-template-columns: 1fr; }
}
"""
