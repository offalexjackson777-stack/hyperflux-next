// SPDX-License-Identifier: GPL-2.0-only

const COMPANION_STATES = new Set(["ready", "driver-pending", "legacy-v2-detected", "bridge-unavailable", "no-receiver"]);
const PRESENCE_STATES = new Set(["active", "sleeping", "unavailable", "unknown"]);
const SUPPORT_STATES = new Set(["unknown", "identified", "profile-qualified", "production-qualified"]);
const VERDICTS = new Set(["not-run", "in-progress", "blocked", "failed", "evidence-ready", "accepted"]);
const STAGE_STATUSES = new Set(["locked", "ready", "running", "awaiting-observation", "passed", "failed", "blocked", "skipped"]);
const RISKS = new Set(["read-only", "lighting-write", "device-lifecycle", "system-lifecycle"]);
const DIGEST = /^[a-f0-9]{64}$/;
const LOCAL_ENDPOINT = /^http:\/\/(127\.0\.0\.1|localhost)(:[0-9]+)?$/;

export function validateQualificationView(value) {
  object(value, "view");
  exact(value, ["schema", "api_version", "view_revision", "generated_at", "companion", "system", "receivers", "plans", "actions"], "view");
  equal(value.schema, "hyperflux-local-qualification-v1", "view.schema");
  equal(value.api_version, 1, "view.api_version");
  integer(value.view_revision, "view.view_revision", 0);
  string(value.generated_at, "view.generated_at");
  companion(value.companion);
  system(value.system);

  array(value.receivers, "view.receivers", 8);
  value.receivers.forEach((item, index) => receiver(item, `view.receivers[${index}]`));
  array(value.plans, "view.plans", 32);
  value.plans.forEach((item, index) => plan(item, `view.plans[${index}]`));
  array(value.actions, "view.actions", 256);
  value.actions.forEach((item, index) => action(item, `view.actions[${index}]`));

  const devices = value.receivers.flatMap((item) => item.devices);
  unique(devices.map((item) => item.device_id), "device IDs");
  unique(value.plans.map((item) => item.plan_id), "plan IDs");
  unique(value.actions.map((item) => item.id), "action IDs");
  const deviceById = new Map(devices.map((item) => [item.device_id, item]));
  const actionById = new Map(value.actions.map((item) => [item.id, item]));
  const stageIds = [];
  for (const item of value.plans) {
    const device = deviceById.get(item.device_id);
    require(device, `plan ${item.plan_id} references an unknown device`);
    if (item.profile_binding || device.profile) {
      require(item.profile_binding && device.profile, `plan ${item.plan_id} profile binding is incomplete`);
      equal(item.profile_binding.id, device.profile.id, `plan ${item.plan_id} profile ID`);
      equal(item.profile_binding.digest, device.profile.digest, `plan ${item.plan_id} profile digest`);
    }
    for (const group of item.groups) {
      for (const stage of group.stages) {
        stageIds.push(stage.stage_id);
        if (stage.action_id !== null) {
          const stageAction = actionById.get(stage.action_id);
          require(stageAction, `stage ${stage.stage_id} references an unknown action`);
          equal(stageAction.risk, stage.risk, `stage ${stage.stage_id} action risk`);
        }
      }
    }
  }
  unique(stageIds, "stage IDs");
  return value;
}

function companion(value) {
  object(value, "view.companion");
  exact(value, ["state", "version", "bridge_protocol", "endpoint", "simulation", "network_upload_executed", "hardware_write_executed"], "view.companion");
  enumeration(value.state, COMPANION_STATES, "view.companion.state");
  string(value.version, "view.companion.version");
  nullableInteger(value.bridge_protocol, "view.companion.bridge_protocol", 1);
  require(typeof value.endpoint === "string" && LOCAL_ENDPOINT.test(value.endpoint), "view.companion.endpoint is not loopback");
  boolean(value.simulation, "view.companion.simulation");
  equal(value.network_upload_executed, false, "view.companion.network_upload_executed");
  boolean(value.hardware_write_executed, "view.companion.hardware_write_executed");
}

function system(value) {
  object(value, "view.system");
  exact(value, ["driver_version", "bridge_version", "profile_catalog_digest"], "view.system");
  nullableString(value.driver_version, "view.system.driver_version");
  nullableString(value.bridge_version, "view.system.bridge_version");
  if (value.profile_catalog_digest !== null) digest(value.profile_catalog_digest, "view.system.profile_catalog_digest");
}

