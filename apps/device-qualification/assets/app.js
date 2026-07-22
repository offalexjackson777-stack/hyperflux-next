// SPDX-License-Identifier: GPL-2.0-only

import { validateQualificationView } from "./contract.js";

const app = document.querySelector("#app");
const notice = document.querySelector("#global-notice");
const connection = document.querySelector("#connection-state");
const refreshButton = document.querySelector("#refresh-view");

const state = {
  view: null,
  selectedDeviceId: null,
  selectedStageId: null,
  busyActionId: null,
  loading: true,
  error: null,
  hasDraft: false,
};

refreshButton.addEventListener("click", () => refreshView(true));

document.addEventListener("click", (event) => {
  const deviceButton = event.target.closest("[data-device-id]");
  if (deviceButton) {
    state.selectedDeviceId = deviceButton.dataset.deviceId;
    state.selectedStageId = null;
    state.hasDraft = false;
    render();
    return;
  }
  const stageButton = event.target.closest("[data-stage-id]");
  if (stageButton) {
    state.selectedStageId = stageButton.dataset.stageId;
    state.hasDraft = false;
    render();
    return;
  }
  const retry = event.target.closest("[data-refresh]");
  if (retry) refreshView(true);
});

document.addEventListener("input", (event) => {
  if (event.target.closest("[data-stage-form]")) state.hasDraft = true;
});

document.addEventListener("change", (event) => {
  if (event.target.closest("[data-stage-form]")) state.hasDraft = true;
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-stage-form]");
  if (!form) return;
  event.preventDefault();
  await invokeStage(form);
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !state.hasDraft && !state.busyActionId) refreshView(false);
});

window.setInterval(() => {
  if (!document.hidden && !state.hasDraft && !state.busyActionId) refreshView(false);
}, 5_000);

async function refreshView(visible) {
  if (state.busyActionId) return;
  if (visible || !state.view) {
    state.loading = true;
    renderConnection("loading", "Refreshing");
    refreshButton.disabled = true;
  }
  try {
    const response = await fetchWithTimeout("/v1/qualification/view", { cache: "no-store" });
    if (!response.ok) throw new Error(`Local companion returned HTTP ${response.status}`);
    const value = validateQualificationView(await response.json());
    state.view = value;
    state.error = null;
    reconcileSelection();
    if (value.companion.state === "legacy-v2-detected") {
      renderConnection("warning", "HyperFlux V2 detected");
    } else {
      renderConnection("ready", value.companion.state === "ready" ? "Connected" : "Needs attention");
    }
  } catch (error) {
    state.error = readableError(error);
    renderConnection("error", "Unavailable");
  } finally {
    state.loading = false;
    refreshButton.disabled = false;
    render();
  }
}

