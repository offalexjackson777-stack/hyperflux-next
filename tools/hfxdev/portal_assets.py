# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations


def architecture_svg() -> str:
    labels = ("Applications", "SDK", "Bridge", "Kernel", "Receiver")
    colors = ("#6ccff6", "#b7df50", "#43d6b5", "#ffd166", "#ff7d6e")
    boxes = []
    arrows = []
    for index, (label, color) in enumerate(zip(labels, colors, strict=True)):
        x = 24 + index * 174
        boxes.append(
            f'<rect x="{x}" y="45" width="138" height="70" rx="6" fill="#1c2229" '
            f'stroke="{color}" stroke-width="2"/><text x="{x + 69}" y="86" '
            f'text-anchor="middle" fill="#edf3f2" font-family="sans-serif" font-size="16">'
            f"{label}</text>"
        )
        if index < len(labels) - 1:
            arrows.append(
                f'<path d="M {x + 140} 80 H {x + 168}" stroke="#93a4aa" stroke-width="2" '
                'marker-end="url(#arrow)"/>'
            )
    return """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="160" viewBox="0 0 900 160" role="img" aria-labelledby="title description">
<title id="title">HyperFlux responsibility flow</title>
<desc id="description">Applications use the SDK, which talks to the bridge, kernel, and receiver in one direction.</desc>
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#93a4aa"/></marker></defs>
<rect width="900" height="160" fill="#151a20"/>
""" + "".join(boxes + arrows) + "\n</svg>\n"