function receiver(value, label) {
  object(value, label);
  exact(value, ["receiver_id", "generation_id", "model_name", "vendor_id", "product_id", "profile", "lifecycle", "devices"], label);
  string(value.receiver_id, `${label}.receiver_id`);
  integer(value.generation_id, `${label}.generation_id`, 1);
  nullableString(value.model_name, `${label}.model_name`);
  nullableInteger(value.vendor_id, `${label}.vendor_id`, 0, 65535);
  nullableInteger(value.product_id, `${label}.product_id`, 0, 65535);
  nullableProfile(value.profile, `${label}.profile`);
  enumeration(value.lifecycle, new Set(["active", "recovering", "unavailable"]), `${label}.lifecycle`);
  array(value.devices, `${label}.devices`, 16);
  value.devices.forEach((item, index) => device(item, `${label}.devices[${index}]`));
}

function device(value, label) {
  object(value, label);
  exact(value, ["device_id", "kind", "model_name", "vendor_id", "product_id", "profile", "presence", "support", "battery", "capabilities"], label);
  string(value.device_id, `${label}.device_id`);
  enumeration(value.kind, new Set(["mouse", "keyboard", "other"]), `${label}.kind`);
  nullableString(value.model_name, `${label}.model_name`);
  nullableInteger(value.vendor_id, `${label}.vendor_id`, 0, 65535);
  integer(value.product_id, `${label}.product_id`, 0, 65535);
  nullableProfile(value.profile, `${label}.profile`);
  enumeration(value.presence, PRESENCE_STATES, `${label}.presence`);
  enumeration(value.support, SUPPORT_STATES, `${label}.support`);
  battery(value.battery, `${label}.battery`);
  array(value.capabilities, `${label}.capabilities`, 128);
  value.capabilities.forEach((item, index) => capability(item, `${label}.capabilities[${index}]`));
}

function battery(value, label) {
  object(value, label);
  exact(value, ["availability", "percentage"], label);
  enumeration(value.availability, new Set(["reported", "stale", "unavailable", "unknown"]), `${label}.availability`);
  nullableInteger(value.percentage, `${label}.percentage`, 0, 100);
  if (value.availability === "reported") require(value.percentage !== null, `${label}.percentage is required when reported`);
}

function capability(value, label) {
  object(value, label);
  exact(value, ["id", "access", "support_level"], label);
  string(value.id, `${label}.id`);
  enumeration(value.access, new Set(["read", "write"]), `${label}.access`);
  string(value.support_level, `${label}.support_level`);
}

function plan(value, label) {
  object(value, label);
  exact(value, ["plan_id", "device_id", "profile_binding", "verdict", "summary", "groups", "evidence"], label);
  string(value.plan_id, `${label}.plan_id`);
  string(value.device_id, `${label}.device_id`);
  nullableProfile(value.profile_binding, `${label}.profile_binding`);
  enumeration(value.verdict, VERDICTS, `${label}.verdict`);
  string(value.summary, `${label}.summary`);
  array(value.groups, `${label}.groups`, 16, 1);
  value.groups.forEach((item, index) => group(item, `${label}.groups[${index}]`));
  evidence(value.evidence, `${label}.evidence`);
}

function group(value, label) {
  object(value, label);
  exact(value, ["group_id", "title", "description", "stages"], label);
  string(value.group_id, `${label}.group_id`);
  string(value.title, `${label}.title`);
  string(value.description, `${label}.description`);
  array(value.stages, `${label}.stages`, 32, 1);
  value.stages.forEach((item, index) => stage(item, `${label}.stages[${index}]`));
}

function stage(value, label) {
  object(value, label);
  exact(value, ["stage_id", "title", "description", "kind", "risk", "status", "capabilities", "instructions", "observations", "action_id", "result"], label);
  string(value.stage_id, `${label}.stage_id`);
  string(value.title, `${label}.title`);
  string(value.description, `${label}.description`);
  enumeration(value.kind, new Set(["automatic", "watched-observation", "lifecycle"]), `${label}.kind`);
  enumeration(value.risk, RISKS, `${label}.risk`);
  enumeration(value.status, STAGE_STATUSES, `${label}.status`);
  stringArray(value.capabilities, `${label}.capabilities`, 128);
  stringArray(value.instructions, `${label}.instructions`, 32);
  array(value.observations, `${label}.observations`, 32);
  value.observations.forEach((item, index) => observation(item, `${label}.observations[${index}]`));
  nullableString(value.action_id, `${label}.action_id`);
  if (value.result !== null) result(value.result, `${label}.result`);
}

