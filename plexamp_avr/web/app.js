"use strict";

const ZONE_NAMES = { z1: "Main zone", z2: "Zone 2", z3: "Zone 3" };
const state = { zone: "z1", status: null, socketOpen: false, inputs: [], inputAliases: {}, draggingVolume: false };
const $ = (id) => document.getElementById(id);

function inputLabel(name) {
  const alias = state.inputAliases[name];
  return alias && alias !== name ? `${name} (${alias})` : name;
}

function currentZone() {
  return (state.status && state.status.zones && state.status.zones[state.zone]) || {};
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

function formatVolume(volume) {
  if (volume === null || volume === undefined) return "–";
  const db = volume - 80;
  return `${volume} (${db > 0 ? "+" : ""}${db} dB)`;
}

function renderInputs(zone) {
  const select = $("input");
  const options = [...state.inputs];
  if (state.zone !== "z1" && !options.includes("SOURCE")) options.unshift("SOURCE");
  if (zone.input && !options.includes(zone.input)) options.push(zone.input);
  if (!zone.input) options.unshift("");
  const current = [...select.options].map((option) => `${option.value}=${option.textContent}`);
  const desired = options.map((name) => `${name}=${name ? inputLabel(name) : "—"}`);
  if (current.join("\n") !== desired.join("\n")) {
    select.replaceChildren(...options.map((name) => new Option(name ? inputLabel(name) : "—", name)));
  }
  select.value = zone.input || "";
}

function renderInputShortcuts(zone, avrConnected) {
  const shortcuts = Object.entries(state.inputAliases)
    .filter(([inputName, alias]) => alias && inputName)
    .slice(0, 4);
  const container = $("input-shortcuts");
  container.hidden = shortcuts.length === 0;
  container.replaceChildren(...shortcuts.map(([inputName, alias]) => {
    const button = document.createElement("button");
    const label = document.createElement("span");
    button.type = "button";
    button.className = "input-shortcut";
    button.dataset.input = inputName;
    button.title = alias;
    button.disabled = !avrConnected;
    button.setAttribute("aria-label", alias);
    button.setAttribute("aria-pressed", String(zone.input === inputName));
    label.className = "input-shortcut-label";
    label.textContent = alias;
    button.append(label);
    return button;
  }));
}

function render() {
  const avrConnected = Boolean(state.socketOpen && state.status && state.status.connected);
  const connection = $("connection");
  connection.textContent = !state.socketOpen ? "Server offline" : avrConnected ? "AVR connected" : "AVR disconnected";
  connection.classList.toggle("ok", avrConnected);

  const zone = currentZone();
  $("zone-name").textContent = ZONE_NAMES[state.zone];
  const on = zone.power === "on";
  $("power").textContent = on ? "On" : zone.power === "off" ? "Off" : "–";
  $("power").setAttribute("aria-pressed", String(on));
  renderInputs(zone);
  renderInputShortcuts(zone, avrConnected);

  const slider = $("volume");
  slider.step = state.zone === "z1" ? "0.5" : "1";
  if (!state.draggingVolume && zone.volume !== null && zone.volume !== undefined) slider.value = zone.volume;
  if (!state.draggingVolume) $("volume-value").textContent = formatVolume(zone.volume);
  $("mute").setAttribute("aria-pressed", String(zone.muted === true));
  $("mute").textContent = zone.muted ? "Muted" : "Mute";

  for (const id of ["power", "input", "volume", "volume-down", "volume-up", "mute"]) {
    $(id).disabled = !avrConnected;
  }
}

function selectZone(zone) {
  state.zone = zone;
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.setAttribute("aria-selected", String(tab.dataset.zone === zone));
  }
  $("zone-panel").hidden = false;
  $("webhooks-panel").hidden = true;
  $("zone-panel").setAttribute("aria-labelledby", `tab-${zone}`);
  showError("");
  render();
}

