"use strict";

const ZONE_NAMES = { z1: "Main zone", z2: "Zone 2", z3: "Zone 3" };
const state = { zone: "z1", status: null, socketOpen: false, inputs: [], draggingVolume: false };
const $ = (id) => document.getElementById(id);

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
  const current = [...select.options].map((option) => option.value);
  if (current.join("\n") !== options.join("\n")) {
    select.replaceChildren(...options.map((name) => new Option(name || "—", name)));
  }
  select.value = zone.input || "";
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
  $("zone-panel").setAttribute("aria-labelledby", `tab-${zone}`);
  showError("");
  render();
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
  tab.addEventListener("click", () => selectZone(tab.dataset.zone));
}
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
    render();
  })
  .catch(() => {});

render();
connect();