function observation(value, label) {
  object(value, label);
  exact(value, ["id", "prompt", "choices"], label);
  string(value.id, `${label}.id`);
  string(value.prompt, `${label}.prompt`);
  array(value.choices, `${label}.choices`, 8, 2);
  value.choices.forEach((item, index) => choice(item, `${label}.choices[${index}]`));
}

function choice(value, label) {
  object(value, label);
  exact(value, ["id", "label", "outcome"], label);
  string(value.id, `${label}.id`);
  string(value.label, `${label}.label`);
  enumeration(value.outcome, new Set(["pass", "fail", "unclear"]), `${label}.outcome`);
}

function result(value, label) {
  object(value, label);
  exact(value, ["summary", "completed_at", "evidence_refs"], label);
  string(value.summary, `${label}.summary`);
  string(value.completed_at, `${label}.completed_at`);
  stringArray(value.evidence_refs, `${label}.evidence_refs`, 128);
}

function evidence(value, label) {
  object(value, label);
  exact(value, ["run_id", "artifact_state", "completed_claims", "missing_claims", "export_action_id"], label);
  nullableString(value.run_id, `${label}.run_id`);
  enumeration(value.artifact_state, new Set(["none", "collecting", "ready", "reviewed"]), `${label}.artifact_state`);
  stringArray(value.completed_claims, `${label}.completed_claims`, 256);
  stringArray(value.missing_claims, `${label}.missing_claims`, 256);
  nullableString(value.export_action_id, `${label}.export_action_id`);
}

function action(value, label) {
  object(value, label);
  exact(value, ["id", "label", "method", "href", "enabled", "risk", "confirmation"], label);
  string(value.id, `${label}.id`);
  string(value.label, `${label}.label`);
  equal(value.method, "POST", `${label}.method`);
  require(typeof value.href === "string" && value.href.startsWith("/v1/qualification/actions/") && !/[?#\\]/.test(value.href), `${label}.href is invalid`);
  boolean(value.enabled, `${label}.enabled`);
  enumeration(value.risk, RISKS, `${label}.risk`);
  if (value.confirmation !== null) {
    object(value.confirmation, `${label}.confirmation`);
    exact(value.confirmation, ["phrase", "summary"], `${label}.confirmation`);
    string(value.confirmation.phrase, `${label}.confirmation.phrase`);
    string(value.confirmation.summary, `${label}.confirmation.summary`);
  }
}

function nullableProfile(value, label) {
  if (value === null) return;
  object(value, label);
  exact(value, ["id", "digest"], label);
  string(value.id, `${label}.id`);
  digest(value.digest, `${label}.digest`);
}

function exact(value, keys, label) {
  const observed = Object.keys(value).sort();
  const expected = [...keys].sort();
  require(observed.length === expected.length && observed.every((item, index) => item === expected[index]), `${label} has missing or unknown fields`);
}

function object(value, label) {
  require(value !== null && typeof value === "object" && !Array.isArray(value), `${label} must be an object`);
}

function array(value, label, maximum, minimum = 0) {
  require(Array.isArray(value) && value.length >= minimum && value.length <= maximum, `${label} has an invalid item count`);
}

function stringArray(value, label, maximum) {
  array(value, label, maximum);
  value.forEach((item, index) => string(item, `${label}[${index}]`));
}

function string(value, label) {
  require(typeof value === "string" && value.length > 0 && value.length <= 1024, `${label} must be a bounded string`);
}

function nullableString(value, label) {
  if (value !== null) string(value, label);
}

function integer(value, label, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  require(Number.isInteger(value) && value >= minimum && value <= maximum, `${label} must be a bounded integer`);
}

function nullableInteger(value, label, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  if (value !== null) integer(value, label, minimum, maximum);
}

function boolean(value, label) {
  require(typeof value === "boolean", `${label} must be boolean`);
}

function enumeration(value, values, label) {
  require(values.has(value), `${label} has an unsupported value`);
}

function digest(value, label) {
  require(typeof value === "string" && DIGEST.test(value), `${label} must be a SHA-256 digest`);
}

function equal(value, expected, label) {
  require(value === expected, `${label} is not supported`);
}

function unique(values, label) {
  require(new Set(values).size === values.length, `${label} are not unique`);
}

function require(condition, message) {
  if (!condition) throw new Error(message);
}
