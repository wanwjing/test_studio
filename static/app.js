"use strict";
const $ = id => document.getElementById(id);
const json = value => JSON.stringify(value, null, 2);
const parsed = id => JSON.parse($(id).value);
const action = (kind, fields = {}) => ({schema_version: 1, action: kind, ...fields});
let token, screen, planId, activeRun, reportReady = false;
let rerunPackage = "";
let savingPlan = false;
let planDetailsDirty = false;
let runState = "IDLE", startingRun = false, latestRun, selectedReportId;
let previewInterval = 300;
let reviewedPlan, planRevision = 0, validationTimer, loadingPlan = false;
let pendingEmulators = new Set();

async function bridgeRequest(path, data) {
  const send = (sessionToken = token) => fetch("/api/" + path, data === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Studio-Token": sessionToken},
    body: JSON.stringify(data)
  });
  const originalToken = token;
  const response = await send();
  if (data === undefined || response.status !== 403) return response;

  // A restarted bridge replaces its token while the browser tab stays open.
  const session = await api("session");
  if (!session.token || session.token === originalToken) return response;
  token = session.token;
  adbTunnelPort = session.adb_tunnel_port ?? null;
  showAdbConnection();
  return send(token);
}

async function fetchPreview(serial) {
  const response = await bridgeRequest("emulators/preview", {serial, format: "png"});
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || "Could not capture the emulator screen");
  }
  if (response.headers.get("Content-Type") === "image/png") {
    return {
      blob: await response.blob(),
      width: Number(response.headers.get("X-Studio-Screen-Width")),
      height: Number(response.headers.get("X-Studio-Screen-Height"))
    };
  }
  return response.json();
}

async function api(path, data) {
  const response = await bridgeRequest(path, data);
  const result = await response.json();
  if (!response.ok) {
    const message = response.status === 404 && path === "emulators/action"
      ? "Studio is running an older bridge. Stop it and reopen the desktop shortcut."
      : result.error || "Local bridge request failed";
    throw new Error(message);
  }
  return result;
}

const status = message => { $("status").textContent = message; };
const guarded = callback => async event => {
  try { await callback(event); } catch (error) { status(error.message); }
};
const on = (id, callback, event = "click") => $(id).addEventListener(event, guarded(callback));
const selection = () => ({serial: $("device").value, package: $("package").value,
                          authorized: $("authorized").checked});

function options(id, rows, title) {
  const control = $(id);
  const previous = control.value;
  control.replaceChildren(new Option(title, ""));
  for (const [value, label] of rows) control.add(new Option(label, value));
  if (rows.some(([value]) => value === previous)) control.value = previous;
}

let previewGeneration = 0;
let previewTimer;
let liveFrame;
let previewImageUrl;
let logcatLines = [];
let logcatRequest = 0;
let logcatLevel = "all";
let installingApks = false;

function syncApkControls() {
  $("install-apks").disabled = switchingAdb || installingApks || !$("device").value ||
    pendingEmulators.has($("device").value) || !$("apk-files").files.length;
}

