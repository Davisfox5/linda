import { loadConfig, saveConfig, ensureHostPermission } from "./shared.js";

const els = {
  form: document.getElementById("options-form"),
  apiBase: document.getElementById("api-base"),
  apiKey: document.getElementById("api-key"),
  appUrl: document.getElementById("app-url"),
  micDefault: document.getElementById("mic-default"),
  testMic: document.getElementById("test-mic"),
  micTestResult: document.getElementById("mic-test-result"),
  saveResult: document.getElementById("save-result"),
};

async function populate() {
  const config = await loadConfig();
  els.apiBase.value = config.apiBase;
  els.apiKey.value = config.apiKey;
  els.appUrl.value = config.appUrl === config.apiBase ? "" : config.appUrl;
  els.micDefault.checked = config.micDefault;
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.saveResult.textContent = "";

  const apiBase = els.apiBase.value.trim();
  await saveConfig({
    apiBase,
    apiKey: els.apiKey.value.trim(),
    appUrl: els.appUrl.value.trim(),
    micDefault: els.micDefault.checked,
  });

  // Best-effort — grants the host permission now (this submit click is a
  // user gesture) so the popup's Start button doesn't need to prompt too.
  try {
    if (apiBase) await ensureHostPermission(apiBase);
  } catch {
    /* user can still grant it from the popup when starting a session */
  }

  els.saveResult.textContent = "Saved.";
  setTimeout(() => {
    els.saveResult.textContent = "";
  }, 2000);
});

els.testMic.addEventListener("click", async () => {
  els.micTestResult.textContent = "Requesting…";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    stream.getTracks().forEach((t) => t.stop());
    // Granting mic permission here (a visible, user-facing extension page)
    // is what lets the invisible offscreen document reuse it later —
    // Chrome scopes getUserMedia permission per extension origin, and
    // offscreen documents can't show their own permission prompt.
    els.micTestResult.textContent = "Microphone access granted. Mic mixing is ready to use.";
  } catch (err) {
    els.micTestResult.textContent = `Microphone access denied or unavailable: ${err.message}`;
  }
});

populate();