SITE_CSS = """:root {
  color-scheme: dark;
  --bg: #11151a;
  --bg-elevated: #151a20;
  --surface: #1c2229;
  --surface-strong: #252d35;
  --line: #39444d;
  --ink: #edf3f2;
  --muted: #aebbc0;
  --teal: #43d6b5;
  --lime: #b7df50;
  --coral: #ff7d6e;
  --yellow: #ffd166;
  --cyan: #6ccff6;
  --danger: #ff5964;
  --code-bg: #0c1014;
  --shadow: rgb(0 0 0 / 35%);
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #f5f7f6;
  --bg-elevated: #ffffff;
  --surface: #ffffff;
  --surface-strong: #e8eeec;
  --line: #b6c1bf;
  --ink: #17211f;
  --muted: #52625f;
  --teal: #087f6b;
  --lime: #557c10;
  --coral: #b43e36;
  --yellow: #8a6500;
  --cyan: #176fa0;
  --danger: #b51f32;
  --code-bg: #e9efed;
  --shadow: rgb(31 48 44 / 20%);
}
* { box-sizing: border-box; letter-spacing: 0; }
html { background: var(--bg); scroll-behavior: smooth; }
body { margin: 0; color: var(--ink); background: var(--bg); font: 15px/1.62 "Noto Sans", "DejaVu Sans", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
a { color: var(--cyan); text-underline-offset: 3px; }
a:hover { color: var(--teal); }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid var(--yellow); outline-offset: 3px; }
.skip-link { position: fixed; top: 8px; left: 8px; z-index: 20; padding: 8px 12px; background: var(--yellow); color: #11151a; transform: translateY(-180%); }
.skip-link:focus { transform: translateY(0); }
.site-header { position: sticky; top: 0; z-index: 10; min-height: 72px; display: grid; grid-template-columns: minmax(220px, 300px) minmax(260px, 1fr) auto; align-items: center; gap: 20px; padding: 10px 24px; border-bottom: 1px solid var(--line); background: var(--bg-elevated); }
.brand { display: flex; align-items: center; gap: 10px; color: var(--ink); text-decoration: none; }
.brand-mark { display: grid; place-items: center; width: 38px; height: 38px; border: 1px solid var(--teal); border-radius: 6px; color: var(--teal); font-weight: 800; }
.brand strong, .brand small { display: block; }
.brand small { color: var(--muted); font-size: 12px; }
.search-box { position: relative; }
.search-box input { width: 100%; min-height: 40px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 6px; color: var(--ink); background: var(--surface); font: inherit; }
.search-results { position: absolute; top: 46px; right: 0; left: 0; max-height: 360px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--surface-strong); box-shadow: 0 12px 28px var(--shadow); }
.search-results a { display: block; padding: 10px 12px; border-bottom: 1px solid var(--line); color: var(--ink); text-decoration: none; }
.search-results small { display: block; color: var(--muted); }
.header-tools { display: flex; align-items: center; justify-content: end; gap: 12px; }
.phase { color: var(--yellow); font-size: 12px; white-space: nowrap; }
.theme-switch { display: inline-flex; border: 1px solid var(--line); border-radius: 5px; background: var(--surface); }
.theme-switch button { min-height: 34px; padding: 5px 8px; border: 0; border-right: 1px solid var(--line); color: var(--muted); background: transparent; font: inherit; font-size: 12px; cursor: pointer; }
.theme-switch button:last-child { border-right: 0; }
.theme-switch button[aria-pressed="true"] { color: var(--ink); background: var(--surface-strong); box-shadow: inset 0 -2px var(--teal); }
.site-grid { display: grid; grid-template-columns: 258px minmax(0, 1fr); min-height: calc(100vh - 122px); }
.side-nav { position: sticky; top: 72px; align-self: start; height: calc(100vh - 72px); overflow: auto; padding: 24px 16px; border-right: 1px solid var(--line); background: var(--bg-elevated); scrollbar-color: var(--line) var(--bg-elevated); scrollbar-width: thin; }
.side-nav > a, .nav-group a { display: block; min-height: 34px; padding: 6px 10px; border-left: 2px solid transparent; color: var(--muted); text-decoration: none; }
.nav-group { margin-top: 20px; }
.nav-group h2 { margin: 0 10px 6px; color: var(--ink); font-size: 12px; text-transform: uppercase; }
.side-nav a:hover, .side-nav a.active { border-left-color: var(--teal); color: var(--ink); background: var(--surface); }
.mobile-nav { display: none; }
main { width: min(1380px, 100%); min-width: 0; padding: 38px clamp(24px, 4%, 64px) 72px; }
.breadcrumb { margin-bottom: 12px; color: var(--muted); font-size: 13px; }
.document > h1, .home-intro h1 { margin: 0 0 10px; font-size: 34px; line-height: 1.2; }
.lede { max-width: 760px; color: var(--muted); font-size: 18px; }
.phase-band { display: flex; flex-wrap: wrap; gap: 0; margin: 24px 0 0; border: 1px solid var(--line); background: var(--line); }
.phase-band > * { flex: 1 1 190px; padding: 9px 12px; background: var(--surface); }
.phase-band strong { color: var(--teal); }
.phase-band span { color: var(--muted); }
.system-map { display: block; width: 100%; max-width: 900px; height: auto; margin: 28px 0; border: 1px solid var(--line); border-radius: 6px; background: #151a20; }
.workbench-links { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 28px 0; border: 1px solid var(--line); background: var(--line); gap: 1px; }
.workbench-links a { min-width: 0; padding: 14px 16px; color: var(--ink); background: var(--surface); text-decoration: none; }
.workbench-links a:hover { box-shadow: inset 0 3px var(--teal); }
.workbench-links strong, .workbench-links span { display: block; }
.workbench-links span { margin-top: 3px; color: var(--muted); font-size: 13px; }
.audience-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 28px 0 38px; }
.audience-card { min-width: 0; padding: 18px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.audience-card:nth-child(1) { border-top-color: var(--cyan); }
.audience-card:nth-child(2) { border-top-color: var(--lime); }
.audience-card:nth-child(3) { border-top-color: var(--coral); }
.audience-card h2 { margin: 0 0 6px; font-size: 18px; }
.audience-card p { color: var(--muted); }
.status-band { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin: 24px 0; border: 1px solid var(--line); background: var(--line); }
.status-band div { min-width: 0; padding: 16px; background: var(--surface); }
.status-band strong { display: block; color: var(--teal); font-size: 24px; }
.status-band span { color: var(--muted); }
.notice { margin: 24px 0; padding: 14px 16px; border-left: 3px solid var(--yellow); background: var(--surface); }
.document { overflow-wrap: anywhere; }
.document h2 { margin-top: 38px; padding-bottom: 8px; border-bottom: 1px solid var(--line); font-size: 23px; }
.document h3 { margin-top: 28px; font-size: 18px; }
.document p, .document li { max-width: 860px; }
.document table { display: block; width: 100%; max-width: 100%; overflow-x: auto; margin: 18px 0; border-collapse: collapse; font-size: 14px; }
.document th, .document td { padding: 9px 10px; border: 1px solid var(--line); text-align: left; vertical-align: top; }
.document th { background: var(--surface-strong); }
.document code { padding: 2px 5px; border-radius: 4px; color: var(--lime); background: var(--surface); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.document pre { max-width: 100%; overflow: auto; padding: 16px; border: 1px solid var(--line); border-radius: 6px; background: var(--code-bg); }
.document pre code { padding: 0; color: var(--ink); background: transparent; }
.document blockquote { margin-left: 0; padding: 10px 16px; border-left: 3px solid var(--yellow); color: var(--muted); background: var(--surface); }
.compiled-diagram { margin: 22px 0; padding: 16px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg-elevated); }
.compiled-diagram figcaption { margin-bottom: 14px; color: var(--muted); font-size: 13px; font-weight: 700; text-transform: uppercase; }
.diagram-pipeline, .sequence-participants { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.diagram-step { display: inline-flex; align-items: center; gap: 10px; }
.diagram-node { display: inline-flex; min-height: 42px; min-width: 112px; align-items: center; justify-content: center; padding: 8px 12px; border: 1px solid var(--teal); border-radius: 6px; color: var(--ink); background: var(--surface); text-align: center; }
.diagram-link { display: inline-grid; min-width: 44px; justify-items: center; gap: 2px; }
.diagram-label { max-width: 170px; color: var(--muted); text-align: center; }
.diagram-arrow { position: relative; display: block; width: 38px; height: 2px; background: var(--muted); }
.diagram-arrow::after { position: absolute; top: -4px; right: -1px; width: 8px; height: 8px; border-top: 2px solid var(--muted); border-right: 2px solid var(--muted); content: ""; transform: rotate(45deg); }
.diagram-edges { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.diagram-edge { display: grid; grid-template-columns: minmax(110px, 1fr) 54px minmax(110px, 1fr); align-items: center; gap: 8px; }
.sequence-messages { display: grid; gap: 8px; margin-top: 16px; }
.sequence-message { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(160px, 2fr) minmax(120px, 1fr); align-items: center; gap: 10px; padding: 8px 0; border-top: 1px solid var(--line); }
.sequence-message > span:first-child, .sequence-message > span:last-child { font-weight: 700; }
.sequence-track { display: grid; justify-items: center; gap: 4px; color: var(--muted); text-align: center; }
.sequence-track.response .diagram-arrow { background: var(--yellow); }
.sequence-track.response .diagram-arrow::after { border-color: var(--yellow); }
footer { min-height: 50px; display: flex; justify-content: space-between; gap: 18px; padding: 14px 24px; border-top: 1px solid var(--line); color: var(--muted); background: var(--bg-elevated); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 900px) {
  .site-header { position: static; grid-template-columns: 1fr; gap: 10px; }
  .header-tools { justify-content: space-between; }
  .site-grid { grid-template-columns: 1fr; }
  .desktop-nav { display: none; }
  .mobile-nav { display: block; margin: 16px 20px 0; border: 1px solid var(--line); border-radius: 6px; background: var(--bg-elevated); }
  .mobile-nav summary { min-height: 44px; padding: 9px 14px; color: var(--ink); cursor: pointer; font-weight: 700; }
  .mobile-nav[open] summary { border-bottom: 1px solid var(--line); }
  .mobile-nav-links { columns: 2; padding: 4px 8px 16px; }
  .mobile-nav-links > a, .mobile-nav-links .nav-group a { display: block; min-height: 34px; padding: 6px 10px; border-left: 2px solid transparent; color: var(--muted); text-decoration: none; }
  .mobile-nav-links a:hover, .mobile-nav-links a.active { border-left-color: var(--teal); color: var(--ink); background: var(--surface); }
  .nav-group { break-inside: avoid; }
  .audience-grid, .status-band, .workbench-links { grid-template-columns: 1fr; }
  .diagram-edges { grid-template-columns: 1fr; }
  .sequence-message { grid-template-columns: minmax(84px, 1fr) minmax(100px, 1.5fr) minmax(84px, 1fr); font-size: 13px; }
  main { padding: 28px 20px 56px; }
  footer { flex-wrap: wrap; }
}
@media (max-width: 520px) {
  .mobile-nav-links { columns: 1; }
  .diagram-pipeline { display: grid; grid-template-columns: 1fr; justify-items: stretch; }
  .diagram-pipeline > .diagram-node, .diagram-step .diagram-node { width: 100%; }
  .diagram-step { display: grid; justify-items: center; }
  .diagram-step .diagram-arrow { transform: rotate(90deg); }
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme]) {
    color-scheme: light;
    --bg: #f5f7f6;
    --bg-elevated: #ffffff;
    --surface: #ffffff;
    --surface-strong: #e8eeec;
    --line: #b6c1bf;
    --ink: #17211f;
    --muted: #52625f;
    --teal: #087f6b;
    --lime: #557c10;
    --coral: #b43e36;
    --yellow: #8a6500;
    --cyan: #176fa0;
    --danger: #b51f32;
    --code-bg: #e9efed;
    --shadow: rgb(31 48 44 / 20%);
  }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
"""