async function installApks() {
  const serial = $("device").value;
  const input = $("apk-files");
  const files = Array.from(input.files);
  const summary = $("install-status");
  const results = $("install-results");
  if (!serial || !files.length || installingApks) return;
  installingApks = true;
  input.disabled = true;
  syncApkControls();
  results.replaceChildren();
  results.hidden = false;
  const failed = [];
  const existingPackages = new Set(Array.from($("package").options, option => option.value));
  try {
    for (const [index, file] of files.entries()) {
      summary.textContent = `Installing ${index + 1} of ${files.length} on ${serial}…`;
      try {
        if (!file.name.toLowerCase().endsWith(".apk")) throw new Error("Select an APK file");
        const response = await fetch("/api/install-apk", {
          method: "POST", body: file,
          headers: {"Content-Type": "application/octet-stream", "X-Studio-Token": token,
                    "X-Studio-Device": serial}
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Installation failed");
        append(results, "li", file.name + " installed");
      } catch (error) {
        failed.push(file);
        append(results, "li", file.name + " · " + error.message);
      }
    }
    const installed = files.length - failed.length;
    summary.textContent = `${installed} of ${files.length} APKs installed on ${serial}.`;
    const remaining = new DataTransfer();
    for (const file of failed) remaining.items.add(file);
    input.files = remaining.files;
    if (installed && $("device").value === serial) {
      await loadPackages(serial);
      if ($("device").value === serial) {
        const added = Array.from($("package").options).filter(option => !existingPackages.has(option.value));
        if (added.length === 1) $("package").value = added[0].value;
        $("package").dispatchEvent(new Event("change"));
      }
    }
  } finally {
    installingApks = false;
    input.disabled = false;
    syncApkControls();
  }
}

function renderLogcat() {
  const term = $("logcat-search").value.trim().toLowerCase();
  const matches = logcatLines.filter(line => {
    if (!line.toLowerCase().includes(term)) return false;
    if (logcatLevel === "all") return true;
    // ADB's threadtime format puts priority after the timestamp, PID, and TID.
    const priority = /^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+([A-Z])\s/.exec(line)?.[1];
    return logcatLevel === "info" ? priority === "I" : ["E", "F", "A"].includes(priority);
  });
  $("logcat-output").textContent = matches.join("\n");
  $("logcat-status").textContent = matches.length + " of " + logcatLines.length + " lines";
}

async function refreshLogcat() {
  const serial = $("device").value;
  if (!serial) return;
  const request = ++logcatRequest;
  const current = () => request === logcatRequest && $("device").value === serial;
  $("refresh-logcat").disabled = true;
  $("logcat-status").textContent = "Reading logs…";
  try {
    const result = await api("logcat", {serial});
    if (!current()) return;
    logcatLines = result.lines;
    renderLogcat();
  } catch (error) {
    if (current()) $("logcat-status").textContent = error.message;
  } finally {
    if (current()) $("refresh-logcat").disabled = false;
  }
}
let liveRefresh;
let emulatorActionPending = false;
let screenGesture;
let gestureFeedbackTimer;

function syncEmulatorControls() {
  const visible = Boolean(liveFrame);
  const ready = visible && $("enable-emulator-control").checked && !emulatorActionPending;
  $("emulator-controls").hidden = !visible;
  $("emulator-back").disabled = !ready;
  $("emulator-home").disabled = !ready;
  $("screen").classList.toggle("interactive", ready || Boolean(screen && screen.image));
}

function stopPreview() {
  screenGesture = null;
  clearTimeout(gestureFeedbackTimer);
  $("screen-gesture").hidden = true;
  previewGeneration++;
  clearTimeout(previewTimer);
  liveFrame = null;
  if (previewImageUrl) URL.revokeObjectURL(previewImageUrl);
  previewImageUrl = null;
  liveRefresh = null;
  syncEmulatorControls();
}

function resetScreen() {
  stopPreview();
  screen = null;
  syncEmulatorControls();
  $("screen").hidden = true;
  $("screen-empty").hidden = false;
  $("screen-empty").textContent = "Launch or select an emulator to see and control its screen here.";
  $("screen").removeAttribute("src");
  $("screen").classList.remove("interactive");
  for (const id of ["resolution", "hierarchy", "analysis"]) $(id).textContent = "";
  options("element", [], "Select an element");
}

function connection(message, state = "") {
  $("connection-status").textContent = message;
  $("connection-status").dataset.state = state;
}

let setupPlan;
let setupDevice;
let setupApp;
let appVersion = "";
let versionRequest = 0;

function syncRunSetup(plan, serial, app, available, busy) {
  const targetApp = available ? app : "";
  if (setupDevice !== serial || setupApp !== targetApp) {
    setupDevice = serial;
    setupApp = targetApp;
    appVersion = "";
    if (targetApp) {
      const request = ++versionRequest;
      api("app-version", {serial, package: targetApp}).then(
        result => showVersion(result.version || "Version unavailable"),
        () => showVersion("Version unavailable")
      );
      function showVersion(version) {
        if (request !== versionRequest || setupDevice !== serial || setupApp !== targetApp) return;
        appVersion = version;
        syncDeviceControls();
      }
    }
  }
  const items = $("run-precondition-items");
  if (setupPlan !== plan || items.dataset.serial !== serial || items.dataset.app !== targetApp) {
    setupPlan = plan;
    items.dataset.serial = serial;
    items.dataset.app = targetApp;
    items.replaceChildren();
    for (const condition of plan?.preconditions || []) {
      const label = append(items, "label", "");
      label.className = "consent";
      const checkbox = append(label, "input", "");
      checkbox.type = "checkbox";
      checkbox.addEventListener("change", syncDeviceControls);
      label.append(" " + condition);
    }
  }
  $("run-objective").hidden = !plan;
  $("run-objective").textContent = plan?.objective || "";
  $("run-preconditions").hidden = !items.childElementCount;
  $("run-preconditions").disabled = !available || busy;
  return [...items.querySelectorAll("input")].every(checkbox => checkbox.checked);
}

function syncPlanDevicePicker(busy) {
  const source = $("device");
  const picker = $("plan-device");
  const sameOptions = picker.options.length === source.options.length &&
    [...source.options].every((option, index) =>
      option.value === picker.options[index].value && option.text === picker.options[index].text);
  if (!sameOptions) picker.replaceChildren(...[...source.options].map(option => option.cloneNode(true)));
  picker.value = source.value;
  picker.disabled = source.disabled || busy;
  $("plan-discover").disabled = $("discover").disabled || busy;
}

function syncDeviceControls() {
  syncApkControls();
  const serial = $("device").value;
  const app = $("package").value;
  const connected = Boolean(serial) && !pendingEmulators.has(serial);
  const authorized = $("authorized").checked;
  const appAvailable = Boolean(app) && !$("package").disabled;
  const ready = connected && appAvailable && authorized && !switchingAdb;
  const required = reviewedPlan?.package || rerunPackage;
  const matching = !required || required === app;
  const busy = Boolean(activeRun) || startingRun || switchingAdb || batchActive || batchStarting;
  syncBatchControls(connected && !$("package").disabled, busy);
  $("use-adb-tunnel").disabled = $("use-local-adb").disabled = busy;
  syncPlanDevicePicker(busy);
  const appReady = connected && appAvailable && matching;
  const batchMode = selectedPlans.size > 0;
  for (const id of ["check-app", "check-plan", "check-authorization", "run-preconditions"]) $(id).hidden = batchMode;
  const requiredInstalled = [...$("package").options].some(option => option.value === required);
  $("plan-app-label").textContent = batchMode ? "App for plans without a package" : "Test application";
  $("plan-app").value = app;
  $("plan-app").disabled = !connected || $("package").disabled || busy;
  $("plan-authorized").checked = authorized;
  $("plan-authorized").disabled = !appReady || busy;
  $("plan-authorized").parentElement.hidden = batchMode;
  $("explore").disabled = !ready || !emulatorSerial(serial) || busy;
  $("save").disabled = !reviewedPlan || loadingPlan || savingPlan;
  $("duplicate-plan").disabled = !reviewedPlan || loadingPlan || savingPlan;
  $("delete-plan").disabled = !planId || savingPlan;
  $("queue-plan").disabled = !planId || !reviewedPlan || loadingPlan || savingPlan || planDetailsDirty;
  const setupConfirmed = syncRunSetup(loadingPlan ? null : reviewedPlan, serial, app, appReady, busy);
  $("run").disabled = !setupConfirmed || !ready || !matching || !reviewedPlan || loadingPlan || busy;
  if (batchMode) $("run-preconditions").hidden = true;
  readiness("device", connected, connected ? serial :
    serial ? "Approve USB debugging in Live Screen." : "Connect an emulator or Android device.");
  readiness("app", appReady, appReady ? app + " · " + (appVersion || "Checking version…") : !connected ? "Connect a device first." :
    required && !requiredInstalled && $("connection-status").dataset.state === "ready" ?
      "Install " + (required === "com.google.android.apps.maps" ? "Google Maps" : required) + " on this device." :
      required ? "Select " + required + " after the app list loads." : "Choose an installed app.");
  readiness("plan", Boolean(reviewedPlan) && !loadingPlan, loadingPlan ? "Opening plan…" : reviewedPlan ? "Validated" : "Load or fix the plan.");
  readiness("authorization", appReady && authorized, appReady && authorized ?
    "Confirmed" : "Confirm after selecting the test app.");
  $("run-device-link").hidden = appReady;
  for (const id of ["launch", "observe", "analyze", "input", "clear", "back"])
    $(id).disabled = !ready;
  $("tap").disabled = !ready || $("element").value === "";
}

function readiness(id, ready, message) {
  const row = $("check-" + id);
  const label = {device: "Device", app: "App", plan: "Plan", authorization: "Authorization"}[id];
  row.dataset.ready = ready;
  row.textContent = label + " · " + message;
}

function selectPlanApp() {
  const required = reviewedPlan?.package || rerunPackage;
  const picker = $("package");
  if (!activeRun && !startingRun && required && picker.value !== required &&
      [...picker.options].some(option => option.value === required)) {
    picker.value = required;
    picker.dispatchEvent(new Event("change"));
  } else {
    syncDeviceControls();
  }
}

const emulatorSerial = serial => serial.startsWith("emulator-") || serial === "127.0.0.1:5555";
const localEmulatorAlias = serial => ({
  "emulator-5554": "127.0.0.1:5555",
  "127.0.0.1:5555": "emulator-5554"
})[serial];

async function displayDevice(serial, refresh = false) {
  const pending = pendingEmulators.has(serial);
  const apps = refresh
    ? (pending ? Promise.resolve() : loadPackages(serial))
    : selectDevice();
  const preview = emulatorSerial(serial) ? previewEmulator(serial) : Promise.resolve(false);
  const [, screenResult] = await Promise.allSettled([apps, preview]);
  if ($("device").value !== serial) return;
  if (pending) {
    connection("Approve USB debugging in Live Screen to connect ADB.");
  }
  if (screenResult.status === "rejected") {
    $("resolution").textContent = "Live preview unavailable · " + screenResult.reason.message;
  }
}

let adbTunnelPort = null;
let switchingAdb = false;

function showAdbConnection() {
  if (adbTunnelPort !== null) $("adb-tunnel-port").value = adbTunnelPort;
  $("adb-connection-status").textContent = adbTunnelPort === null ?
    "Using default ADB." : "Using SSH tunnel on local port " + adbTunnelPort + ".";
}

async function switchAdbConnection(port) {
  if (switchingAdb || activeRun || startingRun || installingApks || endingEmulator || batchActive || batchStarting) return;
  switchingAdb = true;
  syncDeviceControls();
  try {
    const result = await api("adb/connection", {port});
    adbTunnelPort = result.port;
    ++discoveryGeneration;
    pendingEmulators.clear();
    options("device", [], "Select a device");
    $("device").disabled = true;
    stopPreview();
    await selectDevice();
    showAdbConnection();
    await Promise.all([discover(), listAvds()]);
  } catch (error) {
    $("adb-connection-status").textContent = error.message;
  } finally {
    switchingAdb = false;
    syncDeviceControls();
  }
}

let endingEmulator = false;
let discoveryGeneration = 0;

async function discover({connectLocal = true} = {}) {
  if (endingEmulator) return;
  const generation = ++discoveryGeneration;
  $("discover").disabled = true;
  syncPlanDevicePicker(Boolean(activeRun) || startingRun);
  connection("Checking ADB…");
  const choice = $("device");
  try {
    const devices = await api("devices");
    if (generation !== discoveryGeneration) return;
    const previous = choice.value;
    const online = devices.filter(device => device.status === "device");
    const pending = devices.filter(device => device.emulator && device.status === "unauthorized");
    const wasPending = pendingEmulators.has(previous);
    pendingEmulators = new Set(pending.map(device => device.serial));
    let emulators = online.filter(device => emulatorSerial(device.serial));
    let localError = "";
    if (connectLocal && adbTunnelPort === null && !emulators.length && !pending.length && (!previous || emulatorSerial(previous))) {
      try {
        const {serial} = await api("emulators/connect-local", {});
        const detected = online.find(device => device.serial === serial) || {serial, model: "Local emulator"};
        if (!online.includes(detected)) online.push(detected);
        emulators = [detected];
      } catch (error) {
        localError = error.message;
      }
    }
    if (generation !== discoveryGeneration) return;
    const available = [...online, ...pending];
    options("device", available.map(device => [device.serial,
      [device.model, device.serial, pendingEmulators.has(device.serial) ? "ADB approval pending" : ""]
        .filter(Boolean).join(" · ")]), "Select a device");
    choice.disabled = !available.length;
    const alias = localEmulatorAlias(previous);
    if (pendingEmulators.has(previous) && emulators.some(device => device.serial === alias))
      choice.value = alias;
    if (!choice.value && emulators.length === 1) choice.value = emulators[0].serial;
    if (!choice.value && available.length === 1) choice.value = available[0].serial;
    const serial = choice.value;
    if (serial && (serial !== previous || wasPending !== pendingEmulators.has(serial))) {
      await displayDevice(serial);
      if (generation === discoveryGeneration && wasPending && online.some(device => device.serial === serial))
        $("emulator-status").textContent = "Emulator connected.";
    } else if (serial) {
      await displayDevice(serial, true);
    } else {
      if (previous) await selectDevice();
      if (online.length) {
        connection(online.length + " online devices · select a device", "ready");
      } else if (pending.length) {
        connection("Select an emulator to approve USB debugging in Live Screen Viewer.");
      } else if (devices.some(device => device.status === "unauthorized")) {
        connection("Device detected. Accept the USB debugging prompt, then refresh ADB.", "error");
      } else {
        connection(localError || "ADB is running. No devices detected.");
      }
    }
  } catch (error) {
    if (generation !== discoveryGeneration) return;
    if (!choice.value) {
      options("device", [], "Select a device");
      choice.disabled = true;
      await selectDevice();
    }
    connection(error.message, "error");
  } finally {
    if (generation === discoveryGeneration) $("discover").disabled = false;
    syncDeviceControls();
  }
}

async function selectDevice() {
  $("batch-authorized").checked = false;
  resetScreen();
  $("enable-emulator-control").checked = emulatorSerial($("device").value);
  $("emulator-control-status").textContent = "";
  $("authorized").checked = false;
  $("device-info").textContent = "";
  $("package").disabled = true;
  options("package", [], "Select an application");
  options("plan-app", [], "Select an application");
  syncDeviceControls();
  const serial = $("device").value;
  ++logcatRequest;
  logcatLines = [];
  $("logcat-output").textContent = "";
  $("logcat-status").textContent = serial ? "Open Logcat or refresh to read logs." : "Select an ADB device to view its logs.";
  $("refresh-logcat").disabled = !serial || pendingEmulators.has(serial);
  if (!serial || pendingEmulators.has(serial)) return;
  if ($("logcat-panel").open) refreshLogcat();
  await loadPackages(serial);
}

async function loadPackages(serial) {
  try {
    const result = await api("packages", {serial});
    if ($("device").value !== serial) return;
    const previousApp = $("package").value;
    const apps = result.packages.map(name => [name, name]);
    options("package", apps, "Select an application");
    if ($("package").value !== previousApp) $("authorized").checked = false;
    options("plan-app", apps, "Select an application");
    $("package").disabled = !result.packages.length;
    selectPlanApp();
    $("device-info").textContent = [result.device.model, "Android " + result.device.android,
      result.packages.length ? "" : "No test apps installed"].filter(Boolean).join(" · ");
    connection("Connected via ADB · " + serial, "ready");
    syncDeviceControls();
  } catch (error) {
    if ($("device").value === serial) {
      $("package").disabled = true;
      connection(error.message, "error");
      syncDeviceControls();
    }
    throw error;
  }
}

async function observe() {
  const target = selection();
  const result = await api("observe", target);
  if (target.serial !== selection().serial || target.package !== selection().package) return;
  stopPreview();
  screen = result;
  $("screen").hidden = !screen.image;
  $("screen").classList.add("interactive");
  $("screen-empty").hidden = Boolean(screen.image);
  $("screen-empty").textContent = "A screenshot is unavailable for this screen.";
  if (screen.image) $("screen").src = "data:image/png;base64," + screen.image;
  else $("screen").removeAttribute("src");
  $("resolution").textContent = screen.width + " × " + screen.height;
  $("hierarchy").textContent = json(screen.nodes);
  options("element", screen.nodes.flatMap((node, i) => node.package === target.package &&
    (node.resource_id || node.description || node.text) ?
      [[String(i), node.resource_id || node.description || node.text]] : []), "Select an element");
  syncDeviceControls();
}

async function manual(value, refresh = true) {
  await api("action", {...selection(), action: value});
  status("Action executed");
  if (refresh) await observe();
}

let savedPlans = [];
const selectedPlans = new Set();
let batchActive = false;
let batchStarting = false;
const workspacePageSize = 25;
let planPage = 0;
let libraryPage = 0;
let batchPage = 0;
let batchEntries = [];
let batchResultSignature = "";

function filterPlans(query, selectedOnly = false) {
  const normalized = query.trim().toLocaleLowerCase();
  return savedPlans.filter(({id, plan}) => (!selectedOnly || selectedPlans.has(id)) &&
    [plan.name, plan.objective, plan.package].some(value => value?.toLocaleLowerCase().includes(normalized)));
}

function searchablePlans() {
  return filterPlans($("workspace-plan-search").value, $("workspace-selected-only").checked);
}

function workspacePage(prefix, page, count) {
  const last = Math.max(0, Math.ceil(count / workspacePageSize) - 1);
  const current = Math.min(Math.max(0, page), last);
  const start = current * workspacePageSize;
  $(prefix + "-pages").hidden = last === 0;
  $(prefix + "-prev").disabled = current === 0;
  $(prefix + "-next").disabled = current === last;
  $(prefix + "-range").textContent = (start + 1) + "–" + Math.min(start + workspacePageSize, count) + " of " + count;
  return current;
}


function syncBatchControls(connected, busy) {
  const selected = savedPlans.filter(row => selectedPlans.has(row.id));
  const batchMode = Boolean(selected.length);
  $("batch-controls").hidden = !batchMode;
  $("run").hidden = batchMode;
  $("batch-authorized").disabled = busy;
  $("run-batch").disabled = !batchMode || !connected || busy || !$("batch-authorized").checked;
  const matches = searchablePlans();
  $("select-all-plans").disabled = busy || !matches.some(row => !selectedPlans.has(row.id));
  $("select-all-plans").textContent = matches.length === savedPlans.length ? "Select all" : "Select matches";
  $("clear-plans").disabled = busy || !batchMode;
  for (const checkbox of $("workspace-plans").querySelectorAll("input")) checkbox.disabled = busy;
}

function refreshBatchSelection() {
  const selected = savedPlans.filter(row => selectedPlans.has(row.id));
  $("batch-selected-count").textContent = selected.length + " selected · " + savedPlans.length + " total";
  $("batch-authorized").checked = false;
  const requirements = $("batch-preconditions");
  requirements.replaceChildren();
  for (const row of selected) {
    for (const condition of row.plan.preconditions || []) {
      append(requirements, "li", (row.plan.name || row.plan.objective) + " · " + condition);
    }
  }
  $("batch-requirements").hidden = !requirements.childElementCount;
  syncDeviceControls();
}

function batchOutcome(entry) {
  return {WAITING: "Waiting", NOT_EXECUTED: "Not run"}[entry.state] || runLabel(entry);
}

function renderBatchResults() {
  const filter = $("batch-result-filter").value;
  const query = $("batch-result-search").value.trim().toLocaleLowerCase();
  const filtered = batchEntries.filter(entry => {
    const outcome = batchOutcome(entry);
    if (![entry.name, entry.objective].some(value => value?.toLocaleLowerCase().includes(query))) return false;
    return filter === "all" || filter === "issues" && ["Failed", "Blocked", "Inconclusive"].includes(outcome) ||
      filter === "passed" && outcome === "Passed" || filter === "waiting" && ["Waiting", "Not run"].includes(outcome) ||
      filter === "stopped" && outcome === "Stopped";
  });
  batchPage = workspacePage("batch", batchPage, filtered.length);
  $("batch-results-empty").hidden = Boolean(filtered.length);
  const entries = $("batch-results");
  entries.replaceChildren();
  const offset = batchPage * workspacePageSize;
  for (const entry of filtered.slice(offset, offset + workspacePageSize)) {
    const result = append(entries, "li", "");
    const title = append(result, "span", entry.name || entry.objective);
    title.className = "batch-result-name";
    const badge = append(result, "span", batchOutcome(entry));
    badge.className = "batch-badge";
    badge.dataset.outcome = batchOutcome(entry);
    if (entry.id && entry.metrics) {
      const button = append(result, "button", "View report");
      button.type = "button";
      button.setAttribute("aria-label", "View report for " + (entry.name || entry.objective));
      button.addEventListener("click", guarded(() => openWorkspaceReport(entry.id)));
    }
  }
}

async function pollBatch() {
  try {
    const batch = await api("batch");
    batchActive = Boolean(batch.active);
    $("stop-batch").hidden = !batchActive;
    syncDeviceControls();
    if (!batch.entries.length) return runs();
    $("batch-monitor").hidden = false;
    $("batch-feedback").textContent = "";
    batchEntries = batch.entries;
    const outcomes = new Map();
    for (const entry of batchEntries) {
      const outcome = batchOutcome(entry);
      outcomes.set(outcome, (outcomes.get(outcome) || 0) + 1);
    }
    const total = batchEntries.length;
    const finished = total - (outcomes.get("Waiting") || 0) - (outcomes.get("Running") || 0);
    const processed = finished - (outcomes.get("Not run") || 0);
    const status = processed + " of " + total + " plans processed";
    if ($("batch-status").textContent !== status) $("batch-status").textContent = status;
    $("batch-percent").textContent = Math.floor(processed / total * 100) + "%";
    const batchState = batchActive ? "Running" : outcomes.has("Stopped") || outcomes.has("Not run") ? "Stopped" : "Finished";
    $("batch-state").textContent = batchState;
    $("batch-state").dataset.outcome = batchState;
    $("batch-progress").max = total;
    $("batch-progress").value = processed;
    const current = batchEntries.find(entry => entry.state === "RUNNING");
    $("batch-current-card").hidden = !batchActive;
    $("batch-current").textContent = current ? current.name || current.objective : "Preparing next plan…";
    const steps = batch.current;
    const stepProgress = $("batch-step-progress");
    stepProgress.hidden = !current || !steps?.total_steps;
    $("batch-current-steps").textContent = stepProgress.hidden ? ""
      : steps.completed_steps + " of " + steps.total_steps + " steps";
    if (!stepProgress.hidden) {
      stepProgress.max = steps.total_steps;
      stepProgress.value = steps.completed_steps;
    }
    const signature = JSON.stringify(batchEntries);
    if (signature !== batchResultSignature) {
      batchResultSignature = signature;
      const totals = $("batch-totals");
      totals.replaceChildren();
      const counts = [
        ["Passed", outcomes.get("Passed") || 0, "passed"],
        ["Needs review", ["Failed", "Blocked", "Inconclusive"].reduce((sum, key) => sum + (outcomes.get(key) || 0), 0), "issues"],
        [batchActive ? "Waiting" : "Not run", outcomes.get(batchActive ? "Waiting" : "Not run") || 0, "waiting"]
      ];
      if (outcomes.has("Stopped")) counts.push(["Stopped", outcomes.get("Stopped"), "stopped"]);
      for (const [label, count, filter] of counts) {
        const button = append(append(totals, "li", ""), "button", "");
        button.type = "button";
        button.dataset.filter = filter;
        append(button, "strong", String(count));
        append(button, "span", label);
        button.addEventListener("click", () => {
          $("batch-result-details").open = true;
          $("batch-result-search").value = "";
          $("batch-result-filter").value = filter;
          batchPage = 0;
          renderBatchResults();
          $("batch-result-filter").focus();
        });
      }
      renderBatchResults();
    }
    if (batchActive) setTimeout(guarded(pollBatch), 1000);
    else await runs();
  } catch (error) {
    $("batch-feedback").textContent = error.message;
    if (batchActive) setTimeout(guarded(pollBatch), 1500);
  }
}

let runHistory = [];
const workspaceRunPlans = new Map();

async function plans() {
  savedPlans = await api("plans");
  renderWorkspacePlans();
  renderLibraryPlans();
  refreshBatchSelection();
}

function renderWorkspacePlans() {
  const list = $("workspace-plans");
  list.replaceChildren();
  const matches = searchablePlans();
  $("workspace-plans-empty").hidden = Boolean(matches.length);
  $("workspace-plans-empty").textContent = savedPlans.length ? "No matching plans." : "No saved plans yet.";
  planPage = workspacePage("plan", planPage, matches.length);
  const offset = planPage * workspacePageSize;
  for (const row of matches.slice(offset, offset + workspacePageSize)) {
    const item = append(list, "li", "");
    const label = append(item, "label", "");
    label.className = "workspace-plan-choice";
    const checkbox = append(label, "input", "");
    checkbox.type = "checkbox";
    checkbox.checked = selectedPlans.has(row.id);
    label.append(row.plan.name || row.plan.objective);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedPlans.add(row.id);
      else selectedPlans.delete(row.id);
      if ($("workspace-selected-only").checked) renderWorkspacePlans();
      refreshBatchSelection();
    });
  }
}