function selectWebhooks() {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === "webhooks"));
  }
  $("zone-panel").hidden = true;
  $("webhooks-panel").hidden = false;
  loadWebhooks();
}

// Webhooks ------------------------------------------------------------------

const webhooks = { list: [], editing: null };

function showWebhookError(message) {
  $("webhook-error").textContent = message;
  $("webhook-error").hidden = !message;
}

async function webhookRequest(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function loadWebhooks() {
  try {
    const data = await webhookRequest("GET", "api/webhooks");
    webhooks.list = data.webhooks || [];
    showWebhookError("");
  } catch (error) {
    showWebhookError(error.message === "Failed to fetch" ? "Network error" : error.message);
  }
  renderWebhooks();
}

function describeWebhook(hook) {
  return `${hook.zone.toUpperCase()} ${hook.event} = ${hook.value || "any"} → ${hook.method} ${hook.url}`;
}

function renderWebhooks() {
  const items = webhooks.list.map((hook) => {
    const item = document.createElement("li");
    item.classList.toggle("disabled", !hook.enabled);
    const title = document.createElement("h2");
    title.textContent = (hook.name || "Unnamed webhook") + (hook.enabled ? "" : " (disabled)");
    const summary = document.createElement("p");
    summary.className = "summary";
    summary.textContent = describeWebhook(hook);
    const actions = document.createElement("div");
    actions.className = "actions";
    for (const [label, handler] of [
      ["Edit", () => editWebhook(hook, false)],
      ["Duplicate", () => editWebhook(hook, true)],
      ["Delete", () => deleteWebhook(hook)],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", handler);
      actions.append(button);
    }
    item.append(title, summary, actions);
    return item;
  });
  $("webhook-list").replaceChildren(...items);
  $("webhook-empty").hidden = webhooks.list.length > 0 || !$("webhook-form").hidden;
}

function renderValueOptions(selected) {
  const event = $("webhook-event").value;
  let options;
  if (event === "input") {
    const inputs = [...state.inputs];
    if ($("webhook-zone").value !== "z1" && !inputs.includes("SOURCE")) inputs.unshift("SOURCE");
    if (selected && !inputs.includes(selected)) inputs.push(selected);
    options = [new Option("Any input", ""), ...inputs.map((name) => new Option(inputLabel(name), name))];
  } else {
    options = [new Option("On", "on"), new Option("Off", "off")];
  }
  $("webhook-value").replaceChildren(...options);
  if (selected !== undefined && [...$("webhook-value").options].some((o) => o.value === selected)) {
    $("webhook-value").value = selected;
  }
}

function renderBodyField() {
  $("webhook-body-field").hidden = $("webhook-method").value === "GET";
}

function editWebhook(hook, duplicate) {
  const source = hook || { name: "", enabled: true, zone: "z1", event: "power", value: "on", method: "POST", url: "", body: "", headers: {} };
  webhooks.editing = hook && !duplicate ? hook.id : null;
  $("webhook-form-title").textContent = !hook ? "New webhook" : duplicate ? "Duplicate webhook" : "Edit webhook";
  $("webhook-name").value = duplicate && source.name ? `${source.name} (copy)` : source.name;
  $("webhook-enabled").checked = source.enabled;
  $("webhook-zone").value = source.zone;
  $("webhook-event").value = source.event;
  renderValueOptions(source.value);
  $("webhook-method").value = source.method;
  $("webhook-url").value = source.url;
  $("webhook-body").value = source.body || "";
  $("webhook-headers").value = JSON.stringify(source.headers || {}, null, 2);
  renderBodyField();
  showWebhookError("");
  $("webhook-form").hidden = false;
  renderWebhooks();
  $("webhook-form").scrollIntoView({ block: "start" });
  $("webhook-url").focus({ preventScroll: true });
}

function closeWebhookForm() {
  webhooks.editing = null;
  $("webhook-form").hidden = true;
  renderWebhooks();
}

async function saveWebhook(event) {
  event.preventDefault();
  let headers;
  try {
    headers = JSON.parse($("webhook-headers").value || "{}");
  } catch {
    showWebhookError("Headers must be valid JSON.");
    return;
  }
  if (!headers || typeof headers !== "object" || Array.isArray(headers)) {
    showWebhookError("Headers must be a JSON object.");
    return;
  }
  const body = {
    name: $("webhook-name").value,
    enabled: $("webhook-enabled").checked,
    zone: $("webhook-zone").value,
    event: $("webhook-event").value,
    value: $("webhook-value").value,
    method: $("webhook-method").value,
    url: $("webhook-url").value,
    body: $("webhook-body").value,
    headers,
  };
  try {
    if (webhooks.editing) {
      await webhookRequest("PUT", `api/webhooks/${encodeURIComponent(webhooks.editing)}`, body);
    } else {
      await webhookRequest("POST", "api/webhooks", body);
    }
    closeWebhookForm();
    await loadWebhooks();
  } catch (error) {
    showWebhookError(error.message === "Failed to fetch" ? "Network error" : error.message);
  }
}

async function deleteWebhook(hook) {
  if (!window.confirm(`Delete webhook "${hook.name || hook.url}"?`)) return;
  try {
    await webhookRequest("DELETE", `api/webhooks/${encodeURIComponent(hook.id)}`);
    if (webhooks.editing === hook.id) closeWebhookForm();
    await loadWebhooks();
  } catch (error) {
    showWebhookError(error.message === "Failed to fetch" ? "Network error" : error.message);
  }
}

async function send(action, body) {
  try {
    const response = await fetch(`api/zones/${state.zone}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      showError(data.error || `Request failed (${response.status})`);
    } else {
      showError("");
    }
  } catch (error) {
    showError("Network error");
  }
}

let reconnectDelay = 1000;

function connect() {
  const url = new URL("api/ws", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(url);
  socket.addEventListener("open", () => {
    reconnectDelay = 1000;
    state.socketOpen = true;
    render();
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "status") {
      state.status = message;
      render();
    }
  });
  socket.addEventListener("close", () => {
    state.socketOpen = false;
    render();
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
  });
}

for (const tab of document.querySelectorAll('[role="tab"]')) {
  tab.addEventListener("click", () => (tab.dataset.view === "webhooks" ? selectWebhooks() : selectZone(tab.dataset.zone)));
}
$("webhook-add").addEventListener("click", () => editWebhook(null, false));
$("webhook-cancel").addEventListener("click", closeWebhookForm);
$("webhook-form").addEventListener("submit", saveWebhook);
$("webhook-event").addEventListener("change", () => renderValueOptions());
$("webhook-zone").addEventListener("change", () => renderValueOptions($("webhook-value").value));
$("webhook-method").addEventListener("change", renderBodyField);
$("input-shortcuts").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-input]");
  if (button && !button.disabled) send("input", { input: button.dataset.input });
});
$("power").addEventListener("click", () => send("power", { power: currentZone().power === "on" ? "off" : "on" }));
$("input").addEventListener("change", (event) => {
  if (event.target.value) send("input", { input: event.target.value });
});
$("volume").addEventListener("input", (event) => {
  state.draggingVolume = true;
  $("volume-value").textContent = formatVolume(Number(event.target.value));
});
$("volume").addEventListener("change", (event) => {
  state.draggingVolume = false;
  send("volume", { volume: Number(event.target.value) });
});
$("volume-down").addEventListener("click", () => send("volume", { volume: "down" }));
$("volume-up").addEventListener("click", () => send("volume", { volume: "up" }));
$("mute").addEventListener("click", () => send("mute", { muted: !currentZone().muted }));

fetch("api/inputs")
  .then((response) => response.json())
  .then((data) => {
    state.inputs = data.inputs || [];
    state.inputAliases = data.aliases || {};
    renderValueOptions($("webhook-value").value);
    render();
  })
  .catch(() => {});

render();
connect();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