PORTAL_JS = """(() => {
  const themeButtons = [...document.querySelectorAll('[data-theme-choice]')];
  const storedTheme = (() => {
    try { return localStorage.getItem('hyperflux-theme') || 'system'; }
    catch (_error) { return 'system'; }
  })();
  const applyTheme = (choice) => {
    if (choice === 'light' || choice === 'dark') document.documentElement.dataset.theme = choice;
    else document.documentElement.removeAttribute('data-theme');
    themeButtons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === choice)));
    try { localStorage.setItem('hyperflux-theme', choice); } catch (_error) { /* local preference is optional */ }
  };
  const initialTheme = ['system', 'light', 'dark'].includes(storedTheme) ? storedTheme : 'system';
  applyTheme(initialTheme);
  themeButtons.forEach((button) => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice)));

  const input = document.getElementById('portal-search');
  const results = document.getElementById('search-results');
  const source = document.getElementById('search-index');
  if (!input || !results || !source) return;
  const records = JSON.parse(source.textContent || '[]');
  const close = () => { results.hidden = true; results.replaceChildren(); };
  input.addEventListener('input', () => {
    const query = input.value.trim().toLocaleLowerCase();
    if (query.length < 2) { close(); return; }
    const matches = records.filter((record) => record.search.includes(query)).slice(0, 8);
    results.replaceChildren(...matches.map((record) => {
      const link = document.createElement('a');
      link.href = record.url;
      const title = document.createElement('strong');
      title.textContent = record.title;
      const detail = document.createElement('small');
      detail.textContent = `${record.audience} · ${record.summary}`;
      link.append(title, detail);
      return link;
    }));
    results.hidden = matches.length === 0;
  });
  input.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-box')) close(); });
})();
"""