function renderLibraryPlans() {
  const list = $("library-plans");
  list.replaceChildren();
  const matches = filterPlans($("library-search").value);
  $("library-empty").hidden = Boolean(matches.length);
  $("library-empty").textContent = savedPlans.length ? "No matching plans." : "No saved plans yet.";
  libraryPage = workspacePage("library", libraryPage, matches.length);
  const offset = libraryPage * workspacePageSize;
  for (const row of matches.slice(offset, offset + workspacePageSize)) {
    const button = append(append(list, "li", ""), "button", row.plan.name || row.plan.objective);
    button.type = "button";
    button.dataset.planId = row.id;
    button.setAttribute("aria-pressed", String(row.id === planId));
    button.addEventListener("click", guarded(() => usePlanSource(async () => ({
      plan: await api("plan/validate", {plan: row.plan}), id: row.id, label: "Loaded"
    }))));
  }
}

function markLibraryPlan() {
  for (const button of $("library-plans").querySelectorAll("button"))
    button.setAttribute("aria-pressed", String(button.dataset.planId === planId));
}

function renderWorkspaceRuns() {
  const matching = reviewedPlan ? runHistory.filter(run => run.metrics &&
    ((planId && run.plan_id === planId) || workspaceRunPlans.get(run.id) === reviewedPlan)) : [];
  const list = $("workspace-runs");
  list.replaceChildren();
  $("workspace-runs-empty").hidden = Boolean(matching.length);
  $("workspace-last-result").hidden = !matching.length;
  if (matching.length) $("workspace-last-result").textContent = "Last run · " + runLabel(matching[0]);
  for (const run of matching.slice(0, 8)) {
    const button = append(append(list, "li", ""), "button", runLabel(run));
    button.type = "button";
    button.dataset.reportId = run.id;
    button.setAttribute("aria-pressed", String(selectedReportId === run.id));
    if (run.started) append(button, "small", new Date(run.started * 1000).toLocaleString());
    button.addEventListener("click", guarded(() => openWorkspaceReport(run.id)));
  }
}

let historyPage = 0;

async function runs() {
  runHistory = await api("runs");
  renderWorkspaceRuns();
  renderRunHistory();
  return runHistory;
}

function renderRunHistory() {
  const query = $("history-search").value.trim().toLocaleLowerCase();
  const verdict = $("history-verdict").value;
  const matches = runHistory.filter(run => {
    if (verdict === "in-progress" && terminalStates.has(run.state)) return false;
    if (verdict !== "all" && verdict !== "in-progress" && runLabel(run) !== verdict) return false;
    return !query || [run.name, run.objective, run.package, run.app_version, run.device, run.serial, run.id]
      .some(value => value?.toLocaleLowerCase().includes(query));
  });
  $("history-count").textContent = matches.length === runHistory.length
    ? matches.length + (matches.length === 1 ? " run" : " runs")
    : matches.length + " of " + runHistory.length + " runs";
  historyPage = workspacePage("history", historyPage, matches.length);
  const target = $("run-history-rows");
  target.replaceChildren();
  $("run-history-empty").hidden = Boolean(matches.length);
  $("run-history-empty").textContent = runHistory.length ? "No runs match your search." : "No test runs yet.";
  const offset = historyPage * workspacePageSize;
  for (const run of matches.slice(offset, offset + workspacePageSize)) {
    const row = append(target, "li", "");
    const button = append(row, "button", "");
    const heading = append(button, "span", "");
    heading.className = "history-card-heading";
    append(heading, "strong", run.name || run.objective);
    const outcome = runLabel(run);
    const badge = append(heading, "span", outcome);
    badge.className = "batch-badge";
    badge.dataset.outcome = outcome;
    button.type = "button";
    button.dataset.reportId = run.id;
    button.setAttribute("aria-pressed", String(selectedReportId === run.id));
    button.disabled = !run.metrics;
    button.addEventListener("click", guarded(() => renderReport(run.id)));
    if (run.started) append(button, "small", reportDate(run.started));
    if (run.metrics) {
      const summary = [run.metrics.passed + " / " + run.metrics.total + " steps passed"];
      if (run.metrics.duration_seconds != null) summary.push(run.metrics.duration_seconds + " s");
      append(button, "small", summary.join(" · "));
    }
    const app = [run.package, run.app_version && "v" + run.app_version].filter(Boolean);
    if (app.length) append(button, "small", app.join(" · "));
    if (run.device || run.serial) append(button, "small", run.device || run.serial);
    if (run.issue && Number.isInteger(run.issue.step)) {
      const issue = append(button, "small", (stepLabels[run.issue.status] || run.issue.status) + " at step " + (run.issue.step + 1));
      issue.className = "history-card-issue";
    }
  }
}

