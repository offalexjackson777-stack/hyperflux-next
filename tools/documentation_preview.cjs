#!/usr/bin/env node
// SPDX-License-Identifier: GPL-2.0-only

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL, fileURLToPath } = require("node:url");
const { chromium } = require("playwright");
const { marked } = require("marked");

process.umask(0o077);

const SCRIPT_SCHEMA = "hyperflux-documentation-render-result-v2";
const RENDER_CONTRACT = "github-compatible-gfm-v2";
const SAFE_PATH = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$)).+$/;

function fail(message) {
  throw new Error(message);
}

function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!["--root", "--output", "--chromium"].includes(key) || !value) {
      fail("usage: documentation_preview.cjs --root ROOT --output DIR --chromium BINARY");
    }
    values[key] = value;
  }
  if (Object.keys(values).length !== 3) {
    fail("all root, output, and chromium arguments are required exactly once");
  }
  return {
    root: fs.realpathSync(values["--root"]),
    output: path.resolve(values["--output"]),
    chromium: fs.realpathSync(values["--chromium"]),
  };
}

function readJson(file) {
  const value = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!value || Array.isArray(value) || typeof value !== "object") {
    fail("JSON authority must contain one object");
  }
  return value;
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function sha256File(file) {
  return sha256Bytes(fs.readFileSync(file));
}

function relativeFile(root, candidate) {
  const relative = path.relative(root, candidate).split(path.sep).join("/");
  if (!relative || !SAFE_PATH.test(relative)) {
    fail("render input escapes the repository");
  }
  return relative;
}

function safeOutputName(documentPath, width, theme) {
  const stem = documentPath.replace(/\.md$/, "").replace(/[^A-Za-z0-9]+/g, "__");
  return `${stem}--${width}--${theme}.png`;
}

