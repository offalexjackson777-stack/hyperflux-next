# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations


def architecture_svg() -> str:
    labels = ("APPS", "SDK", "BRIDGE", "KERNEL", "RECEIVER")
    colors = ("#62d6ff", "#b8ef5a", "#46dbb7", "#ffd166", "#ff7d74")
    boxes = []
    arrows = []
    for index, (label, color) in enumerate(zip(labels, colors, strict=True)):
        x = 28 + index * 190
        boxes.append(
            f'<rect x="{x}" y="62" width="154" height="76" fill="#1a2026" '
            f'stroke="#34404a"/><path d="M{x} 62 H{x + 154}" stroke="{color}" '
            f'stroke-width="4"/><text x="{x + 77}" y="107" text-anchor="middle" '
            'fill="#f2f7f5" font-family="ui-monospace,monospace" font-size="17" '
            f'font-weight="700">{label}</text>'
        )
        if index < len(labels) - 1:
            arrows.append(
                f'<path d="M{x + 155} 100 H{x + 184}" stroke="{color}" '
                'stroke-width="2" marker-end="url(#arrow)"/>'
            )
    return """<svg xmlns="http://www.w3.org/2000/svg" width="950" height="200" viewBox="0 0 950 200" role="img" aria-labelledby="title description">
<title id="title">HyperFlux responsibility flow</title>
<desc id="description">Applications use the SDK, bridge, kernel, and receiver in one direction.</desc>
<defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" fill="#aebcc2"/></marker></defs>
<rect width="950" height="200" fill="#101419"/>
""" + "".join(boxes + arrows) + "\n</svg>\n"


