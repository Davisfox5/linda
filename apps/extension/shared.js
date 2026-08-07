// Shared helpers used by background.js, popup.js and options.js.
//
// Wire contract (verified against backend/app/api/websocket.py and
// backend/app/api/ws_tickets.py — do not change these without re-checking
// those files):
//
//   1. POST {apiBase}/api/v1/ws/tickets
//        headers: Authorization: Bearer <csk_... API key>, Content-Type: application/json
//        body:    { "role": "agent" }              (session_id omitted -> server mints one)
//        resp:    { ticket, session_id, role, expires_at }
//      (backend/app/api/ws_tickets.py:39-127; API key auth via
//      backend/app/auth.py:_extract_bearer_token / _principal_from_api_key)
//
//   2. WS {wsBase}/ws/live/{session_id}?ticket={ticket}
//      (backend/app/api/websocket.py:74; ticket is a query param, validated
//      before websocket.accept() — an absent/expired/consumed ticket closes
//      with code 4401)
//
//   3. Audio frames: binary WS frames forwarded verbatim to Deepgram
//      (`await dg_connection.send(data["bytes"])`, websocket.py:376-378).
//      The Deepgram `LiveOptions` built server-side
//      (websocket.py:185-190) does NOT set `encoding` / `sample_rate` /
//      `channels`. Per Deepgram's own docs ("Encoding is required when raw,
//      headerless audio packets are sent... If containerized audio packets
//      are sent, the encoding is automatically detected" — containerized
//      formats include opus-in-WebM), the only way this receiver accepts
//      audio at all is a self-describing container stream. We therefore
//      send a continuous `audio/webm;codecs=opus` MediaRecorder stream, NOT
//      raw PCM — sending raw PCM here would be silently rejected by
//      Deepgram. See offscreen.js for the capture path.

const API_V1_PREFIX = "/api/v1";
const TICKET_PATH = `${API_V1_PREFIX}/ws/tickets`;

export const STORAGE_KEYS = {
  apiBase: "linda_api_base",
  apiKey: "linda_api_key",
  appUrl: "linda_app_url",
  micDefault: "linda_mic_default",
};

/** Strip a trailing slash so we can safely concatenate paths. */
export function normalizeBase(url) {
  if (!url) return "";
  return url.trim().replace(/\/+$/, "");
}

/** https://host -> wss://host, http://host -> ws://host. */
export function toWsOrigin(httpBase) {
  const base = normalizeBase(httpBase);
  if (base.startsWith("https://")) return "wss://" + base.slice("https://".length);
  if (base.startsWith("http://")) return "ws://" + base.slice("http://".length);
  return base;
}

export async function loadConfig() {
  const stored = await chrome.storage.local.get([
    STORAGE_KEYS.apiBase,
    STORAGE_KEYS.apiKey,
    STORAGE_KEYS.appUrl,
    STORAGE_KEYS.micDefault,
  ]);
  return {
    apiBase: normalizeBase(stored[STORAGE_KEYS.apiBase] || ""),
    apiKey: (stored[STORAGE_KEYS.apiKey] || "").trim(),
    // App URL (the Next.js frontend, apps/app) is separate from the API
    // base in split dev setups (api :8000, app :3001) but the same host in
    // the reverse-proxied production deployment — see live-coaching.ts's
    // buildWsUrl() comment. Default to the API base when unset.
    appUrl: normalizeBase(stored[STORAGE_KEYS.appUrl] || stored[STORAGE_KEYS.apiBase] || ""),
    micDefault: !!stored[STORAGE_KEYS.micDefault],
  };
}

export async function saveConfig({ apiBase, apiKey, appUrl, micDefault }) {
  await chrome.storage.local.set({
    [STORAGE_KEYS.apiBase]: normalizeBase(apiBase),
    [STORAGE_KEYS.apiKey]: (apiKey || "").trim(),
    [STORAGE_KEYS.appUrl]: normalizeBase(appUrl),
    [STORAGE_KEYS.micDefault]: !!micDefault,
  });
}

/** Mint a single-use agent ticket. Throws with a readable message on failure. */
export async function mintTicket(config) {
  if (!config.apiBase || !config.apiKey) {
    throw new Error("Set the LINDA API base URL and API key in the extension options first.");
  }
  const url = `${config.apiBase}${TICKET_PATH}`;
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify({ role: "agent" }),
    });
  } catch (err) {
    throw new Error(
      `Could not reach ${url} (${err.message}). Check the API base URL and that the extension has permission to access this host.`,
    );
  }
  if (!resp.ok) {
    let detail = "";
    try {
      const body = await resp.json();
      detail = body.detail ? ` — ${JSON.stringify(body.detail)}` : "";
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`Ticket request failed (${resp.status})${detail}`);
  }
  return resp.json(); // { ticket, session_id, role, expires_at }
}

export function buildLiveWsUrl(config, sessionId, ticket) {
  const wsOrigin = toWsOrigin(config.apiBase);
  return `${wsOrigin}/ws/live/${encodeURIComponent(sessionId)}?ticket=${encodeURIComponent(ticket)}`;
}

export function buildOpenInLindaUrl(config, sessionId) {
  if (!config.appUrl) return null;
  return `${config.appUrl}/live/${encodeURIComponent(sessionId)}`;
}

/** Request (or confirm we already hold) host permission for one origin. */
export async function ensureHostPermission(baseUrl) {
  const origin = new URL(baseUrl).origin + "/*";
  const already = await chrome.permissions.contains({ origins: [origin] });
  if (already) return true;
  return chrome.permissions.request({ origins: [origin] });
}