function htmlTemplate(markdownHtml, title, theme, baseUrl) {
  return `<!doctype html>
<html lang="en" data-theme="${theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="${baseUrl}">
<title>${escapeHtml(title)}</title>
<style>
:root { color-scheme: light; --bg:#ffffff; --fg:#1f2328; --muted:#59636e; --line:#d1d9e0; --soft:#f6f8fa; --link:#0969da; --quote:#656d76; --accent:#1f883d; }
html[data-theme="dark"] { color-scheme: dark; --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; --line:#30363d; --soft:#161b22; --link:#58a6ff; --quote:#8b949e; --accent:#3fb950; }
* { box-sizing:border-box; }
html, body { margin:0; min-width:0; background:var(--bg); color:var(--fg); }
body { font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; letter-spacing:0; }
main { width:min(100% - 32px, 980px); margin:0 auto; padding:32px 0 64px; min-width:0; }
article { min-width:0; overflow-wrap:anywhere; }
h1,h2,h3,h4,h5,h6 { line-height:1.25; letter-spacing:0; margin:1.5em 0 .55em; overflow-wrap:anywhere; }
h1 { font-size:2rem; padding-bottom:.3em; border-bottom:1px solid var(--line); margin-top:0; }
h2 { font-size:1.5rem; padding-bottom:.25em; border-bottom:1px solid var(--line); }
h3 { font-size:1.25rem; }
p,ul,ol,blockquote,pre,table { margin:0 0 1rem; }
a { color:var(--link); text-underline-offset:.2em; }
strong { color:inherit; }
blockquote { color:var(--quote); border-left:4px solid var(--line); padding:0 1em; margin-left:0; }
.markdown-alert { color:var(--fg); border-left-color:var(--link); }
.markdown-alert-important { border-left-color:var(--accent); }
.markdown-alert-title { display:block; color:var(--link); font-weight:600; margin-bottom:.25rem; text-transform:capitalize; }
.markdown-alert-important .markdown-alert-title { color:var(--accent); }
code { font:0.875em/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; background:var(--soft); border-radius:4px; padding:.15em .35em; overflow-wrap:normal; }
p code, li code, a code { white-space:normal; overflow-wrap:anywhere; word-break:break-word; }
pre { max-width:100%; overflow:auto; background:var(--soft); border:1px solid var(--line); border-radius:6px; padding:16px; }
pre code { padding:0; background:transparent; white-space:pre; }
table { display:block; width:max-content; max-width:100%; overflow-x:auto; border-collapse:collapse; }
th,td { border:1px solid var(--line); padding:6px 13px; min-width:0; }
tr:nth-child(2n) { background:var(--soft); }
img { max-width:100%; height:auto; }
hr { height:1px; border:0; background:var(--line); margin:24px 0; }
details { max-width:100%; margin:0 0 1rem; }
summary { cursor:pointer; color:var(--link); }
@media (max-width:480px) { main { width:min(100% - 24px, 980px); padding-top:24px; } h1 { font-size:1.75rem; } h2 { font-size:1.35rem; } }
</style>
</head>
<body><main><article class="markdown-body">${markdownHtml}</article></main></body>
</html>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function inlineRepositoryImages(html, root, source) {
  const mediaTypes = new Map([
    [".gif", "image/gif"],
    [".jpeg", "image/jpeg"],
    [".jpg", "image/jpeg"],
    [".png", "image/png"],
    [".svg", "image/svg+xml"],
    [".webp", "image/webp"],
  ]);
  return html.replace(/(<img\b[^>]*\bsrc=")([^"]+)("[^>]*>)/gi, (match, before, href, after) => {
    let url;
    try { url = new URL(href, pathToFileURL(source).href); } catch { fail("image source is malformed"); }
    if (["http:", "https:", "data:"].includes(url.protocol)) return match;
    if (url.protocol !== "file:") fail("image source uses an unsupported protocol");
    const local = fileURLToPath(url);
    const relative = path.relative(root, local);
    if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      fail("image source escapes the repository");
    }
    const metadata = fs.lstatSync(local);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 1048576) {
      fail("local image is not one bounded regular file");
    }
    const mediaType = mediaTypes.get(path.extname(local).toLowerCase());
    if (!mediaType) fail("local image has an unsupported format");
    const encoded = fs.readFileSync(local).toString("base64");
    return `${before}data:${mediaType};base64,${encoded}${after}`;
  });
}

function renderGitHubAlerts(html) {
  return html.replace(
    /<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/g,
    (match, kind) => `<blockquote class="markdown-alert markdown-alert-${kind.toLowerCase()}"><p><span class="markdown-alert-title">${kind.toLowerCase()}</span>`,
  );
}

function validateAuthorities(root, catalog, policy) {
  const expectedPolicyKeys = [
    "$schema", "capture", "limits", "render_modes", "schema", "screenshot_documents", "themes", "viewports",
  ];
  if (Object.keys(policy).sort().join("\0") !== expectedPolicyKeys.sort().join("\0")) {
    fail("render policy has missing or unknown fields");
  }
  if (policy.schema !== "hyperflux-documentation-render-policy-v2") {
    fail("unsupported render policy schema");
  }
  if (JSON.stringify(policy.render_modes) !== JSON.stringify(["human", "hybrid", "generated"]) ||
      JSON.stringify(policy.themes) !== JSON.stringify(["light", "dark"]) ||
      JSON.stringify(policy.viewports) !== JSON.stringify([
        { width: 375, height: 812 }, { width: 768, height: 1024 }, { width: 1280, height: 900 },
      ])) {
    fail("render policy does not cover the required modes, themes, and viewports");
  }
  if (!policy.capture || Object.keys(policy.capture).sort().join("\0") !== "max_page_height\0mode" ||
      policy.capture.mode !== "full-page" || policy.capture.max_page_height !== 32768) {
    fail("render policy does not capture complete bounded pages");
  }
  if (!Array.isArray(catalog.documents) || catalog.documents.length > policy.limits.max_documents) {
    fail("document catalog exceeds the render bound");
  }
  const renderDocuments = catalog.documents.filter((item) => policy.render_modes.includes(item.mode));
  if (renderDocuments.length === 0 || renderDocuments.length > policy.limits.max_documents) {
    fail("render policy selected an invalid document count");
  }
  const paths = new Set();
  for (const record of renderDocuments) {
    if (typeof record.path !== "string" || !SAFE_PATH.test(record.path) || paths.has(record.path)) {
      fail("render document paths must be unique and repository-relative");
    }
    paths.add(record.path);
    const source = path.resolve(root, record.path);
    if (relativeFile(root, source) !== record.path || !fs.statSync(source).isFile()) {
      fail("render document is unavailable");
    }
  }
  if (!Array.isArray(policy.screenshot_documents) ||
      policy.screenshot_documents.length * policy.viewports.length * policy.themes.length !== policy.limits.max_screenshots) {
    fail("screenshot inventory does not match its exact bound");
  }
  for (const documentPath of policy.screenshot_documents) {
    if (!paths.has(documentPath)) {
      fail("screenshot inventory selects a non-rendered document");
    }
  }
  return renderDocuments;
}

async function inspectPage(page, maxTableColumns) {
  return page.evaluate((tableColumnBound) => {
    const issues = [];
    const add = (kind, detail) => {
      if (issues.length < 128) issues.push({ kind, detail });
    };
    const root = document.documentElement;
    const body = document.body;
    if (root.scrollWidth > window.innerWidth + 1 || body.scrollWidth > window.innerWidth + 1) {
      add("page-horizontal-overflow", `${Math.max(root.scrollWidth, body.scrollWidth)}>${window.innerWidth}`);
    }
    const containsHorizontalOverflow = (element) => {
      let current = element.parentElement;
      while (current && current !== body) {
        const style = getComputedStyle(current);
        if (["auto", "scroll", "hidden", "clip"].includes(style.overflowX)) return true;
        current = current.parentElement;
      }
      return false;
    };
    for (const element of document.querySelectorAll("body *")) {
      const style = getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden") continue;
      const rect = element.getBoundingClientRect();
      if (rect.width > 0 && (rect.left < -1 || rect.right > window.innerWidth + 1) && !containsHorizontalOverflow(element)) {
        add("element-outside-viewport", element.tagName.toLowerCase());
      }
      if (["fixed", "absolute"].includes(style.position)) {
        add("detached-positioning", element.tagName.toLowerCase());
      }
      if (["hidden", "clip"].includes(style.overflowY) && element.scrollHeight > element.clientHeight + 1 && element.textContent.trim()) {
        add("clipped-text", element.tagName.toLowerCase());
      }
    }
    for (const image of document.images) {
      if (!image.complete || image.naturalWidth <= 0 || image.naturalHeight <= 0) {
        add("broken-image", image.alt || "missing-alt");
      }
      if (!image.alt.trim()) add("missing-image-alt", "img");
    }
    for (const row of document.querySelectorAll("table tr")) {
      if (row.children.length > tableColumnBound) {
        add("table-column-bound", `${row.children.length}>${tableColumnBound}`);
      }
    }
    return {
      issues,
      metrics: {
        viewport_width: window.innerWidth,
        page_width: Math.max(root.scrollWidth, body.scrollWidth),
        page_height: Math.max(root.scrollHeight, body.scrollHeight),
        image_count: document.images.length,
        heading_count: document.querySelectorAll("h1,h2,h3,h4,h5,h6").length,
      },
    };
  }, maxTableColumns);
}

async function main() {
  const args = parseArguments(process.argv.slice(2));
  if (fs.existsSync(args.output)) fail("output directory must be absent");
  fs.mkdirSync(path.join(args.output, "screenshots"), { recursive: true, mode: 0o700 });

  const catalogPath = path.join(args.root, "docs", "catalog.json");
  const policyPath = path.join(args.root, "docs", "render-preview.json");
  const rendererPath = fs.realpathSync(__filename);
  const catalog = readJson(catalogPath);
  const policy = readJson(policyPath);
  const documents = validateAuthorities(args.root, catalog, policy);
  const checks = [];
  const screenshots = [];
  const documentRecords = [];
  const network = { blocked_requests: 0, placeholder_images: 0 };
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: args.chromium,
      headless: true,
      args: ["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
    });
    for (const record of documents) {
      const source = path.join(args.root, record.path);
      const markdown = fs.readFileSync(source);
      if (markdown.length > policy.limits.max_markdown_bytes) fail("Markdown source exceeds its byte bound");
      const text = markdown.toString("utf8");
      if (/<(?:script|iframe|object|embed)\b|\son[a-z]+\s*=/i.test(text)) {
        fail("Markdown contains active browser content");
      }
      const rendered = inlineRepositoryImages(
        renderGitHubAlerts(marked.parse(text, { async: false, gfm: true })),
        args.root,
        source,
      );
      if (Buffer.byteLength(rendered, "utf8") > policy.limits.max_rendered_html_bytes) {
        fail("rendered Markdown exceeds its byte bound");
      }
      documentRecords.push({ path: record.path, sha256: sha256Bytes(markdown), bytes: markdown.length });
      for (const viewport of policy.viewports) {
        for (const theme of policy.themes) {
          const context = await browser.newContext({
            viewport,
            colorScheme: theme,
            javaScriptEnabled: true,
          });
          const page = await context.newPage();
          await page.route("**/*", async (route) => {
            const request = route.request();
            const url = new URL(request.url());
            if (["about:", "data:"].includes(url.protocol)) return route.continue();
            if (url.protocol === "file:") {
              let local;
              try { local = fileURLToPath(url); } catch { return route.abort("blockedbyclient"); }
              const relative = path.relative(args.root, local);
              if (relative && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)) {
                return route.continue();
              }
              network.blocked_requests += 1;
              return route.abort("blockedbyclient");
            }
            if (["http:", "https:"].includes(url.protocol) && request.resourceType() === "image") {
              network.placeholder_images += 1;
              return route.fulfill({
                status: 200,
                contentType: "image/svg+xml",
                body: '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="20"><rect width="120" height="20" fill="#59636e"/><text x="60" y="14" text-anchor="middle" font-family="sans-serif" font-size="10" fill="white">external badge</text></svg>',
              });
            }
            network.blocked_requests += 1;
            return route.abort("blockedbyclient");
          });
          const baseUrl = pathToFileURL(`${path.dirname(source)}${path.sep}`).href;
          const html = htmlTemplate(rendered, record.title, theme, baseUrl);
          await page.setContent(html, { waitUntil: "load", timeout: 5000 });
          await page.evaluate(() => document.fonts.ready);
          const inspection = await inspectPage(page, policy.limits.max_table_columns);
          checks.push({
            path: record.path,
            width: viewport.width,
            height: viewport.height,
            theme,
            issues: inspection.issues,
            metrics: inspection.metrics,
          });
          if (policy.screenshot_documents.includes(record.path)) {
            if (inspection.metrics.page_height > policy.capture.max_page_height) {
              fail("full-page screenshot exceeds the page-height bound");
            }
            const name = safeOutputName(record.path, viewport.width, theme);
            const target = path.join(args.output, "screenshots", name);
            await page.screenshot({ path: target, type: "png", fullPage: true });
            const bytes = fs.statSync(target).size;
            if (bytes > policy.limits.max_screenshot_bytes) fail("screenshot exceeds its byte bound");
            screenshots.push({
              path: record.path,
              width: viewport.width,
              height: viewport.height,
              theme,
              file: `screenshots/${name}`,
              sha256: sha256File(target),
              bytes,
            });
          }
          await context.close();
        }
      }
    }
    const issues = checks.flatMap((check) => check.issues.map((issue) => ({
      path: check.path, width: check.width, theme: check.theme, ...issue,
    })));
    if (issues.length > policy.limits.max_issues) fail("render issue inventory exceeds its bound");
    const result = {
      schema: SCRIPT_SCHEMA,
      render_contract: RENDER_CONTRACT,
      status: issues.length === 0 ? "passed" : "failed",
      policy_sha256: sha256File(policyPath),
      catalog_sha256: sha256File(catalogPath),
      renderer_sha256: sha256File(rendererPath),
      environment: {
        node: process.version,
        playwright: require("playwright/package.json").version,
        marked: require("marked/package.json").version,
        chromium: browser.version(),
      },
      counts: {
        documents: documentRecords.length,
        checks: checks.length,
        screenshots: screenshots.length,
        issues: issues.length,
      },
      capture: policy.capture,
      network,
      documents: documentRecords,
      checks,
      screenshots,
    };
    const bytes = Buffer.from(`${JSON.stringify(result, null, 2)}\n`, "utf8");
    if (bytes.length > policy.limits.max_result_bytes) fail("render result exceeds its byte bound");
    fs.writeFileSync(path.join(args.output, "result.json"), bytes, { mode: 0o600, flag: "wx" });
    if (result.status !== "passed") process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`documentation preview failed: ${error.message}\n`);
  process.exitCode = 1;
});