SITE_CSS = """:root {
  color-scheme: dark;
  --bg: #0f1317;
  --bg-elevated: #14191f;
  --surface: #1a2026;
  --surface-strong: #232b33;
  --line: #34404a;
  --line-soft: #273039;
  --ink: #f2f7f5;
  --muted: #a9b7bc;
  --faint: #778890;
  --teal: #46dbb7;
  --lime: #b8ef5a;
  --coral: #ff7d74;
  --yellow: #ffd166;
  --cyan: #62d6ff;
  --danger: #ff5f6d;
  --code-bg: #090d10;
  --shadow: rgb(0 0 0 / 42%);
  --body-font: "Noto Sans", "DejaVu Sans", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --display-font: "DejaVu Sans Mono", "Liberation Mono", ui-monospace, SFMono-Regular, Consolas, monospace;
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #f3f6f5;
  --bg-elevated: #ffffff;
  --surface: #ffffff;
  --surface-strong: #e7edeb;
  --line: #afbbb8;
  --line-soft: #d4ddda;
  --ink: #17201e;
  --muted: #52615e;
  --faint: #6d7a77;
  --teal: #087c68;
  --lime: #527811;
  --coral: #b83f38;
  --yellow: #866200;
  --cyan: #146f9f;
  --danger: #b51f32;
  --code-bg: #e7edeb;
  --shadow: rgb(31 48 44 / 18%);
}
* { box-sizing: border-box; letter-spacing: 0; }
html { min-width: 320px; background: var(--bg); scroll-behavior: smooth; }
body { margin: 0; color: var(--ink); background: var(--bg); font: 15px/1.62 var(--body-font); }
a { color: var(--cyan); text-underline-offset: 3px; text-decoration-thickness: 1px; }
a:hover { color: var(--teal); }
button, input, select { font: inherit; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible, [tabindex="0"]:focus-visible { outline: 2px solid var(--yellow); outline-offset: 3px; }
[hidden] { display: none !important; }
.skip-link { position: fixed; top: 8px; left: 8px; z-index: 100; padding: 8px 12px; background: var(--yellow); color: #101419; transform: translateY(-180%); }
.skip-link:focus { transform: translateY(0); }
.site-header { position: sticky; top: 0; z-index: 30; display: grid; grid-template-columns: minmax(210px, auto) auto minmax(220px, 1fr) auto; min-height: 68px; align-items: center; gap: 22px; padding: 10px 22px; border-bottom: 1px solid var(--line); background: color-mix(in srgb, var(--bg-elevated) 96%, transparent); }
.brand { display: flex; align-items: center; gap: 10px; color: var(--ink); text-decoration: none; }
.brand-mark { display: grid; width: 39px; height: 39px; place-items: center; border: 2px solid var(--teal); color: var(--teal); font: 800 15px/1 var(--display-font); }
.brand strong, .brand small { display: block; }
.brand strong { font: 700 16px/1.2 var(--display-font); }
.brand small { margin-top: 3px; color: var(--muted); font-size: 11px; }
.primary-nav { display: flex; align-items: center; gap: 2px; }
.primary-nav a { min-height: 36px; padding: 7px 10px; border-bottom: 2px solid transparent; color: var(--muted); text-decoration: none; }
.primary-nav a:hover, .primary-nav a.active { border-bottom-color: var(--teal); color: var(--ink); }
.search-box { position: relative; justify-self: stretch; width: min(480px, 100%); }
.search-box input { width: 100%; min-height: 40px; padding: 7px 12px 7px 38px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); }
.search-box::before { position: absolute; top: 9px; left: 13px; z-index: 1; color: var(--faint); font: 700 14px/1.5 var(--display-font); content: "/"; }
.search-results { position: absolute; top: 46px; right: 0; left: 0; max-height: min(460px, 70vh); overflow: auto; border: 1px solid var(--line); border-radius: 4px; background: var(--bg-elevated); box-shadow: 0 16px 34px var(--shadow); }
.search-results a { display: block; padding: 10px 12px; border-bottom: 1px solid var(--line-soft); color: var(--ink); text-decoration: none; }
.search-results a:last-child { border-bottom: 0; }
.search-results a:hover, .search-results a[aria-selected="true"] { background: var(--surface); }
.search-results strong, .search-results small { display: block; }
.search-results small { color: var(--muted); }
.search-empty { padding: 14px; color: var(--muted); }
.header-tools { display: flex; align-items: center; justify-content: end; gap: 10px; }
.phase { padding: 2px 7px; border: 1px solid var(--yellow); color: var(--yellow); font: 700 11px/1.5 var(--display-font); white-space: nowrap; }
.theme-cycle { min-height: 36px; padding: 6px 9px; border: 1px solid var(--line); border-radius: 4px; color: var(--muted); background: var(--surface); cursor: pointer; }
.site-grid { display: grid; grid-template-columns: 236px minmax(0, 1fr); min-height: calc(100vh - 116px); }
.side-nav { position: sticky; top: 68px; align-self: start; height: calc(100vh - 68px); overflow-y: auto; padding: 25px 15px; border-right: 1px solid var(--line); background: var(--bg-elevated); scrollbar-color: var(--line) transparent; scrollbar-width: thin; }
.side-nav > a, .mobile-nav-links > a { display: block; min-height: 35px; padding: 6px 10px; border-left: 2px solid transparent; color: var(--muted); text-decoration: none; }
.side-nav > a:hover, .side-nav > a.active, .mobile-nav-links > a:hover, .mobile-nav-links > a.active { border-left-color: var(--teal); color: var(--ink); background: var(--surface); }
.nav-context { margin: 0 10px 12px; }
.nav-context p { margin: 5px 0 0; color: var(--faint); font-size: 12px; line-height: 1.45; }
.nav-context--secondary { margin-top: 28px; }
.nav-label, .page-kicker { color: var(--teal); font: 700 11px/1.4 var(--display-font); text-transform: uppercase; }
.mobile-nav { display: none; }
.page-frame { display: grid; grid-template-columns: minmax(0, 1fr) 210px; width: min(1540px, 100%); min-width: 0; }
main { min-width: 0; padding: 42px clamp(24px, 4.2vw, 70px) 76px; }
.page-outline { position: sticky; top: 96px; align-self: start; max-height: calc(100vh - 120px); overflow-y: auto; margin: 42px 22px 40px 0; padding-left: 17px; border-left: 1px solid var(--line); scrollbar-width: thin; }
.page-outline > span { display: block; margin-bottom: 8px; color: var(--ink); font: 700 12px/1.4 var(--display-font); }
.page-outline a { display: block; padding: 4px 0; color: var(--muted); font-size: 12px; line-height: 1.4; text-decoration: none; }
.page-outline a:hover { color: var(--ink); }
.page-outline .outline-level-3 { padding-left: 12px; color: var(--faint); }
.page-outline .outline-top { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line-soft); color: var(--cyan); }
.breadcrumb { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin: 0 0 20px; color: var(--faint); font-size: 12px; }
.breadcrumb > * + *::before { margin-right: 7px; color: var(--line); content: "/"; }
.breadcrumb a { color: var(--muted); text-decoration: none; }
.page-hero { max-width: 940px; margin-bottom: 32px; padding-bottom: 26px; border-bottom: 1px solid var(--line); }
.page-hero h1 { max-width: 880px; margin: 5px 0 10px; font: 700 clamp(30px, 4vw, 46px)/1.08 var(--display-font); overflow-wrap: anywhere; }
.page-hero .lede { max-width: 780px; margin: 0; color: var(--muted); font-size: 18px; }
.page-hero--book .page-kicker { color: var(--yellow); }
.page-hero--ledger .page-kicker { color: var(--coral); }
.page-hero--reference .page-kicker { color: var(--cyan); }
.page-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 18px; color: var(--faint); font-size: 12px; }
.page-meta span { position: relative; }
.page-meta span + span::before { position: absolute; left: -11px; color: var(--line); content: "/"; }
.section-intro { max-width: 760px; margin-bottom: 20px; }
.section-intro h2 { margin: 4px 0 7px; font: 700 clamp(23px, 3vw, 32px)/1.2 var(--display-font); }
.section-intro p:last-child { color: var(--muted); }
.button { display: inline-flex; min-height: 42px; align-items: center; justify-content: center; padding: 8px 14px; border: 1px solid var(--line); border-radius: 4px; color: var(--ink); background: var(--surface); font-weight: 700; text-decoration: none; }
.button:hover { border-color: var(--teal); color: var(--ink); }
.button--primary { border-color: var(--teal); color: #08110f; background: var(--teal); }
.button--primary:hover { color: #08110f; background: var(--lime); }
.home { min-width: 0; }
.home-hero { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(260px, .65fr); gap: clamp(32px, 7vw, 96px); align-items: end; min-height: min(610px, calc(100vh - 150px)); padding: clamp(38px, 8vh, 92px) 0 52px; border-bottom: 1px solid var(--line); }
.home-copy h1 { max-width: 880px; margin: 7px 0 16px; font: 700 clamp(46px, 7vw, 86px)/.98 var(--display-font); }
.home-lede { max-width: 760px; margin: 0; color: var(--ink); font-size: clamp(20px, 2.2vw, 28px); line-height: 1.35; }
.home-boundary { max-width: 650px; color: var(--muted); }
.home-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 28px; }
.home-signal { padding: 18px 0 18px 18px; border-left: 3px solid var(--yellow); }
.home-signal span, .home-signal strong { display: block; }
.home-signal span { color: var(--yellow); font: 700 11px/1.4 var(--display-font); text-transform: uppercase; }
.home-signal strong { margin-top: 7px; font: 700 18px/1.3 var(--display-font); }
.home-signal p { color: var(--muted); }
.flow-section, .path-section, .truth-section { padding: 64px 0; border-bottom: 1px solid var(--line); }
.system-map { display: block; width: 100%; max-width: 950px; height: auto; margin-top: 28px; border: 1px solid var(--line); background: #101419; }
.path-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.path-grid a { min-width: 0; padding: 22px 24px; border-right: 1px solid var(--line); color: var(--ink); text-decoration: none; }
.path-grid a:last-child { border-right: 0; }
.path-grid a:hover { background: var(--surface); }
.path-grid span { color: var(--teal); font: 700 12px/1 var(--display-font); }
.path-grid strong { display: block; margin-top: 18px; font: 700 18px/1.3 var(--display-font); }
.path-grid p { margin-bottom: 0; color: var(--muted); }
.status-band, .book-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.status-band div, .book-summary div { min-width: 0; padding: 18px; border-right: 1px solid var(--line); }
.status-band div:last-child, .book-summary div:last-child { border-right: 0; }
.status-band strong, .status-band span, .book-summary strong, .book-summary span { display: block; }
.status-band strong, .book-summary strong { color: var(--teal); font: 700 28px/1 var(--display-font); }
.status-band span, .book-summary span { margin-top: 7px; color: var(--muted); font-size: 13px; }
.truth-note { max-width: 900px; color: var(--muted); }
.document { max-width: 1040px; overflow-wrap: anywhere; }
.document--guide, .document--concept { max-width: 920px; }
.document-body { max-width: 920px; }
.document--guide .document-body, .document--concept .document-body { max-width: 800px; }
.document-body h2, .document-body h3 { position: relative; scroll-margin-top: 96px; }
.document-body h2 { margin: 46px 0 14px; padding-top: 4px; font: 700 25px/1.25 var(--display-font); }
.document-body h3 { margin: 32px 0 10px; font: 700 18px/1.3 var(--display-font); }
.document-body p, .document-body li { max-width: 800px; }
.document-body li + li { margin-top: 5px; }
.heading-anchor { position: absolute; right: 100%; margin-right: 8px; color: var(--faint); font: 400 14px/1.6 var(--display-font); text-decoration: none; opacity: 0; }
.document-body h2:hover .heading-anchor, .document-body h3:hover .heading-anchor, .heading-anchor:focus { opacity: 1; }
.table-scroll { position: relative; max-width: 100%; margin: 20px 0; overflow-x: auto; border: 1px solid var(--line); scrollbar-color: var(--line) var(--surface); }
.table-scroll table { width: 100%; min-width: 620px; border-collapse: collapse; margin: 0; font-size: 13px; }
.document-body table { width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }
.document-body th, .document-body td, .table-scroll th, .table-scroll td { padding: 9px 10px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
.document-body tr:last-child > *, .table-scroll tr:last-child > * { border-bottom: 0; }
.document-body tr > *:last-child, .table-scroll tr > *:last-child { border-right: 0; }
.document-body thead th, .table-scroll thead th { position: sticky; top: 0; color: var(--ink); background: var(--surface-strong); }
code { color: var(--lime); font-family: var(--display-font); overflow-wrap: anywhere; }
.document-body code { padding: 2px 5px; border-radius: 3px; background: var(--surface); }
.document-body pre { max-width: 100%; overflow: auto; padding: 17px; border: 1px solid var(--line); border-radius: 4px; background: var(--code-bg); }
.document-body pre code { padding: 0; color: var(--ink); background: transparent; }
.document-body blockquote { margin: 22px 0; padding: 12px 16px; border-left: 3px solid var(--yellow); color: var(--muted); background: var(--surface); }
.document-body blockquote > :first-child { margin-top: 0; }
.document-body blockquote > :last-child { margin-bottom: 0; }
.notice { margin: 24px 0; padding: 13px 15px; border-left: 3px solid var(--yellow); color: var(--muted); background: var(--surface); }
.notice strong { color: var(--ink); }
.source-note { margin-top: 52px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.source-note summary { min-height: 44px; padding: 10px 0; color: var(--muted); cursor: pointer; }
.source-note p { max-width: 800px; }
.page-pager { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 46px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.page-pager a { min-height: 82px; padding: 15px 17px; color: var(--ink); text-decoration: none; }
.page-pager a + a { border-left: 1px solid var(--line); text-align: right; }
.page-pager a:only-child[rel="next"] { grid-column: 2; border-left: 1px solid var(--line); }
.page-pager span, .page-pager strong { display: block; }
.page-pager span { color: var(--muted); font-size: 12px; }
.page-pager strong { margin-top: 5px; }
.page-pager a:hover { background: var(--surface); }
.compiled-diagram { margin: 26px 0; padding: 20px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); background: var(--bg-elevated); }
.compiled-diagram figcaption { margin-bottom: 17px; color: var(--muted); font: 700 11px/1.4 var(--display-font); text-transform: uppercase; }
.diagram-pipeline, .sequence-participants { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.diagram-step { display: inline-flex; align-items: center; gap: 10px; }
.diagram-node { display: inline-flex; min-height: 42px; min-width: 112px; align-items: center; justify-content: center; padding: 8px 12px; border-top: 3px solid var(--teal); color: var(--ink); background: var(--surface); text-align: center; }
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
.design-book-index { max-width: 1120px; }
.book-summary { margin: 26px 0 48px; }
.book-layout { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 48px; align-items: start; }
.book-chapters { border-top: 1px solid var(--line); }
.book-chapter { display: grid; grid-template-columns: 48px minmax(0, 1fr) auto; gap: 14px; min-height: 76px; align-items: center; padding: 12px 10px; border-bottom: 1px solid var(--line); color: var(--ink); text-decoration: none; }
.book-chapter:hover { background: var(--surface); }
.book-number { color: var(--yellow); font: 700 18px/1 var(--display-font); }
.book-chapter-copy strong, .book-chapter-copy small { display: block; }
.book-chapter-copy strong { font: 700 16px/1.3 var(--display-font); }
.book-chapter-copy small { margin-top: 4px; color: var(--muted); }
.book-arrow { color: var(--teal); font: 700 20px/1 var(--display-font); }
.book-note { position: sticky; top: 98px; padding: 18px 0 18px 18px; border-left: 3px solid var(--yellow); }
.book-note strong { font: 700 15px/1.3 var(--display-font); }
.book-note p { color: var(--muted); }
.text-link { display: inline-flex; margin-top: 18px; font-weight: 700; }
footer { min-height: 48px; display: flex; justify-content: space-between; gap: 18px; padding: 13px 22px; border-top: 1px solid var(--line); color: var(--faint); background: var(--bg-elevated); font-size: 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 1180px) {
  .site-header { grid-template-columns: auto 1fr auto; }
  .primary-nav { display: none; }
  .page-frame { grid-template-columns: minmax(0, 1fr); }
  .page-outline { display: none; }
}
@media (max-width: 900px) {
  .site-header { position: static; grid-template-columns: 1fr auto; gap: 10px; }
  .search-box { grid-column: 1 / -1; grid-row: 2; width: 100%; }
  .site-grid { grid-template-columns: 1fr; }
  .desktop-nav { display: none; }
  .mobile-nav { display: block; margin: 16px 20px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); background: var(--bg-elevated); }
  .mobile-nav summary { min-height: 44px; padding: 10px 2px; color: var(--ink); cursor: pointer; font-weight: 700; }
  .mobile-nav[open] summary { border-bottom: 1px solid var(--line); }
  .mobile-nav-links { padding: 12px 0; columns: 2; }
  .mobile-nav-links .nav-context { break-inside: avoid; }
  main { padding: 32px 20px 58px; }
  .home-hero { grid-template-columns: 1fr; min-height: auto; }
  .path-grid { grid-template-columns: 1fr; }
  .path-grid a { border-right: 0; border-bottom: 1px solid var(--line); }
  .path-grid a:last-child { border-bottom: 0; }
  .status-band, .book-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .status-band div:nth-child(2), .book-summary div:nth-child(2) { border-right: 0; }
  .status-band div:nth-child(-n + 2), .book-summary div:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
  .book-layout { grid-template-columns: 1fr; }
  .book-note { position: static; }
  .diagram-edges { grid-template-columns: 1fr; }
  .sequence-message { grid-template-columns: minmax(84px, 1fr) minmax(100px, 1.5fr) minmax(84px, 1fr); font-size: 13px; }
  footer { flex-wrap: wrap; }
}
@media (max-width: 560px) {
  .site-header { padding: 10px 14px; }
  .brand small, .phase { display: none; }
  .theme-cycle { font-size: 12px; }
  .mobile-nav-links { columns: 1; }
  .home-copy h1 { font-size: 42px; }
  .page-hero h1 { font-size: 30px; }
  .status-band, .book-summary { grid-template-columns: 1fr; }
  .status-band div, .status-band div:nth-child(2), .book-summary div, .book-summary div:nth-child(2) { border-right: 0; border-bottom: 1px solid var(--line); }
  .status-band div:last-child, .book-summary div:last-child { border-bottom: 0; }
  .page-pager { grid-template-columns: 1fr; }
  .page-pager a:only-child[rel="next"], .page-pager a + a { grid-column: 1; border-top: 1px solid var(--line); border-left: 0; text-align: left; }
  .diagram-pipeline { display: grid; grid-template-columns: 1fr; justify-items: stretch; }
  .diagram-pipeline > .diagram-node, .diagram-step .diagram-node { width: 100%; }
  .diagram-step { display: grid; justify-items: center; }
  .diagram-step .diagram-arrow { transform: rotate(90deg); }
  .sequence-message { grid-template-columns: 1fr; }
  .book-chapter { grid-template-columns: 38px minmax(0, 1fr); }
  .book-arrow { display: none; }
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme]) {
    color-scheme: light;
    --bg: #f3f6f5; --bg-elevated: #ffffff; --surface: #ffffff; --surface-strong: #e7edeb;
    --line: #afbbb8; --line-soft: #d4ddda; --ink: #17201e; --muted: #52615e; --faint: #6d7a77;
    --teal: #087c68; --lime: #527811; --coral: #b83f38; --yellow: #866200; --cyan: #146f9f;
    --danger: #b51f32; --code-bg: #e7edeb; --shadow: rgb(31 48 44 / 18%);
  }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
@media print {
  .site-header, .side-nav, .mobile-nav, .page-outline, footer, .page-pager { display: none !important; }
  .site-grid, .page-frame { display: block; }
  main { padding: 0; }
  body { color: #000; background: #fff; }
  a { color: #000; }
}
"""