async function invokeStage(form) {
  if (!state.view) return;
  const context = selectedContext();
  const stage = selectedStage(context?.plan);
  const action = actionFor(stage);
  if (!stage || !action || !action.enabled) {
    showNotice("This check is not available in the current receiver state.");
    return;
  }

  const formData = new FormData(form);
  const observations = {};
  for (const prompt of stage.observations) {
    const answer = formData.get(`observation:${prompt.id}`);
    if (typeof answer !== "string") {
      showNotice("Answer every watched observation before recording the check.");
      return;
    }
    observations[prompt.id] = answer;
  }
  const confirmation = action.confirmation ? formData.get("confirmation") : undefined;
  if (action.confirmation && confirmation !== action.confirmation.phrase) {
    showNotice("Enter the exact authorization phrase shown for this check.");
    return;
  }

  state.busyActionId = action.id;
  state.error = null;
  state.hasDraft = false;
  hideNotice();
  render();
  try {
    const response = await fetchWithTimeout(action.href, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        view_revision: state.view.view_revision,
        observations,
        ...(action.confirmation ? { confirmation } : {}),
      }),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Local companion returned HTTP ${response.status}`);
    state.view = validateQualificationView(value);
    state.selectedStageId = null;
    reconcileSelection();
  } catch (error) {
    state.error = readableError(error);
    showNotice(state.error);
  } finally {
    state.busyActionId = null;
    render();
  }
}

async function fetchWithTimeout(path, options) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 6_000);
  try {
    return await fetch(path, {
      credentials: "same-origin",
      ...options,
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}

function reconcileSelection() {
  const contexts = deviceContexts();
  if (!contexts.some(({ device }) => device.device_id === state.selectedDeviceId)) {
    state.selectedDeviceId = contexts[0]?.device.device_id ?? null;
    state.selectedStageId = null;
    state.hasDraft = false;
  }
  const context = selectedContext();
  const stages = allStages(context?.plan);
  if (!stages.some((stage) => stage.stage_id === state.selectedStageId)) {
    state.selectedStageId = currentStage(context?.plan)?.stage_id ?? null;
  }
}

function render() {
  app.setAttribute("aria-busy", String(state.loading));
  if (state.error && !state.view) {
    app.innerHTML = connectionGate();
    return;
  }
  if (!state.view) return;
  if (state.view.companion.state !== "ready") {
    app.innerHTML = systemGate(state.view.companion.state);
    return;
  }
  const contexts = deviceContexts();
  if (!contexts.length) {
    app.innerHTML = emptyInventory();
    return;
  }
  const context = selectedContext() || contexts[0];
  app.innerHTML = `
    ${state.view.companion.simulation ? '<div class="simulation-banner">Simulation data. No support claim may be created.</div>' : ""}
    <div class="console-grid">
      ${renderDeviceRail(contexts)}
      <section class="workspace">
        ${renderDeviceHeader(context)}
        ${context.plan ? renderQualification(context.plan) : renderUnknown(context)}
      </section>
    </div>`;
}

function renderDeviceRail(contexts) {
  return `
    <aside class="device-rail" aria-label="Detected controllers">
      <header class="rail-heading"><span>Detected controllers</span><strong>${contexts.length}</strong></header>
      <div class="device-list">
        ${contexts.map(({ device, plan }) => {
          const progress = stageProgress(plan);
          return `
            <button type="button" class="device-row ${device.device_id === state.selectedDeviceId ? "is-selected" : ""}" data-device-id="${escapeAttribute(device.device_id)}">
              <span class="device-kind" aria-hidden="true">${device.kind === "mouse" ? "M" : device.kind === "keyboard" ? "K" : "D"}</span>
              <span class="device-copy">
                <strong>${escapeHtml(device.model_name || "Unrecognized controller")}</strong>
                <small>${formatVidPid(device.vendor_id, device.product_id)}</small>
                <span class="device-facts">
                  ${statusMark(device.presence, labelize(device.presence))}
                  <span>${batteryLabel(device.battery)}</span>
                </span>
                ${plan ? `<progress max="${progress.total}" value="${progress.complete}" aria-label="${progress.complete} of ${progress.total} checks passed"></progress>` : ""}
              </span>
            </button>`;
        }).join("")}
      </div>
      <footer class="rail-boundary">
        <strong>Installed state only</strong>
        <span>No account, cloud inventory, or sample hardware.</span>
      </footer>
    </aside>`;
}

function renderDeviceHeader({ receiver, device, plan }) {
  const progress = stageProgress(plan);
  const support = supportLabel(plan?.verdict, device.support);
  return `
    <header class="device-header">
      <div class="device-title-row">
        <div>
          <p class="eyebrow">${escapeHtml(labelize(device.kind))} qualification</p>
          <h1>${escapeHtml(device.model_name || "Unrecognized controller")}</h1>
          <p>${formatVidPid(device.vendor_id, device.product_id)} through ${escapeHtml(receiver.model_name || "the active receiver")}</p>
        </div>
        ${statusMark(support.state, support.label)}
      </div>
      <dl class="identity-strip">
        <div><dt>Profile</dt><dd class="mono">${escapeHtml(device.profile?.id || "No reviewed profile")}</dd></div>
        <div><dt>Profile digest</dt><dd class="mono">${device.profile ? escapeHtml(shortDigest(device.profile.digest)) : "Unavailable"}</dd></div>
        <div><dt>Receiver generation</dt><dd>${receiver.generation_id} / ${escapeHtml(receiver.lifecycle)}</dd></div>
        <div><dt>Evidence progress</dt><dd>${plan ? `${progress.complete} of ${progress.total} checks passed` : "No qualification plan"}</dd></div>
      </dl>
    </header>`;
}

function renderQualification(plan) {
  const stage = selectedStage(plan);
  return `
    <div class="qualification-layout">
      <section class="plan" aria-labelledby="plan-heading">
        <header class="section-heading">
          <div><p class="eyebrow">Profile-bound plan</p><h2 id="plan-heading">Required evidence</h2></div>
          <p>${escapeHtml(plan.summary)}</p>
        </header>
        ${plan.groups.map(renderGroup).join("")}
      </section>
      ${renderInspector(stage)}
    </div>
    ${renderEvidence(plan)}`;
}

function renderGroup(group) {
  return `
    <section class="stage-group">
      <header><div><h3>${escapeHtml(group.title)}</h3><p>${escapeHtml(group.description)}</p></div><span>${group.stages.length}</span></header>
      <div class="stage-list">
        ${group.stages.map((stage) => `
          <button type="button" class="stage-row ${stage.stage_id === state.selectedStageId ? "is-selected" : ""}" data-stage-id="${escapeAttribute(stage.stage_id)}">
            <span class="risk-code">${riskCode(stage.risk)}</span>
            <span class="stage-copy"><strong>${escapeHtml(stage.title)}</strong><small>${escapeHtml(stage.description)}</small></span>
            ${statusMark(stage.status, labelize(stage.status))}
          </button>`).join("")}
      </div>
    </section>`;
}

function renderInspector(stage) {
  if (!stage) {
    return '<aside class="inspector inspector-empty"><p>Select a check to inspect its exact scope.</p></aside>';
  }
  const action = actionFor(stage);
  const interactive = action && ["ready", "running", "awaiting-observation"].includes(stage.status);
  return `
    <aside class="inspector" aria-labelledby="inspector-heading">
      <header><div><p class="eyebrow">Selected check</p><h2 id="inspector-heading">${escapeHtml(stage.title)}</h2></div>${statusMark(stage.status, labelize(stage.status))}</header>
      <p class="inspector-summary">${escapeHtml(stage.description)}</p>
      <div class="risk-band" data-risk="${escapeAttribute(stage.risk)}">
        <strong>${riskTitle(stage.risk)}</strong>
        <span>${riskSummary(stage.risk)}</span>
      </div>
      ${stage.capabilities.length ? `<section class="capability-block"><h3>Claims under test</h3><div>${stage.capabilities.map((item) => `<code>${escapeHtml(item)}</code>`).join("")}</div></section>` : ""}
      ${stage.instructions.length ? `<section class="instruction-block"><h3>Procedure</h3><ol>${stage.instructions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol></section>` : ""}
      ${stage.result ? renderResult(stage.result) : ""}
      ${interactive ? renderStageForm(stage, action) : renderStageBoundary(stage, action)}
    </aside>`;
}

function renderStageForm(stage, action) {
  return `
    <form class="stage-form" data-stage-form novalidate>
      ${stage.observations.map((prompt) => `
        <fieldset>
          <legend>${escapeHtml(prompt.prompt)}</legend>
          ${prompt.choices.map((choice) => `
            <label><input type="radio" name="observation:${escapeAttribute(prompt.id)}" value="${escapeAttribute(choice.id)}"><span>${escapeHtml(choice.label)}</span></label>`).join("")}
        </fieldset>`).join("")}
      ${action.confirmation ? `
        <label class="confirmation">
          <strong>Explicit authorization</strong>
          <span>${escapeHtml(action.confirmation.summary)}</span>
          <input type="text" name="confirmation" autocomplete="off" spellcheck="false" placeholder="${escapeAttribute(action.confirmation.phrase)}">
        </label>` : ""}
      <button class="primary-command" type="submit" ${action.enabled && !state.busyActionId ? "" : "disabled"}>
        ${state.busyActionId === action.id ? "Running check..." : escapeHtml(action.label)}
      </button>
    </form>`;
}

function renderStageBoundary(stage, action) {
  if (stage.status === "passed" || stage.status === "failed") return "";
  const explanation = stage.status === "locked"
    ? "Complete the earlier checks first. The companion will unlock this step in sequence."
    : action
      ? "This action is unavailable while the installed state is changing. Refresh after the controller is stable."
      : "No supervised runner is installed for this hardware-changing step. Nothing was sent to the device.";
  return `<div class="stage-boundary"><strong>Not runnable</strong><span>${escapeHtml(explanation)}</span></div>`;
}

function renderResult(result) {
  return `
    <section class="recorded-result">
      <h3>Recorded result</h3>
      <p>${escapeHtml(result.summary)}</p>
      <small>${escapeHtml(result.completed_at)} / ${result.evidence_refs.length} local reference(s)</small>
    </section>`;
}

function renderEvidence(plan) {
  const progress = stageProgress(plan);
  return `
    <footer class="evidence-bar">
      <div><small>Local evidence run</small><strong>${escapeHtml(plan.evidence.run_id || "Created when the first check passes")}</strong></div>
      <div><small>Required checks</small><strong>${progress.complete} / ${progress.total}</strong></div>
      <div><small>Claims recorded</small><strong>${plan.evidence.completed_claims.length}</strong></div>
      <p>Completion creates a local evidence candidate. Maintainer review is the separate support decision.</p>
    </footer>`;
}

function renderUnknown({ device }) {
  return `
    <section class="unknown-profile">
      <p class="eyebrow">Read-only identity</p>
      <h2>This PID has no reviewed local profile</h2>
      <p>The controller remains visible, but HyperFlux will not invent a model, LED map, capability, or writable test plan.</p>
      <dl><div><dt>Observed product ID</dt><dd class="mono">${formatPid(device.product_id)}</dd></div><div><dt>Current support state</dt><dd>${escapeHtml(labelize(device.support))}</dd></div></dl>
    </section>`;
}

function connectionGate() {
  return `
    <section class="system-gate">
      <p class="eyebrow">Local companion unavailable</p>
      <h1>Open the installed qualification console</h1>
      <p>The page cannot read <code>/v1/qualification/view</code> from its own loopback companion.</p>
      <div class="next-actions"><p><strong>Run</strong><code>hyperfluxctl qualification serve</code></p><p><strong>Then</strong>Open the exact local URL printed by the command.</p></div>
      <button class="primary-command" type="button" data-refresh>Retry connection</button>
      <small>No remote fallback or sample-device data was used.</small>
    </section>`;
}

function systemGate(companionState) {
  const content = {
    "driver-pending": ["Activation required", "The installed driver is newer than the active driver", "The console is connected, but qualification cannot begin yet.", "Run hyperfluxctl doctor and follow its one activation action.", "Reopen this console after Doctor reports Ready."],
    "legacy-v2-detected": ["Different generation detected", "This computer is running HyperFlux V2", "This is the HyperFlux Next console. It will not misreport a working V2 installation as a broken Next bridge.", "Keep V2 for normal use, or install a reviewed HyperFlux Next candidate when one is available.", "Launch the console from that same Next installation."],
    "bridge-unavailable": ["Service unavailable", "The HyperFlux Next bridge is not ready", "The console is connected, but qualification cannot begin yet.", "Run the Next installation's Doctor command and follow its one safe action.", "Reopen this console after Doctor reports Ready."],
    "no-receiver": ["Hardware not detected", "Connect a HyperFlux receiver", "The console is ready, but no receiver is available for qualification.", "Connect the receiver and wake its paired controllers.", "This inventory refreshes automatically."],
    ready: ["Inventory empty", "No paired controller is available", "The console is ready, but no controller can be qualified yet.", "Wake or pair a controller through the receiver.", "Use Refresh after the controller produces activity."],
  }[companionState];
  return `
    <section class="system-gate">
      <p class="eyebrow">${content[0]}</p>
      <h1>${content[1]}</h1>
      <p>${content[2]}</p>
      <div class="next-actions"><p><strong>Next</strong>${content[3]}</p><p><strong>Then</strong>${content[4]}</p></div>
      <button class="primary-command" type="button" data-refresh>Refresh local state</button>
      <small>No remote database, upload, or hardware write.</small>
    </section>`;
}

function emptyInventory() {
  return systemGate("ready");
}

function deviceContexts() {
  if (!state.view) return [];
  return state.view.receivers.flatMap((receiver) => receiver.devices.map((device) => ({
    receiver,
    device,
    plan: state.view.plans.find((plan) => plan.device_id === device.device_id) || null,
  })));
}

function selectedContext() {
  return deviceContexts().find(({ device }) => device.device_id === state.selectedDeviceId) || null;
}

function allStages(plan) {
  return plan ? plan.groups.flatMap((group) => group.stages) : [];
}

function selectedStage(plan) {
  const stages = allStages(plan);
  return stages.find((stage) => stage.stage_id === state.selectedStageId) || currentStage(plan);
}

function currentStage(plan) {
  const stages = allStages(plan);
  return stages.find((stage) => ["running", "awaiting-observation"].includes(stage.status))
    || stages.find((stage) => stage.status === "ready")
    || stages.find((stage) => !["passed", "skipped"].includes(stage.status))
    || stages.at(-1)
    || null;
}

function actionFor(stage) {
  if (!state.view || !stage?.action_id) return null;
  return state.view.actions.find((action) => action.id === stage.action_id) || null;
}

function stageProgress(plan) {
  const stages = allStages(plan).filter((stage) => stage.status !== "skipped");
  return { complete: stages.filter((stage) => stage.status === "passed").length, total: stages.length };
}

function statusMark(status, label) {
  return `<span class="status" data-status="${escapeAttribute(status)}"><i aria-hidden="true"></i>${escapeHtml(label)}</span>`;
}

function supportLabel(verdict, support) {
  if (verdict === "accepted") return { state: "accepted", label: "Support accepted" };
  if (verdict === "evidence-ready") return { state: "evidence-ready", label: "Evidence ready" };
  if (verdict === "failed") return { state: "failed", label: "Qualification failed" };
  if (verdict === "blocked") return { state: "blocked", label: "Qualification blocked" };
  if (verdict === "in-progress") return { state: "in-progress", label: "Testing in progress" };
  if (support === "unknown") return { state: "unknown", label: "Unknown profile" };
  return { state: "not-run", label: "Not yet tested" };
}

function batteryLabel(battery) {
  return battery.availability === "reported" && battery.percentage !== null
    ? `${battery.percentage}% battery`
    : `Battery ${labelize(battery.availability)}`;
}

function riskCode(risk) {
  return { "read-only": "R", "lighting-write": "RGB", "device-lifecycle": "HW", "system-lifecycle": "SYS" }[risk];
}

function riskTitle(risk) {
  return { "read-only": "Read-only", "lighting-write": "Visible lighting write", "device-lifecycle": "Physical controller step", "system-lifecycle": "Computer lifecycle step" }[risk];
}

function riskSummary(risk) {
  return {
    "read-only": "No hardware state is changed.",
    "lighting-write": "Only this exact profile-bound route may receive the bounded test frames.",
    "device-lifecycle": "You will be asked to power, move, or reconnect hardware.",
    "system-lifecycle": "Suspend or restart requires a separate exact authorization.",
  }[risk];
}

function renderConnection(mode, label) {
  connection.dataset.state = mode;
  connection.querySelector("span").textContent = label;
}

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = false;
}

function hideNotice() {
  notice.hidden = true;
  notice.textContent = "";
}

function readableError(error) {
  if (error?.name === "AbortError") return "The local companion did not respond within six seconds.";
  return error instanceof Error ? error.message : "The local companion response could not be read.";
}

function labelize(value) {
  return value.replaceAll("-", " ").replace(/^./, (character) => character.toUpperCase());
}

function formatPid(value) {
  return `0x${value.toString(16).toUpperCase().padStart(4, "0")}`;
}

function formatVidPid(vendor, product) {
  return `VID ${vendor === null ? "----" : formatPid(vendor)} / PID ${formatPid(product)}`;
}

function shortDigest(value) {
  return `${value.slice(0, 12)}...${value.slice(-8)}`;
}

function escapeAttribute(value) {
  return escapeHtml(String(value));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

refreshView(true);