const terminalStates = new Set(["COMPLETED", "FAILED", "STOPPED"]);
const stepLabels = {
  NOT_EXECUTED: "Waiting", PASSED: "Passed", FAILED: "Failed",
  BLOCKED: "Blocked", INCONCLUSIVE: "Inconclusive"
};

function runLabel(run) {
  if (run.state === "FAILED" && run.metrics?.blocked) return "Blocked";
  if (run.state === "FAILED" && !run.metrics?.failed && run.metrics?.inconclusive) return "Inconclusive";
  return {IDLE: "Idle", COMPLETED: "Passed", FAILED: "Failed", STOPPED: "Stopped",
    PAUSED: "Paused"}[run.state] || "Running";
}

function updateWorkflow(run) {
  runState = run?.state || "IDLE";
  const finished = terminalStates.has(runState);
  const active = Boolean(activeRun) || startingRun;
  const badge = $("run-stage");
  badge.textContent = startingRun ? "Starting" : runLabel(run || {state: "IDLE"});
  $("console").dataset.stage = finished ? (runState === "COMPLETED" ? "done" : "attention") :
    active ? (runState === "PAUSED" ? "attention" : "running") : "idle";
  for (const button of document.querySelectorAll("[data-control]")) {
    const command = button.dataset.control;
    button.disabled = !activeRun || finished ||
      (command === "pause" && runState === "PAUSED") ||
      (command === "resume" && runState !== "PAUSED");
  }
  const done = finished && Boolean(run?.metrics);
  if (done) latestRun = run.id;
  $("run-monitor").hidden = !active && !finished;
  $("run-controls").hidden = !active || finished;
  $("completion").hidden = !done;
  $("completion-summary").textContent = done ?
    `${run.metrics.passed} of ${run.metrics.total} steps passed. ${run.metrics.duration_seconds} s elapsed.` : "";
  const issue = done && run.results.find(row => ["FAILED", "BLOCKED", "INCONCLUSIVE"].includes(row.status));
  $("completion-issue").textContent = issue ?
    `Step ${issue.step + 1} · ${stepLabels[issue.status]}${issue.error ? " · " + issue.error : ""}` :
    done ? run.error || "" : "";
  $("view-latest").disabled = $("print-latest").disabled = !done;
}

function actionDescription(step) {
  const action = step.action;
  if (!action) return step.instruction;
  const label = {launch_app: "Open the app", inspect_screen: "Inspect the screen",
    press_back: "Press Back", input_text: "Enter text", long_press: "Long-press",
    wait_for_element: "Wait for"}[action.action] || action.action.replaceAll("_", " ");
  const detail = action.target?.value ?? action.text ?? action.direction ??
    (action.point ? action.point.join(", ") : action.start && action.end ?
      action.start.join(", ") + " to " + action.end.join(", ") : "");
  const title = label[0].toUpperCase() + label.slice(1);
  return detail ? title + (action.direction ? " " + detail : " “" + detail + "”") : title;
}

function assertionDescription(assertion) {
  const target = assertion.target ? "“" + assertion.target.value + "”" : "";
  return {
    element_visible: target + " appears",
    element_not_visible: target + " is absent",
    text_equals: target + " reads “" + assertion.value + "”",
    element_enabled: target + " is enabled",
    element_disabled: target + " is disabled",
    screen_transition: "The screen changes",
    application_running: "The app is in the foreground",
    application_not_foreground: "The app leaves the foreground",
    expected_ui_state: target + " appears"
  }[assertion.type];
}

function showPlanPreview(plan, label) {
  loadingPlan = false;
  reviewedPlan = plan;
  $("plan-preview").hidden = false;
  $("plan-preview-heading").textContent = plan.name || plan.objective;
  $("plan-name").value = plan.name || "";
  $("plan-objective").value = plan.objective;
  $("plan-package").value = plan.package || "";
  const required = plan.package || rerunPackage;
  $("plan-target").textContent = required ? "Required app · " + required : "Uses the selected app";
  $("plan-notes").replaceChildren();
  for (const [field, title] of [["preconditions", "Before you run"], ["assumptions", "Assumptions"]]) {
    if (!plan[field]?.length) continue;
    append($("plan-notes"), "h4", title);
    const list = append($("plan-notes"), "ul", "");
    for (const note of plan[field]) append(list, "li", note);
  }
  $("plan-steps").replaceChildren();
  for (const step of plan.steps) {
    const item = append($("plan-steps"), "li", actionDescription(step));
    const checks = append(item, "ul", "");
    for (const assertion of step.assertions) append(checks, "li", assertionDescription(assertion));
  }
  $("tests").dataset.stage = "ready";
  $("plan-stage").textContent = label;
  $("plan-validation").textContent = `Validated · ${plan.steps.length} ${plan.steps.length === 1 ? "step" : "steps"}`;
  $("plan").removeAttribute("aria-invalid");
  selectPlanApp();
  renderWorkspaceRuns();
}

function setPlan(plan, label, id = null, originalPackage = "") {
  rerunPackage = originalPackage;
  ++planRevision;
  clearTimeout(validationTimer);
  planId = id;
  planDetailsDirty = !id;
  $("library-feedback").textContent = "";
  $("workspace-report").hidden = true;
  markLibraryPlan();
  $("plan").value = json(plan);
  $("objective").value = plan.objective;
  showPlanPreview(plan, label);
}

async function validateEditedPlan(revision, label) {
  try {
    let plan;
    try { plan = parsed("plan"); }
    catch { throw new Error("The plan must contain valid JSON."); }
    const validated = await api("plan/validate", {plan});
    if (revision === planRevision) showPlanPreview(validated, label);
  } catch (error) {
    if (revision !== planRevision) return;
    $("tests").dataset.stage = "attention";
    $("plan-stage").textContent = "Needs review";
    $("plan-validation").textContent = error.message;
    $("plan").setAttribute("aria-invalid", "true");
    $("json-tools").open = true;
  }
}

function queuePlanValidation(label = "Edited") {
  planDetailsDirty = true;
  rerunPackage = "";
  const revision = ++planRevision;
  clearTimeout(validationTimer);
  loadingPlan = false;
  reviewedPlan = null;
  renderWorkspaceRuns();
  $("workspace-report").hidden = true;
  $("plan-preview").hidden = true;
  $("tests").dataset.stage = "idle";
  $("plan-stage").textContent = "Checking";
  $("plan-validation").textContent = "Validating plan…";
  $("plan").removeAttribute("aria-invalid");
  syncDeviceControls();
  validationTimer = setTimeout(() => validateEditedPlan(revision, label), 250);
}

function renderStepResults(run, event) {
  const target = $("step-results");
  target.replaceChildren();
  for (const row of run.results) {
    const step = run.plan.steps[row.step];
    const current = !terminalStates.has(run.state) && row.step === event.step && row.status === "NOT_EXECUTED";
    const item = append(target, "li", "");
    item.dataset.result = current ? "RUNNING" : row.status;
    append(item, "span", actionDescription(step));
    append(item, "strong", current ? (run.state === "PAUSED" ? "Paused" : "Running") : stepLabels[row.status]);
    if (row.error) append(item, "small", row.error);
  }
}

async function poll(id) {
  let run;
  try {
    run = await api("run/" + id);
  } catch (error) {
    $("progress").textContent = "Run status unavailable. Retrying… " + error.message;
    if (activeRun === id) setTimeout(guarded(() => poll(id)), 2000);
    return;
  }
  const completed = run.results.filter(row => row.status !== "NOT_EXECUTED").length;
  const event = run.events[run.events.length - 1] || {};
  updateWorkflow(run);
  renderStepResults(run, event);
  const progress = $("run-progress");
  progress.max = run.results.length || 1;
  if (!run.results.length && !terminalStates.has(run.state)) progress.removeAttribute("value");
  else progress.value = completed;
  $("progress").textContent = [run.plan.objective, runLabel(run),
    `${completed} of ${run.results.length} steps finished`, run.error].filter(Boolean).join(" · ");
  $("events").textContent = run.events.map(({time, state, ...other}) =>
    new Date(time * 1000).toLocaleTimeString() + "  " + state + "  " + JSON.stringify(other)).join("\n") +
    "\n" + run.results.map(row => "Step " + (row.step + 1) + "  " + row.status +
      (row.error ? "  " + row.error : "")).join("\n");
  if (terminalStates.has(run.state)) {
    activeRun = null;
    syncDeviceControls();
    if (run.generated_plan_id) {
      setPlan(run.plan, "Generated", run.generated_plan_id);
      await plans();
      status(run.state === "COMPLETED" ? "Test case saved. Replay passed." :
        "Test case saved. Open the run report to review replay results.");
    }
    await runs();
  } else {
    setTimeout(guarded(() => poll(id)), 800);
  }
}

function append(parent, tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.append(node);
  return node;
}

function reportBadge(parent, outcome) {
  const badge = append(parent, "span", outcome);
  badge.className = "report-badge";
  badge.dataset.outcome = outcome;
}

function reportFields(parent, className, entries) {
  const fields = append(parent, "dl", "");
  fields.className = className;
  for (const [label, value] of entries) {
    if (value === undefined || value === null || value === "") continue;
    const item = append(fields, "div", "");
    append(item, "dt", label);
    append(item, "dd", String(value));
  }
}

function reportDate(timestamp) {
  return typeof timestamp === "number" ? new Date(timestamp * 1000).toLocaleString() : "";
}

let reportRun = null;

function appendAssertionResults(parent, step, row) {
  if (!step?.assertions?.length) return;
  const checks = append(parent, "ul", "");
  checks.className = "report-checks";
  step.assertions.forEach((assertion, index) => {
    const outcome = row.assertions?.[index];
    const status = outcome === true ? "Passed" : outcome === false ? "Failed" :
      outcome === null ? "Inconclusive" : "Not checked";
    const check = append(checks, "li", "");
    append(check, "span", assertionDescription(assertion));
    reportBadge(check, status);
  });
}

async function appendStepEvidence(parent, run, row) {
  if (!row.evidence || !run.report_screenshots) return;
  try {
    const data = await api("evidence/" + row.evidence);
    if (!data) throw new Error("Missing screenshot");
    const timing = row.evidence_timing === "before" ? "before" : "after";
    const figure = append(parent, "figure", "");
    figure.className = "report-evidence";
    const caption = "Screenshot " + timing + " step " + (row.step + 1);
    const image = append(figure, "img", "");
    image.alt = caption;
    image.src = "data:image/png;base64," + data;
    append(figure, "figcaption", caption);
  } catch {
    append(parent, "p", "Screenshot unavailable.");
  }
}

