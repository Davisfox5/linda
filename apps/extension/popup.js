import { loadConfig, ensureHostPermission, STORAGE_KEYS } from "./shared.js";

const els = {
  statusPill: document.getElementById("status-pill"),
  configWarning: document.getElementById("config-warning"),
  openOptions: document.getElementById("open-options"),
  errorText: document.getElementById("error-text"),
  micToggle: document.getElementById("mic-toggle"),
  startBtn: document.getElementById("start-btn"),
  stopBtn: document.getElementById("stop-btn"),
  restartBtn: document.getElementById("restart-btn"),
  openInLinda: document.getElementById("open-in-linda"),
};

const PILL_LABEL = {
  idle: "Idle",
  connecting: "Connecting…",
  listening: "Listening",
  disconnected: "Disconnected",
  error: "Error",
};

let config = null;

// Merged view of everything we know about the current session. Offscreen
// broadcasts only carry {status, sessionId, error} — merge rather than
// replace so a live "listening" update doesn't blow away the
// openInLindaUrl we got back from the start-session response.
let currentState = { status: "idle", sessionId: null, error: null, openInLindaUrl: null };

function applyState(patch) {
  currentState = { ...currentState, ...patch };
  render(currentState);
}

function render(state) {
  const status = state.status || "idle";
  els.statusPill.textContent = PILL_LABEL[status] || status;
  els.statusPill.className = `pill pill-${status}`;

  if (state.error) {
    els.errorText.textContent = state.error;
    els.errorText.classList.remove("hidden");
  } else {
    els.errorText.classList.add("hidden");
  }

  const busy = status === "connecting" || status === "listening";
  els.startBtn.classList.toggle("hidden", busy || status === "disconnected" || status === "error");
  els.stopBtn.classList.toggle("hidden", !busy);
  els.restartBtn.classList.toggle("hidden", status !== "disconnected" && status !== "error");

  if (state.openInLindaUrl && (status === "connecting" || status === "listening" || status === "disconnected")) {
    els.openInLinda.href = state.openInLindaUrl;
    els.openInLinda.classList.remove("hidden");
  } else {
    els.openInLinda.classList.add("hidden");
  }
}

async function refreshConfigWarning() {
  config = await loadConfig();
  const missing = !config.apiBase || !config.apiKey;
  els.configWarning.classList.toggle("hidden", !missing);
  els.startBtn.disabled = missing;
  return !missing;
}

async function getActiveMeetingTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id == null) {
    throw new Error("No active tab found.");
  }
  if (!/^https?:\/\//.test(tab.url || "")) {
    throw new Error("Switch to the meeting tab (Meet/Zoom/Teams) before starting.");
  }
  return tab;
}

async function startListening() {
  els.errorText.classList.add("hidden");
  const ok = await refreshConfigWarning();
  if (!ok) return;

  try {
    // chrome.permissions.request needs a user gesture — this handler is
    // one, being called directly from the button's click listener.
    const granted = await ensureHostPermission(config.apiBase);
    if (!granted) {
      throw new Error("Host permission for the LINDA API is required to mint a ticket.");
    }

    const tab = await getActiveMeetingTab();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const micEnabled = els.micToggle.checked;

    await chrome.storage.local.set({ [STORAGE_KEYS.micDefault]: micEnabled });

    applyState({ status: "connecting", error: null });

    const resp = await chrome.runtime.sendMessage({
      type: "start",
      tabId: tab.id,
      streamId,
      micEnabled,
    });
    if (!resp || !resp.ok) {
      throw new Error((resp && resp.error) || "Failed to start.");
    }
    applyState({ sessionId: resp.sessionId, openInLindaUrl: resp.openInLindaUrl });
  } catch (err) {
    applyState({ status: "error", error: err.message });
  }
}

async function stopListening() {
  await chrome.runtime.sendMessage({ type: "stop" });
}

els.startBtn.addEventListener("click", startListening);
els.restartBtn.addEventListener("click", startListening);
els.stopBtn.addEventListener("click", stopListening);
els.openOptions.addEventListener("click", () => chrome.runtime.openOptionsPage());

chrome.runtime.onMessage.addListener((message) => {
  if (message && message.target === "popup" && message.type === "offscreen-status") {
    applyState(message);
  }
});

(async () => {
  await refreshConfigWarning();
  els.micToggle.checked = config.micDefault;

  const state = await chrome.runtime.sendMessage({ type: "get-status" });
  if (state) applyState(state);
})();
