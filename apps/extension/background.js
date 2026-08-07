// Background service worker.
//
// Orchestrates a listening session: mints a ticket, spins up the offscreen
// document (required in MV3 for tabCapture + MediaRecorder/AudioContext,
// which service workers cannot host), and hands it the stream id + WS URL.
// The offscreen document owns the actual MediaRecorder/WebSocket so the
// session survives this service worker being killed/restarted by Chrome.
//
// Messaging convention: every runtime message carries a `target` of
// "background" | "offscreen" | "popup". Each context ignores messages not
// addressed to it. chrome.runtime.sendMessage() is inherently broadcast —
// there's no addressed delivery — so this is just a filter, not routing.

import { loadConfig, mintTicket, buildLiveWsUrl, buildOpenInLindaUrl } from "./shared.js";

const OFFSCREEN_URL = chrome.runtime.getURL("offscreen.html");

// In-memory cache of the last known status, mirrored to
// chrome.storage.session so a freshly (re)spawned service worker or a
// reopened popup can recover it. The offscreen document is the source of
// truth while it's alive; this is just what we show before it reports in.
let lastStatus = { status: "idle", sessionId: null, error: null, openInLindaUrl: null };

async function persistStatus(next) {
  lastStatus = { ...lastStatus, ...next };
  await chrome.storage.session.set({ linda_status: lastStatus });
}

async function restoreStatus() {
  const stored = await chrome.storage.session.get("linda_status");
  if (stored.linda_status) lastStatus = stored.linda_status;
}
restoreStatus();

async function hasOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [OFFSCREEN_URL],
  });
  return contexts.length > 0;
}

async function ensureOffscreenDocument() {
  if (await hasOffscreenDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ["USER_MEDIA"],
    justification:
      "Capture this tab's audio (chrome.tabCapture) and optionally the microphone, mix them, and stream to LINDA's live-transcription WebSocket.",
  });
}

async function closeOffscreenDocument() {
  if (await hasOffscreenDocument()) {
    await chrome.offscreen.closeDocument();
  }
}

async function startSession({ tabId, streamId, micEnabled }) {
  const config = await loadConfig();
  const ticketInfo = await mintTicket(config); // throws on failure — caller (popup) surfaces it
  const wsUrl = buildLiveWsUrl(config, ticketInfo.session_id, ticketInfo.ticket);
  const openInLindaUrl = buildOpenInLindaUrl(config, ticketInfo.session_id);

  await ensureOffscreenDocument();

  await persistStatus({
    status: "connecting",
    sessionId: ticketInfo.session_id,
    error: null,
    openInLindaUrl,
  });

  chrome.runtime.sendMessage({
    target: "offscreen",
    type: "start-capture",
    streamId,
    tabId,
    wsUrl,
    micEnabled,
    sessionId: ticketInfo.session_id,
  });

  return { sessionId: ticketInfo.session_id, openInLindaUrl };
}

async function stopSession() {
  chrome.runtime.sendMessage({ target: "offscreen", type: "stop-capture" });
  // Offscreen confirms with an "offscreen-status" idle message (handled
  // below), which is what actually tears down the document — this just
  // kicks off a clean stop so LINDA finalizes the transcript instead of
  // yanking the WS closed mid-utterance.
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message) return undefined;

  if (message.target === "background" || message.target === undefined) {
    if (message.type === "start") {
      startSession(message)
        .then((result) => sendResponse({ ok: true, ...result }))
        .catch((err) => {
          persistStatus({ status: "error", error: err.message });
          sendResponse({ ok: false, error: err.message });
        });
      return true; // async response
    }

    if (message.type === "stop") {
      stopSession()
        .then(() => sendResponse({ ok: true }))
        .catch((err) => sendResponse({ ok: false, error: err.message }));
      return true;
    }

    if (message.type === "get-status") {
      sendResponse(lastStatus);
      return false;
    }
  }

  // Status reports coming up from the offscreen document.
  if (message.target === "popup" && message.type === "offscreen-status") {
    persistStatus({
      status: message.status,
      sessionId: message.sessionId ?? lastStatus.sessionId,
      error: message.error ?? null,
    });
    if (["idle", "disconnected", "error"].includes(message.status)) {
      // Capture already stopped itself on the offscreen side by the time
      // any of these statuses is reported — tearing down the (now idle)
      // document just frees the tab/mic capture handles. "Restart" from
      // the popup is a fresh start-session call, not a resume.
      closeOffscreenDocument().catch(() => {});
    }
    // No sendResponse — this is a broadcast the popup (if open) also
    // receives directly and renders live; background only persists it.
  }

  return undefined;
});