async function renderReport(id = selectedReportId) {
  if (!id) throw new Error("Select a test run");
  selectedReportId = id;
  reportReady = false;
  reportRun = null;
  $("rerun-plan").disabled = $("download").disabled = $("print").disabled = true;
  for (const button of document.querySelectorAll("#run-history-rows button, #workspace-runs button"))
    button.setAttribute("aria-pressed", String(button.dataset.reportId === id));
  $("report-content").textContent = "Loading report…";
  const run = await api("run/" + id);
  if (!run.metrics) throw new Error("Wait for the run to finish");
  if (selectedReportId !== id) return false;
  const target = document.createDocumentFragment();
  const metrics = run.metrics;
  const verdict = runLabel(run);
  const intro = append(target, "header", "");
  intro.className = "report-hero";
  const heading = append(intro, "div", "");
  heading.className = "report-heading";
  append(heading, "h2", run.plan.name || run.plan.objective);
  reportBadge(heading, verdict);
  if (run.plan.name) append(intro, "p", run.plan.objective);
  const time = [reportDate(run.started), metrics.duration_seconds == null ? "" : metrics.duration_seconds + " s"]
    .filter(Boolean).join(" · ");
  if (time) append(intro, "p", time).className = "report-meta";
  reportFields(intro, "report-metrics", [
    ["Steps passed", metrics.passed + " / " + metrics.total],
    ["Assertions passed", metrics.assertions_passed + " / " + (metrics.assertions_checked ?? 0)],
    ["Failed or blocked", (metrics.failed || 0) + (metrics.blocked || 0)],
    ["Inconclusive", metrics.inconclusive ?? 0],
    ["Not run", metrics.not_executed ?? 0]
  ]);

  const issue = run.results.find(row => ["FAILED", "BLOCKED", "INCONCLUSIVE"].includes(row.status));
  if (issue || run.error) {
    const summary = append(target, "section", "");
    summary.className = "report-issue";
    const step = issue && run.plan.steps[issue.step];
    append(summary, "h3", issue ? (stepLabels[issue.status] || issue.status) + " at step " + (issue.step + 1) : "Run error");
    if (step) append(summary, "p", actionDescription(step));
    const explanation = issue?.error || run.error || {
      FAILED: "An expected result did not match the observed screen.",
      BLOCKED: "Studio could not complete this action.",
      INCONCLUSIVE: "Studio could not verify one or more expected results."
    }[issue?.status];
    if (explanation) append(summary, "p", explanation);
    if (issue) {
      appendAssertionResults(summary, step, issue);
      await appendStepEvidence(summary, run, issue);
    }
  }

  const environment = append(target, "section", "");
  environment.className = "report-section";
  append(environment, "h3", "Test environment");
  reportFields(environment, "report-fields", [
    ["App package", run.package],
    ["App version", run.app_version],
    ["Device", run.device?.model],
    ["Android", run.device?.android],
    ["Device serial", run.serial],
    ["Execution", run.execution_mode === "plan" ? "Recorded actions" : run.model?.model],
    ["Model provider", run.model?.provider],
    ["Run ID", run.id]
  ]);
  for (const [field, label] of [
    ["preconditions", run.preconditions_confirmed ? "Tester-confirmed starting conditions" : "Plan starting conditions"],
    ["assumptions", "Plan assumptions"]
  ]) {
    if (!run.plan[field]?.length) continue;
    append(environment, "h4", label);
    const list = append(environment, "ul", "");
    for (const item of run.plan[field]) append(list, "li", item);
  }

  const execution = append(target, "section", "");
  execution.className = "report-section";
  append(execution, "h3", "Execution details");
  reportFields(execution, "report-fields", [
    ["Actions attempted", metrics.actions],
    ["Distinct screens observed", metrics.unique_screens],
    ["Verified recoveries", metrics.recoveries_verified]
  ]);
  const results = append(execution, "ol", "");
  results.className = "report-steps";
  for (const row of run.results) {
    const step = run.plan.steps[row.step];
    const item = append(results, "li", "");
    const line = append(item, "div", "");
    line.className = "report-heading";
    append(line, "h4", "Step " + (row.step + 1) + (step ? " · " + actionDescription(step) : ""));
    reportBadge(line, row.status === "NOT_EXECUTED" ? "Not run" : stepLabels[row.status] || row.status);
    if (row.error) append(item, "p", row.error);
    appendAssertionResults(item, step, row);
    if (row !== issue) await appendStepEvidence(item, run, row);
    else if (row.evidence && run.report_screenshots) append(item, "p", "Screenshot shown in the issue summary.");
  }
  if (run.improvements?.length) {
    append(execution, "h4", "Selector recoveries");
    const improvements = append(execution, "ul", "");
    for (const entry of run.improvements)
      append(improvements, "li", entry.original.strategy + " → " + entry.target.strategy +
        (entry.verified ? " · verified" : " · unverified"));
  }
  if (run.analysis || run.recommendations?.length) {
    append(target, "h3", "Model analysis");
    if (run.analysis) append(target, "p", run.analysis);
    if (run.recommendations?.length) {
      const recommendations = append(target, "ul", "");
      for (const text of run.recommendations) append(recommendations, "li", text);
    }
  }
  if (selectedReportId !== id) return false;
  $("report-content").replaceChildren(target);
  await Promise.all([...$("report-content").querySelectorAll("img")].map(image => image.decode().catch(() => {})));
  if (selectedReportId !== id) return false;
  reportReady = true;
  reportRun = run;
  $("rerun-plan").disabled = !run.plan.steps.length;
  $("download").disabled = $("print").disabled = false;
  return true;
}

let emulatorLaunchGeneration = 0;

function syncEmulatorButtons() {
  const empty = !$("avd").value;
  $("start-avd").disabled = empty || endingEmulator;
  $("end-avd").disabled = empty || endingEmulator;
}

async function listAvds() {
  try {
    const names = await api("emulators");
    options("avd", names.map(name => [name, name]), "Select an emulator");
    if (!$("avd").value && names.length) $("avd").value = names[0];
    $("avd").disabled = !names.length;
    syncEmulatorButtons();
    $("emulator-status").textContent = names.length ? "" : "No Android Virtual Devices found.";
  } catch (error) {
    $("emulator-status").textContent = error.message;
  }
}

async function previewEmulator(serial = $("device").value) {
  resetScreen();
  const generation = previewGeneration;
  const current = () => generation === previewGeneration && $("device").value === serial;

  async function frame() {
    const image = await fetchPreview(serial);
    if (!current()) return false;
    const previousUrl = previewImageUrl;
    previewImageUrl = image.blob ? URL.createObjectURL(image.blob) : null;
    $("screen").src = previewImageUrl || "data:image/png;base64," + image.image;
    if (previousUrl) URL.revokeObjectURL(previousUrl);
    $("screen").hidden = false;
    $("screen-empty").hidden = true;
    liveFrame = {serial, width: image.width, height: image.height, updated: Date.now()};
    $("resolution").textContent = image.width + " × " + image.height + " · Live emulator";
    syncEmulatorControls();
    return true;
  }

  let refreshing = false;
  let nextAuthorizationCheck = 0;
  async function refresh() {
    if (!current() || refreshing) return;
    refreshing = true;
    const started = performance.now();
    let delay = previewInterval;
    try {
      if (!document.hidden && !$("devices").hidden) {
        if (!(await frame())) return;
        if (pendingEmulators.has(serial) && Date.now() >= nextAuthorizationCheck) {
          nextAuthorizationCheck = Date.now() + 2000;
          const devices = await api("devices");
          const alias = localEmulatorAlias(serial);
          if (current() && devices.some(device => device.status === "device" &&
              (device.serial === serial || device.serial === alias))) await discover();
        }
      }
    } catch (error) {
      delay = 1200;
      if (current()) $("resolution").textContent = "Reconnecting preview… " + error.message;
    } finally {
      refreshing = false;
    }
    if (current() && !$("devices").hidden)
      previewTimer = setTimeout(refresh, Math.max(0, delay - (performance.now() - started)));
  }

  if (!(await frame())) return false;
  liveRefresh = async () => {
    if (!current()) return;
    clearTimeout(previewTimer);
    await refresh();
  };
  syncDeviceControls();
  if (!$("devices").hidden) previewTimer = setTimeout(refresh, previewInterval);
  return true;
}

async function startEmulator() {
  const generation = ++emulatorLaunchGeneration;
  const launch = $("start-avd");
  launch.disabled = true;
  $("avd").disabled = true;
  $("list-avds").disabled = true;
  const notice = $("emulator-status");
  const skipAdbAuth = $("emulator-skip-adb-auth").checked;
  try {
    const {name, reused} = await api("emulators/start", {
      name: $("avd").value, show_window: $("emulator-window").checked,
      skip_adb_auth: skipAdbAuth
    });
    notice.textContent = (reused ? "Connecting to " : "Starting ") + name + "…";
    const deadline = Date.now() + 180000;
    let lastError = "";
    let selectedSerial = "";
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1800));
      if (generation !== emulatorLaunchGeneration) return;
      try {
        const {serial: online, preview_serial: pending, needs_authorization: needsAuthorization} =
          await api("emulators/status");
        if (generation !== emulatorLaunchGeneration) return;
        if (needsAuthorization && !pending) {
          lastError = skipAdbAuth ? "ADB authorization is still pending after starting the emulator." :
            "Accept Allow USB debugging in the emulator window, or end and relaunch your local test emulator with Skip local emulator ADB prompt checked.";
          notice.textContent = lastError;
          continue;
        }
        const serial = online || pending;
        if (!serial) {
          notice.textContent = "Waiting for " + name + " to boot and connect to ADB…";
          continue;
        }
        if (pending) pendingEmulators.add(serial);
        else pendingEmulators.delete(serial);
        if (selectedSerial !== serial) {
          const choice = $("device");
          if (![...choice.options].some(option => option.value === serial))
            choice.add(new Option(name + " · " + serial, serial));
          choice.disabled = false;
          choice.value = serial;
          selectedSerial = serial;
          selectDevice().catch(error => { connection(error.message, "error"); });
        }
        if (await previewEmulator(serial) && generation === emulatorLaunchGeneration) {
          notice.textContent = pending ? "Approve USB debugging in Live Screen Viewer." : name + " is connected.";
          if (pending) connection("Approve USB debugging in Live Screen to connect ADB.");
          return;
        }
        return; // The user selected another device while the screenshot was loading.
      } catch (error) {
        lastError = error.message;
        if (lastError.startsWith("Emulator exited.")) throw error;
        notice.textContent = "Waiting for the emulator preview. " + lastError;
      }
    }
    throw new Error(lastError || "Emulator has not connected yet. Use Refresh ADB to check for it.");
  } catch (error) {
    notice.textContent = error.message;
  } finally {
    $("avd").disabled = false;
    $("list-avds").disabled = false;
    $("start-avd").disabled = !$("avd").value;
  }
}