PORTAL_JS = """(() => {
  const preferredVisible = ({items, selectedId, needle, id, title}) => {
    if (!items.length || items.some((item) => id(item) === selectedId)) return null;
    const normalized = needle.trim().toLocaleLowerCase();
    return items.find((item) => normalized && title(item).toLocaleLowerCase().includes(normalized)) || items[0];
  };
  globalThis.HyperFluxPortal = Object.freeze({preferredVisible});

  const choices = ['system', 'dark', 'light'];
  const button = document.getElementById('theme-cycle');
  const stored = (() => {
    try { return localStorage.getItem('hyperflux-theme') || 'system'; }
    catch (_error) { return 'system'; }
  })();
  let theme = choices.includes(stored) ? stored : 'system';
  const applyTheme = () => {
    if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
    else document.documentElement.removeAttribute('data-theme');
    if (button) button.textContent = `Theme: ${theme[0].toUpperCase()}${theme.slice(1)}`;
    try { localStorage.setItem('hyperflux-theme', theme); } catch (_error) { /* optional */ }
  };
  applyTheme();
  button?.addEventListener('click', () => {
    theme = choices[(choices.indexOf(theme) + 1) % choices.length];
    applyTheme();
  });

  document.querySelectorAll('.document-body table, .reference-detail table').forEach((table) => {
    if (table.parentElement?.classList.contains('table-scroll')) return;
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll';
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', 'Scrollable data table');
    table.parentNode.insertBefore(wrapper, table);
    wrapper.append(table);
  });
  document.querySelectorAll('.document-body h2[id], .document-body h3[id]').forEach((heading) => {
    const anchor = document.createElement('a');
    anchor.className = 'heading-anchor';
    anchor.href = `#${heading.id}`;
    anchor.textContent = '#';
    anchor.setAttribute('aria-label', `Link to ${heading.textContent}`);
    heading.prepend(anchor);
  });

  const input = document.getElementById('portal-search');
  const results = document.getElementById('search-results');
  if (!input || !results) return;
  let records = null;
  let loadError = false;
  const load = async () => {
    if (records || loadError) return;
    try {
      const response = await fetch(document.body.dataset.searchIndex, {cache: 'force-cache'});
      if (!response.ok) throw new Error(`search index ${response.status}`);
      records = await response.json();
    } catch (_error) {
      loadError = true;
      records = [];
    }
  };
  const close = () => { results.hidden = true; results.replaceChildren(); };
  const render = async () => {
    const query = input.value.trim().toLocaleLowerCase();
    if (query.length < 2) { close(); return; }
    await load();
    if (loadError) {
      const message = document.createElement('p');
      message.className = 'search-empty';
      message.textContent = 'Search needs the generated site to be served over HTTP.';
      results.replaceChildren(message);
      results.hidden = false;
      return;
    }
    const matches = records.filter((record) => record.search.includes(query)).slice(0, 10);
    const root = new URL(document.body.dataset.portalRoot, location.href);
    results.replaceChildren(...matches.map((record) => {
      const link = document.createElement('a');
      link.href = new URL(record.url, root).href;
      const title = document.createElement('strong');
      title.textContent = record.title;
      const detail = document.createElement('small');
      detail.textContent = `${record.audience} / ${record.summary}`;
      link.append(title, detail);
      return link;
    }));
    if (!matches.length) {
      const message = document.createElement('p');
      message.className = 'search-empty';
      message.textContent = 'No matching documentation.';
      results.append(message);
    }
    results.hidden = false;
  };
  input.addEventListener('focus', load, {once: true});
  input.addEventListener('input', render);
  input.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '')) {
      event.preventDefault();
      input.focus();
    }
  });
  document.addEventListener('click', (event) => { if (!event.target.closest('.search-box')) close(); });
})();
"""