async function endEmulator() {
  const name = $("avd").value;
  const notice = $("emulator-status");
  const choice = $("device");
  endingEmulator = true;
  ++discoveryGeneration;
  $("discover").disabled = true;
  syncEmulatorButtons();
  notice.textContent = "Stopping " + name + "…";
  try {
    const {serial} = await api("emulators/end", {name});
    ++emulatorLaunchGeneration;
    const stopped = serial || (choice.value === "127.0.0.1:5555" ? choice.value : "");
    const affected = new Set([stopped, localEmulatorAlias(stopped)].filter(Boolean));
    const selectedEnded = affected.has(choice.value);
    if (selectedEnded) {
      choice.value = "";
      connection("Emulator is shutting down.");
      await selectDevice();
    }
    for (const option of [...choice.options]) {
      if (affected.has(option.value)) option.remove();
    }
    choice.disabled = choice.options.length <= 1;

    if (affected.size) {
      const deadline = Date.now() + 10000;
      while ((await api("devices")).some(device => affected.has(device.serial) &&
          (device.status === "device" || device.status === "unauthorized"))) {
        if (Date.now() >= deadline) {
          notice.textContent = name + " is still listed by ADB. Use Refresh ADB to check again.";
          if (selectedEnded && !choice.value) connection("Emulator shutdown is still pending.");
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 350));
      }
    }
    notice.textContent = name + (affected.size ? " stopped." : " shutdown requested.");
    endingEmulator = false;
    await discover({connectLocal: false});
  } catch (error) {
    notice.textContent = error.message;
    if (!$("device").value) connection("No device selected. Use Refresh ADB to check device status.");
  } finally {
    endingEmulator = false;
    $("discover").disabled = false;
    syncEmulatorButtons();
  }
}

on("refresh-logcat", refreshLogcat);
on("apk-files", syncApkControls, "change");
on("install-apks", installApks);
on("logcat-levels", event => {
  const button = event.target.closest("button[data-level]");
  if (!button) return;
  logcatLevel = button.dataset.level;
  for (const tab of $("logcat-levels").querySelectorAll("button"))
    tab.setAttribute("aria-pressed", String(tab === button));
  if (logcatLines.length) renderLogcat();
});
on("logcat-search", () => { if (logcatLines.length) renderLogcat(); }, "input");
on("logcat-panel", () => { if ($("logcat-panel").open) return refreshLogcat(); }, "toggle");
on("list-avds", listAvds);
on("avd", syncEmulatorButtons, "change");
on("start-avd", startEmulator);
on("end-avd", endEmulator);
on("preview-avd", async () => {
  if (endingEmulator) return;
  if (!emulatorSerial($("device").value)) {
    await discover();
    if (!emulatorSerial($("device").value))
      throw new Error("Select a running emulator in Device Explorer.");
  }
  $("enable-emulator-control").checked = true;
  $("emulator-control-status").textContent = "";
  if (liveFrame) syncEmulatorControls();
  else await previewEmulator();
});
on("discover", () => discover());
on("use-adb-tunnel", () => {
  const port = $("adb-tunnel-port").valueAsNumber;
  if (!Number.isInteger(port) || !$("adb-tunnel-port").checkValidity() || port === 5037) {
    $("adb-connection-status").textContent = "Enter a forwarded port from 1024 to 65535 other than 5037.";
    return;
  }
  return switchAdbConnection(port);
});
on("use-local-adb", () => switchAdbConnection(null));
on("device", () => displayDevice($("device").value), "change");
on("plan-discover", () => discover());
on("plan-device", () => {
  $("device").value = $("plan-device").value;
  return displayDevice($("device").value);
}, "change");
on("package", async () => {
  $("batch-authorized").checked = false;
  $("authorized").checked = false;
  resetScreen();
  syncDeviceControls();
  if (emulatorSerial($("device").value)) await previewEmulator();
}, "change");
on("authorized", syncDeviceControls, "change");
on("plan-app", () => {
  $("package").value = $("plan-app").value;
  $("package").dispatchEvent(new Event("change"));
}, "change");
on("plan-authorized", () => {
  $("authorized").checked = $("plan-authorized").checked;
  syncDeviceControls();
}, "change");
on("plan", () => queuePlanValidation(), "input");
on("element", syncDeviceControls, "change");
on("launch", () => manual(action("launch_app"), false));
on("observe", observe);
on("analyze", async () => { $("analysis").textContent = json(await api("analyze", selection())); });
async function emulatorAction(value) {
  const frame = liveFrame;
  if (!frame || frame.serial !== $("device").value || emulatorActionPending) return;
  if (!$("enable-emulator-control").checked) {
    $("emulator-control-status").textContent = "Click Show live emulator to enable taps.";
    return;
  }
  if (["tap", "swipe"].includes(value.action) && Date.now() - frame.updated > 6000) {
    $("emulator-control-status").textContent = "Wait for a fresh emulator frame.";
    return;
  }
  emulatorActionPending = true;
  $("emulator-control-status").textContent = {tap: "Sending tap…", swipe: "Sending swipe…"}[value.action] || "Sending action…";
  syncEmulatorControls();
  try {
    await api("emulators/action", {serial: frame.serial, authorized: true, action: value});
    if (liveFrame && liveFrame.serial === frame.serial) {
      $("emulator-control-status").textContent = {tap: "Tap sent", swipe: "Swipe sent"}[value.action] || "Action sent";
      if (liveRefresh) await liveRefresh();
    }
  } catch (error) {
    if ($("device").value === frame.serial)
      $("emulator-control-status").textContent = error.message;
  } finally {
    emulatorActionPending = false;
    syncEmulatorControls();
  }
}

on("enable-emulator-control", syncEmulatorControls, "change");
on("emulator-back", () => emulatorAction({action: "back"}));
on("emulator-home", () => emulatorAction({action: "home"}));

function screenPoint(event, box, frame) {
  return [
    Math.min(frame.width - 1, Math.max(0, Math.floor((event.clientX - box.left) * frame.width / box.width))),
    Math.min(frame.height - 1, Math.max(0, Math.floor((event.clientY - box.top) * frame.height / box.height)))
  ];
}

function showGesture(event) {
  const {x, y, box} = screenGesture;
  const endX = Math.min(box.right, Math.max(box.left, event.clientX));
  const endY = Math.min(box.bottom, Math.max(box.top, event.clientY));
  const dx = endX - x, dy = endY - y;
  const feedback = $("screen-gesture");
  const stage = feedback.parentElement.getBoundingClientRect();
  feedback.style.left = (x - stage.left) + "px";
  feedback.style.top = (y - stage.top) + "px";
  feedback.style.setProperty("--gesture-length", Math.hypot(dx, dy) + "px");
  feedback.style.setProperty("--gesture-angle", Math.atan2(dy, dx) + "rad");
  feedback.style.setProperty("--gesture-x", dx + "px");
  feedback.style.setProperty("--gesture-y", dy + "px");
  feedback.hidden = false;
}

on("screen", event => {
  if (!liveFrame || event.button !== 0 || !event.isPrimary || emulatorActionPending) return;
  if (!$("enable-emulator-control").checked) {
    $("emulator-control-status").textContent = "Enable emulator controls to tap or swipe.";
    return;
  }
  const box = $("screen").getBoundingClientRect();
  if (!box.width || !box.height) return;
  event.preventDefault();
  $("screen").setPointerCapture(event.pointerId);
  clearTimeout(gestureFeedbackTimer);
  screenGesture = {id: event.pointerId, box, x: event.clientX, y: event.clientY, frame: liveFrame};
  showGesture(event);
}, "pointerdown");

on("screen", event => {
  if (screenGesture?.id === event.pointerId) showGesture(event);
}, "pointermove");

on("screen", async event => {
  if (screenGesture?.id !== event.pointerId) return;
  showGesture(event);
  const {box, x, y, frame} = screenGesture;
  screenGesture = null;
  gestureFeedbackTimer = setTimeout(() => { $("screen-gesture").hidden = true; }, 220);
  if (!liveFrame || frame.serial !== liveFrame.serial ||
      frame.width !== liveFrame.width || frame.height !== liveFrame.height) return;
  const start = screenPoint({clientX: x, clientY: y}, box, frame);
  const size = [frame.width, frame.height];
  const end = screenPoint(event, box, frame);
  const swipe = Math.hypot(event.clientX - x, event.clientY - y) >= 8 &&
    (start[0] !== end[0] || start[1] !== end[1]);
  await emulatorAction(swipe ? {action: "swipe", start, end, size} : {action: "tap", point: start, size});
}, "pointerup");

for (const eventName of ["pointercancel", "lostpointercapture"]) {
  on("screen", event => {
    if (screenGesture?.id !== event.pointerId) return;
    screenGesture = null;
    $("screen-gesture").hidden = true;
  }, eventName);
}

on("screen", async event => {
  if (liveFrame || !screen?.image) return;
  const box = $("screen").getBoundingClientRect();
  if (box.width <= 0 || box.height <= 0) return;
  await manual(action("tap", {point: screenPoint(event, box, screen), fingerprint: screen.image_fingerprint}));
});
on("tap", async () => {
  if (!screen || $("element").value === "") throw new Error("Select a UI element");
  const node = screen.nodes[Number($("element").value)];
  const strategy = ["resource_id", "description", "text"].find(key => node[key] &&
    screen.nodes.filter(other => other.package === node.package && other[key] === node[key]).length === 1);
  if (!strategy) throw new Error("The selected element has no unique selector");
  await manual(action("tap", {target: {strategy, value: node[strategy]}}));
});
on("input", () => manual(action("input_text", {text: $("text").value})));
on("clear", () => manual(action("clear_text")));
on("back", () => manual(action("press_back"), false));
async function usePlanSource(load) {
  const revision = ++planRevision;
  clearTimeout(validationTimer);
  const feedback = $("plan-source-error");
  feedback.hidden = true;
  loadingPlan = true;
  syncDeviceControls();
  try {
    const {plan, label, id} = await load();
    if (revision !== planRevision) return;
    if (label === "Draft" && plan.steps.some(step => !step.assertions.length)) {
      planId = null;
      $("plan").value = json(plan);
      $("json-tools").open = true;
      queuePlanValidation(label);
    } else {
      setPlan(plan, label, id);
    }
  } catch (error) {
    if (revision !== planRevision) return;
    loadingPlan = false;
    syncDeviceControls();
    feedback.textContent = error.message;
    feedback.hidden = false;
    if (!reviewedPlan && $("plan").value) queuePlanValidation();
  }
}

on("maps-template", () => usePlanSource(async () => {
  const response = await fetch("/google-maps-test-plan.json");
  if (!response.ok) throw new Error("Could not load the Google Maps template.");
  return {plan: await api("plan/validate", {plan: await response.json()}), label: "Template"};
}));
on("generate", () => usePlanSource(async () => ({
  plan: await api("plan/generate", {objective: $("objective").value}), label: "Draft"
})));
on("suggest", () => usePlanSource(async () => {
  const objective = $("objective").value;
  const proposal = await api("suggest", {...selection(), objective});
  return {plan: {objective, preconditions: [], assumptions: [],
    steps: [{action: proposal, assertions: []}]}, label: "Draft"};
}));
let importingPlans = false;

async function importPlanFiles(files) {
  if (!files.length || importingPlans) return;
  importingPlans = true;
  $("plan-file").disabled = true;
  $("plan-import-status").textContent = "Importing " + files.length + " files…";
  $("plan-import-error").hidden = true;
  $("import-open-workspace").hidden = true;
  const errors = [];
  let imported = 0;
  try {
    for (const file of files) {
      try {
        if (!/\.json$/i.test(file.name)) throw new Error("Choose a JSON file");
        if (file.size > 500_000) throw new Error("File exceeds 500 KB");
        const uploaded = JSON.parse(await file.text());
        const plan = uploaded?.plan ?? uploaded;
        if (plan && typeof plan === "object" && !Array.isArray(plan) &&
            (plan.name === undefined || typeof plan.name === "string" && !plan.name.trim())) {
          const name = file.name.replace(/\.json$/i, "").trim().slice(0, 150);
          if (name) plan.name = name;
          else delete plan.name;
        }
        const saved = await api("plan/save", {plan});
        selectedPlans.add(saved.id);
        imported++;
      } catch (error) {
        errors.push(file.name + " · " + error.message);
      }
    }
    await plans();
  } catch (error) {
    errors.push(error.message);
  } finally {
    importingPlans = false;
    $("plan-file").disabled = false;
    $("plan-import-status").textContent = imported
      ? imported + " of " + files.length + " plans imported and selected." : "No plans imported.";
    $("import-open-workspace").hidden = !imported;
    $("plan-import-error").hidden = !errors.length;
    $("plan-import-error").textContent = errors.join(" | ");
  }
}

on("plan-file", event => {
  const files = [...event.currentTarget.files];
  event.currentTarget.value = "";
  return importPlanFiles(files);
}, "change");
const planDropZone = $("plan-drop-zone");
planDropZone.addEventListener("dragover", event => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = importingPlans ? "none" : "copy";
  planDropZone.classList.toggle("drag-active", !importingPlans);
});
planDropZone.addEventListener("dragleave", event => {
  if (!planDropZone.contains(event.relatedTarget)) planDropZone.classList.remove("drag-active");
});
planDropZone.addEventListener("drop", guarded(event => {
  event.preventDefault();
  planDropZone.classList.remove("drag-active");
  return importPlanFiles([...event.dataTransfer.files]);
}));
function planFromDetails() {
  const plan = {...reviewedPlan, objective: $("plan-objective").value.trim()};
  const name = $("plan-name").value.trim();
  const app = $("plan-package").value.trim();
  if (name) plan.name = name;
  else delete plan.name;
  if (app) plan.package = app;
  else delete plan.package;
  return plan;
}

for (const field of ["plan-name", "plan-objective", "plan-package"]) {
  on(field, () => {
    if (!reviewedPlan) return;
    ++planRevision;
    planDetailsDirty = true;
    $("plan-stage").textContent = "Unsaved";
    $("plan").value = json(planFromDetails());
    syncDeviceControls();
  }, "input");
}

function newLibraryPlan() {
  setPlan({
    objective: "Verify the app opens",
    steps: [{action: action("launch_app"), assertions: [{type: "application_running"}]}]
  }, "New plan");
  $("plan-name").focus();
}

on("new-plan", newLibraryPlan);
on("save", async () => {
  if ($("save").disabled) return;
  const revision = planRevision;
  savingPlan = true;
  syncDeviceControls();
  try {
    const row = await api("plan/save", {id: planId, plan: planFromDetails()});
    if (revision === planRevision) {
      setPlan(row.plan, "Saved", row.id);
      $("library-feedback").textContent = "Plan saved.";
    }
    await plans();
  } catch (error) {
    if (revision === planRevision) $("library-feedback").textContent = error.message;
  } finally {
    savingPlan = false;
    syncDeviceControls();
  }
});
on("duplicate-plan", () => {
  const copy = planFromDetails();
  copy.name = ((copy.name || copy.objective).slice(0, 143) + " (copy)").trim();
  setPlan(copy, "Unsaved copy");
  $("plan-name").focus();
  $("plan-name").select();
});
on("delete-plan", async () => {
  if ($("delete-plan").disabled) return;
  const id = planId;
  const saved = savedPlans.find(row => row.id === id);
  if (!saved || !window.confirm("Delete “" + (saved.plan.name || saved.plan.objective) +
      "” from Plan Library? Its run reports will still be available.")) return;
  savingPlan = true;
  syncDeviceControls();
  try {
    await api("plan/delete", {id});
    selectedPlans.delete(id);
    if (planId === id) {
      newLibraryPlan();
      $("library-feedback").textContent = "Plan deleted.";
    }
    await plans();
  } catch (error) {
    $("library-feedback").textContent = error.message;
  } finally {
    savingPlan = false;
    syncDeviceControls();
  }
});
on("queue-plan", () => {
  if ($("queue-plan").disabled) return;
  selectedPlans.add(planId);
  renderWorkspacePlans();
  refreshBatchSelection();
  window.location.hash = "execution";
  showPage();
});
async function startRun(path, data) {
  $("workspace-report").hidden = true;
  $("run-readiness").textContent = "";
  startingRun = true;
  syncDeviceControls();
  updateWorkflow();
  $("progress").textContent = "Starting…";
  $("run-progress").value = 0;
  $("step-results").replaceChildren();
  $("events").textContent = "";
  try {
    activeRun = (await api(path, {...selection(), ...data})).id;
    if (path === "start") workspaceRunPlans.set(activeRun, data.plan);
    if (path === "start") {
      for (const checkbox of $("run-precondition-items").querySelectorAll("input"))
        checkbox.checked = false;
    }
  } catch (error) {
    $("run-readiness").textContent = error.message;
    return;
  } finally {
    startingRun = false;
    syncDeviceControls();
    updateWorkflow(activeRun ? {state: "CONNECTING"} : undefined);
  }
  await poll(activeRun);
}
on("history-search", () => { historyPage = 0; renderRunHistory(); }, "input");
on("history-verdict", () => { historyPage = 0; renderRunHistory(); }, "change");
on("history-prev", () => { historyPage--; renderRunHistory(); });
on("history-next", () => { historyPage++; renderRunHistory(); });
on("library-search", () => { libraryPage = 0; renderLibraryPlans(); }, "input");
on("library-prev", () => { libraryPage--; renderLibraryPlans(); });
on("library-next", () => { libraryPage++; renderLibraryPlans(); });
on("workspace-plan-search", () => {
  planPage = 0;
  renderWorkspacePlans();
  syncDeviceControls();
}, "input");
on("workspace-selected-only", () => {
  planPage = 0;
  renderWorkspacePlans();
  syncDeviceControls();
}, "change");
on("plan-prev", () => { planPage--; renderWorkspacePlans(); syncDeviceControls(); });
on("plan-next", () => { planPage++; renderWorkspacePlans(); syncDeviceControls(); });
on("batch-prev", () => { batchPage--; renderBatchResults(); });
on("batch-next", () => { batchPage++; renderBatchResults(); });
on("batch-result-filter", () => { batchPage = 0; renderBatchResults(); }, "change");
on("batch-result-search", () => { batchPage = 0; renderBatchResults(); }, "input");
on("select-all-plans", () => {
  for (const row of searchablePlans()) selectedPlans.add(row.id);
  renderWorkspacePlans();
  refreshBatchSelection();
});
on("clear-plans", () => {
  selectedPlans.clear();
  renderWorkspacePlans();
  refreshBatchSelection();
});
on("batch-authorized", syncDeviceControls, "change");
on("run-batch", async () => {
  if ($("run-batch").disabled) return;
  const planIds = savedPlans.filter(row => selectedPlans.has(row.id)).map(row => row.id);
  batchStarting = true;
  syncDeviceControls();
  try {
    await api("batch/start", {...selection(), authorized: true, plan_ids: planIds,
      preconditions_confirmed: planIds});
    batchActive = true;
    batchPage = 0;
    $("batch-result-filter").value = "all";
    $("batch-result-search").value = "";
    $("batch-result-details").open = false;
    $("batch-authorized").checked = false;
  } catch (error) {
    $("batch-feedback").textContent = error.message;
    return;
  } finally {
    batchStarting = false;
    syncDeviceControls();
  }
  await pollBatch();
});
on("stop-batch", () => api("batch/stop", {}));
on("run", () => {
  if ($("run").disabled) return;
  return startRun("start", {plan: reviewedPlan, plan_id: planId, preconditions_confirmed: true});
});
on("explore", () => {
  window.location.hash = "execution";
  showPage();
  return startRun("explore", {objective: $("objective").value});
});
for (const button of document.querySelectorAll("[data-control]")) {
  button.addEventListener("click", guarded(async () => {
    if (!activeRun) throw new Error("There is no active run");
    await api("control", {id: activeRun, command: button.dataset.control});
  }));
}
function placeReport(inWorkspace) {
  const destination = $(inWorkspace ? "workspace-report" : "report-library-detail");
  const detail = $("report-detail");
  if (detail.parentElement !== destination) destination.append(detail);
}

async function openWorkspaceReport(id = latestRun, print = false) {
  if (!id) return;
  $("workspace-report").hidden = false;
  placeReport(true);
  $("workspace-report-heading").focus();
  const opened = await renderReport(id);
  if (opened && print && !$("execution").hidden) window.print();
}
function returnToSetup() {
  $("workspace-report").hidden = true;
  $("workspace-setup").open = true;
  $("agent-heading").focus();
}
on("close-workspace-report", returnToSetup);
on("view-latest", () => openWorkspaceReport());
on("print-latest", () => openWorkspaceReport(latestRun, true));
on("rerun-plan", () => {
  if (!reportRun?.plan.steps.length || activeRun || startingRun) return;
  const previous = reportRun;
  workspaceRunPlans.set(previous.id, previous.plan);
  setPlan(previous.plan, "Loaded from report", null, previous.package);
  $("authorized").checked = false;
  syncDeviceControls();
  $("run-readiness").textContent = "Review the device, app, and starting conditions before rerunning.";
  const alreadyInWorkspace = !$("execution").hidden;
  window.location.hash = "execution";
  showPage();
  $("workspace-report").hidden = true;
  if (alreadyInWorkspace) returnToSetup();
});
on("download", async () => {
  if (!reportReady) return;
  const page = document.implementation.createHTMLDocument("Android test report");
  page.documentElement.lang = "en";
  const meta = page.createElement("meta");
  meta.charset = "utf-8";
  page.head.prepend(meta);
  const style = page.createElement("style");
  const reportStyles = [...document.querySelector('link[href="/style.css"]').sheet.cssRules]
    .filter(rule => rule.selectorText === ":root" || rule.selectorText?.startsWith("#report-content"))
    .map(rule => rule.cssText).join("\n");
  style.textContent = "body{font:16px system-ui;margin:2rem auto;max-width:62rem;padding:0 1rem;color:#162944}" +
    reportStyles + "@page{size:A4;margin:15mm}li,img,dl div{break-inside:avoid}h2,h3,h4{break-after:avoid}";
  page.head.append(style);
  page.body.append(page.importNode($("report-content"), true));
  const url = URL.createObjectURL(new Blob(["<!doctype html>" + page.documentElement.outerHTML], {type: "text/html"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = "android-report-" + selectedReportId + ".html";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
on("print", () => { if (reportReady) window.print(); });
const settingsGroups = {
  "android-fields": [
    ["adb", "ADB executable path", "text", {id: "adb-path", placeholder: "Use ADB from PATH", wide: true}],
    ["report_screenshots", "Include screenshots in reports", "checkbox"]
  ],
  "limit-fields": [
    ["limits.actions", "Maximum actions", "number", {min: 1, max: 1000}],
    ["limits.retries", "Maximum retries", "number", {min: 0, max: 20}],
    ["limits.recoveries", "Maximum recoveries", "number", {min: 0, max: 50}],
    ["limits.seconds", "Time limit (seconds)", "number", {min: 1, max: 86400}],
    ["limits.repeated", "Maximum repeated actions", "number", {min: 1, max: 50}]
  ],
  "model-fields": [
    ["llm.provider", "Provider", "select", {options: [
      ["mock", "Mock"], ["openai-compatible", "OpenAI-compatible"], ["ollama", "Ollama"]
    ]}],
    ["llm.model", "Model name", "text"],
    ["llm.endpoint_url", "Inference endpoint URL", "url", {wide: true}],
    ["llm.api_key_env", "API key environment variable", "text", {placeholder: "e.g. DEEPSEEK_API_KEY"}],
    ["llm.timeout_seconds", "Request timeout (seconds)", "number", {min: 1, max: 120, step: "any", placeholder: "Default 20"}],
    ["llm.temperature", "Temperature", "number", {min: 0, max: 2, step: "any", placeholder: "Default 0.1"}],
    ["llm.max_output_tokens", "Maximum output tokens", "number", {min: 16, max: 16384, placeholder: "Default 2048"}],
    ["llm.vision", "Enable vision", "checkbox"],
    ["llm.allow_remote", "Allow remote model endpoints", "checkbox"],
    ["llm.allow_remote_images", "Allow screenshots to be sent to remote models", "checkbox", {wide: true}]
  ]
};
const settingsFields = [];
let settingsDraft, activeSettings, settingsBusy = false, rawSettingsEdited = false;

function settingsFeedback(message, error = false, target = "settings-feedback") {
  const feedback = $(target);
  feedback.textContent = message;
  feedback.dataset.error = error;
}

function syncSettingsControls() {
  const unavailable = !settingsDraft || settingsBusy;
  $("settings-controls").disabled = unavailable;
  for (const id of ["save-settings", "save-adb", "test-model"]) $(id).disabled = unavailable || rawSettingsEdited;
  $("load-settings-json").disabled = $("discard-settings-json").disabled = !rawSettingsEdited;
}

function showSettings(draft) {
  settingsDraft = draft;
  rawSettingsEdited = false;
  for (const {group, key, control} of settingsFields) {
    const value = (group ? draft[group] : draft)?.[key];
    if (control.type === "checkbox") control.checked = value === true;
    else control.value = value ?? "";
  }
  $("settings").value = json(draft);
  $("settings").removeAttribute("aria-invalid");
  settingsFeedback("", false, "model-feedback");
  syncSettingsControls();
}

for (const [id, definitions] of Object.entries(settingsGroups)) {
  for (const [path, title, type, options = {}] of definitions) {
    const [prefix, property] = path.split(".");
    const group = property ? prefix : null;
    const key = property || prefix;
    const field = append($(id), "div", "");
    field.className = options.wide ? "settings-field settings-field-wide" : "settings-field";
    const label = append(field, "label", type === "checkbox" ? "" : title);
    const control = append(type === "checkbox" ? label : field, type === "select" ? "select" : "input", "");
    control.id = options.id || "setting-" + path.replace(".", "-");
    label.htmlFor = control.id;
    if (type === "select") {
      for (const [value, text] of options.options) control.add(new Option(text, value));
    } else {
      control.type = type;
      for (const attribute of ["placeholder", "min", "max", "step"]) {
        if (attribute in options) control.setAttribute(attribute, options[attribute]);
      }
    }
    if (type === "checkbox") {
      label.className = "consent";
      label.append(title);
    }
    settingsFields.push({group, key, control});
    control.addEventListener("input", () => {
      if (rawSettingsEdited) {
        settingsFeedback("Load or discard your JSON edits before editing fields.", true);
        const previous = (group ? settingsDraft[group] : settingsDraft)?.[key];
        if (type === "checkbox") control.checked = previous === true;
        else control.value = previous ?? "";
        return;
      }
      if (group && !settingsDraft[group]) settingsDraft[group] = {};
      const target = group ? settingsDraft[group] : settingsDraft;
      if (type === "number" && control.value === "") delete target[key];
      else target[key] = type === "checkbox" ? control.checked :
        type === "number" ? control.valueAsNumber : control.value;
      $("settings").value = json(settingsDraft);
      if (group === "llm") settingsFeedback("", false, "model-feedback");
      settingsFeedback("Unsaved changes.");
    });
  }
}

function readSettingsJson(source) {
  let document;
  try { document = JSON.parse(source); }
  catch { throw new Error("The configuration must contain valid JSON."); }
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  if (!isObject(document)) throw new Error("Choose a JSON configuration object.");
  if ("provider" in document) return {...settingsDraft, llm: document};
  const settings = "settings" in document ? document.settings : document;
  if (!isObject(settings) || !["adb", "llm", "limits", "report_screenshots"].some(key => key in settings) ||
      ["llm", "limits"].some(key => key in settings && !isObject(settings[key]))) {
    throw new Error("Expected a Studio settings object or a model configuration.");
  }
  return {...settingsDraft, ...settings,
    ...(settings.limits && {limits: {...settingsDraft.limits, ...settings.limits}})};
}

async function settingsAction(callback, feedbackId = "settings-feedback") {
  if (settingsBusy || !settingsDraft || rawSettingsEdited) return;
  settingsBusy = true;
  syncSettingsControls();
  try { await callback(); }
  catch (error) { settingsFeedback(error.message, true, feedbackId); }
  finally {
    settingsBusy = false;
    syncSettingsControls();
  }
}

on("settings", () => {
  rawSettingsEdited = true;
  $("settings").removeAttribute("aria-invalid");
  settingsFeedback("Load JSON into fields or discard the JSON edits.");
  syncSettingsControls();
}, "input");
on("load-settings-json", () => {
  try {
    showSettings(readSettingsJson($("settings").value));
    settingsFeedback("JSON loaded. Review the fields before applying.");
  } catch (error) {
    $("settings").setAttribute("aria-invalid", "true");
    settingsFeedback(error.message, true);
  }
});
on("discard-settings-json", () => {
  showSettings(settingsDraft);
  settingsFeedback("JSON edits discarded.");
});
on("config-file", async event => {
  const input = event.currentTarget;
  const [file] = input.files;
  input.value = "";
  if (!file) return;
  if (rawSettingsEdited) {
    settingsFeedback("Load or discard your JSON edits before importing a file.", true);
    return;
  }
  await settingsAction(async () => {
    if (file.size > 100_000) throw new Error("Choose a configuration file under 100 KB.");
    const source = await file.text();
    showSettings(readSettingsJson(source));
    settingsFeedback(file.name + " loaded. Review the fields before applying.");
  });
}, "change");

async function refreshAfterAdbChange() {
  $("device").value = "";
  await selectDevice();
  await discover();
}

on("save-adb", () => settingsAction(async () => {
  const previous = activeSettings.adb;
  const {adb} = await api("adb/settings", {adb: typeof settingsDraft.adb === "string" ? settingsDraft.adb.trim() : settingsDraft.adb});
  activeSettings.adb = adb;
  showSettings({...settingsDraft, adb});
  settingsFeedback("ADB path saved.");
  if (adb !== previous) await refreshAfterAdbChange();
}));
on("test-model", () => settingsAction(async () => {
  settingsFeedback("Testing connection…", false, "model-feedback");
  const result = await api("model/test", {llm: settingsDraft.llm});
  settingsFeedback("Connected to " + result.model, false, "model-feedback");
}, "model-feedback"));
on("settings-form", event => {
  event.preventDefault();
  return settingsAction(async () => {
    settingsFeedback("Applying settings…");
    const previous = activeSettings.adb;
    const draft = {...settingsDraft};
    if (typeof draft.adb === "string") draft.adb = draft.adb.trim();
    const result = await api("settings", draft);
    activeSettings = result.settings;
    showSettings(structuredClone(activeSettings));
    settingsFeedback("Settings applied · " + result.connection.model);
    if (activeSettings.adb !== previous) await refreshAfterAdbChange();
  });
}, "submit");

const pageLinks = [...document.querySelectorAll(".rail nav a")];
const pageAliases = {live: "devices", tests: "execution", console: "execution"};
const compactViewport = window.matchMedia("(max-width: 650px)");
let desktopDrawerOpen = true;
try { desktopDrawerOpen = localStorage.getItem("studio-drawer") !== "closed"; } catch {}

function setDrawer(open) {
  document.body.dataset.drawer = open ? "open" : "closed";
  const toggle = $("drawer-toggle");
  const label = open ? "Hide navigation" : "Show navigation";
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  document.querySelector(".shell").inert = compactViewport.matches && open;
}

function closeMobileDrawer() {
  setDrawer(false);
  $("drawer-toggle").focus();
}

on("drawer-toggle", () => {
  const open = document.body.dataset.drawer === "closed";
  setDrawer(open);
  if (compactViewport.matches) {
    if (open) $("drawer-close").focus();
  } else {
    desktopDrawerOpen = open;
    try { localStorage.setItem("studio-drawer", document.body.dataset.drawer); } catch {}
  }
});
on("drawer-close", closeMobileDrawer);
on("drawer-backdrop", closeMobileDrawer);
for (const link of pageLinks) {
  link.addEventListener("click", () => {
    if (compactViewport.matches) closeMobileDrawer();
  });
}
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && compactViewport.matches && document.body.dataset.drawer === "open")
    closeMobileDrawer();
});
compactViewport.addEventListener("change", () => setDrawer(!compactViewport.matches && desktopDrawerOpen));
setDrawer(!compactViewport.matches && desktopDrawerOpen);

function showPage(focus = false) {
  const requested = window.location.hash.slice(1);
  const route = Object.prototype.hasOwnProperty.call(pageAliases, requested) ? pageAliases[requested] : requested;
  const current = pageLinks.find(link => link.hash === "#" + route) || pageLinks[0];
  for (const link of pageLinks) {
    $(link.hash.slice(1)).hidden = link !== current;
    if (link === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (current.hash === "#reports") {
    $("workspace-report").hidden = true;
    placeReport(false);
  }
  if (current.hash === "#execution" && !$("workspace-report").hidden) placeReport(true);
  $("page-title").textContent = current.textContent;
  document.title = current.textContent + " · Android AI Test Studio";
  if (focus) {
    window.scrollTo(0, 0);
    $("page-title").focus({preventScroll: true});
  }
  if (current.hash === "#devices" && liveRefresh) liveRefresh();
}

document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  $("workspace").focus();
});
window.addEventListener("hashchange", () => showPage(true));
showPage();

guarded(async () => {
  const session = await api("session");
  token = session.token;
  adbTunnelPort = session.adb_tunnel_port ?? null;
  showAdbConnection();
  previewInterval = session.emulator_preview_interval_ms || 300;
  activeSettings = {...session.settings, report_screenshots: session.settings.report_screenshots !== false};
  showSettings(structuredClone(activeSettings));
  if (session.startup_settings) {
    $("startup-json").textContent = json(session.startup_settings);
    $("startup-settings").hidden = false;
  }
  settingsFeedback("Active settings loaded.");
  syncDeviceControls();
  if (!planRevision) {
    $("plan").value = json({objective: "Verify the selected app stays open", preconditions: [],
      assumptions: [], steps: [{action: action("inspect_screen"),
        assertions: [{type: "application_running"}]}]});
    queuePlanValidation("Starter plan");
  }
  status("Local bridge connected");
  activeRun = session.active_run_id;
  syncDeviceControls();
  if (activeRun) {
    updateWorkflow({state: "CONNECTING"});
    void guarded(() => poll(activeRun))();
  }
  await Promise.all([pollBatch(), plans(), discover(), listAvds()]);
})();
