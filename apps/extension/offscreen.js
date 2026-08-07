// Offscreen document: owns the actual audio graph, MediaRecorder and
// WebSocket. Runs independently of the background service worker's
// lifecycle (that's the whole reason MV3 requires an offscreen document
// for tabCapture + long-lived getUserMedia streams).
//
// Audio format: a single continuous `audio/webm;codecs=opus` stream from
// MediaRecorder, chunked every ~250ms and sent as binary WS frames. This
// matches /ws/live's real contract — see shared.js's header comment and
// backend/app/api/websocket.py:185-190 (no encoding/sample_rate/channels
// set on the server's Deepgram LiveOptions, so Deepgram auto-detects the
// container; opus-in-WebM is one of the containers it recognizes). We do
// NOT hand-roll raw PCM via AudioWorklet — that would need an explicit
// `encoding`/`sample_rate` the server never sends, and Deepgram rejects
// headerless raw audio without them.

const PREFERRED_MIME_TYPE = "audio/webm;codecs=opus";
const CHUNK_MS = 250;

function pickMimeType() {
  if (MediaRecorder.isTypeSupported(PREFERRED_MIME_TYPE)) return PREFERRED_MIME_TYPE;
  // Fallback — Chrome's default webm container still encodes opus audio,
  // it just omits explicitly negotiating the codec string.
  return "audio/webm";
}

function reportStatus(status, extra = {}) {
  chrome.runtime.sendMessage({
    target: "popup",
    type: "offscreen-status",
    status,
    ...extra,
  });
}

// One `ctx` object per start-capture attempt. `active` always points at
// the most recent one. Every async callback below closes over its own
// `ctx` and checks `active === ctx` before touching shared state or
// reporting status, so a slow teardown from a superseded attempt can
// never clobber (or narrate over) the current one.
let active = null;
let generationCounter = 0;

function makeCtx(sessionId) {
  generationCounter += 1;
  return {
    gen: generationCounter,
    sessionId,
    ws: null,
    recorder: null,
    audioCtx: null,
    tracks: [],
    stopping: false,
  };
}

function teardown(ctx) {
  if (!ctx) return;
  if (ctx.recorder && ctx.recorder.state !== "inactive") {
    try {
      ctx.recorder.stop();
    } catch {
      /* already stopped */
    }
  }
  ctx.recorder = null;
  for (const track of ctx.tracks) {
    try {
      track.stop();
    } catch {
      /* ignore */
    }
  }
  ctx.tracks = [];
  if (ctx.audioCtx) {
    ctx.audioCtx.close().catch(() => {});
    ctx.audioCtx = null;
  }
}

async function buildMixedStream(streamId, micEnabled) {
  // Tab audio — MV3 offscreen-document capture pattern: the popup obtains
  // the streamId via chrome.tabCapture.getMediaStreamId(), and this
  // getUserMedia call (with the Chrome-specific `mandatory` constraint
  // block) is how an extension page turns that id into an actual
  // MediaStream.
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
      },
    },
    video: false,
  });

  const audioCtx = new AudioContext();
  const tabSource = audioCtx.createMediaStreamSource(tabStream);

  // Keep the tab audible to the user — tabCapture mutes the captured tab
  // by default, so without this the meeting would go silent for them.
  tabSource.connect(audioCtx.destination);

  // Downmix everything to mono before it hits the recorder.
  const mixDestination = audioCtx.createMediaStreamDestination();
  mixDestination.channelCount = 1;
  mixDestination.channelCountMode = "explicit";
  mixDestination.channelInterpretation = "speakers";

  tabSource.connect(mixDestination);

  const tracks = [...tabStream.getTracks()];

  if (micEnabled) {
    try {
      const micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      const micSource = audioCtx.createMediaStreamSource(micStream);
      micSource.connect(mixDestination);
      tracks.push(...micStream.getTracks());
    } catch (err) {
      // Mic permission for the extension origin has to be granted once
      // from a visible extension page (options page "Test microphone
      // access" button) — invisible offscreen documents can't prompt.
      // Non-fatal: fall back to tab-only audio.
      console.warn("[LINDA] mic mixing unavailable, continuing tab-only:", err);
    }
  }

  return { mixedStream: mixDestination.stream, audioCtx, tracks };
}

async function startCapture({ streamId, wsUrl, micEnabled, sessionId }) {
  // Supersede whatever is currently running, synchronously, before doing
  // any async work for the new attempt.
  const prev = active;
  const ctx = makeCtx(sessionId);
  active = ctx;
  if (prev) {
    prev.stopping = true;
    teardown(prev);
    if (prev.ws && prev.ws.readyState !== WebSocket.CLOSED) {
      try {
        prev.ws.close(1000, "superseded");
      } catch {
        /* ignore */
      }
    }
  }

  reportStatus("connecting", { sessionId });

  let mixedStream;
  try {
    const built = await buildMixedStream(streamId, micEnabled);
    if (active !== ctx) {
      // Superseded again while we were awaiting getUserMedia — release
      // what we just acquired rather than installing it.
      built.tracks.forEach((t) => t.stop());
      built.audioCtx.close().catch(() => {});
      return;
    }
    mixedStream = built.mixedStream;
    ctx.audioCtx = built.audioCtx;
    ctx.tracks = built.tracks;
  } catch (err) {
    if (active === ctx) {
      reportStatus("error", { sessionId, error: `Audio capture failed: ${err.message}` });
    }
    return;
  }

  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch (err) {
    if (active === ctx) {
      reportStatus("error", { sessionId, error: `WebSocket open failed: ${err.message}` });
      teardown(ctx);
    }
    return;
  }
  ctx.ws = ws;

  ws.onopen = () => {
    if (active !== ctx) return;
    let recorder;
    try {
      recorder = new MediaRecorder(mixedStream, { mimeType: pickMimeType() });
    } catch (err) {
      reportStatus("error", { sessionId, error: `MediaRecorder unsupported: ${err.message}` });
      ws.close();
      return;
    }
    ctx.recorder = recorder;

    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0 && ws.readyState === WebSocket.OPEN) {
        event.data.arrayBuffer().then((buf) => {
          if (ws.readyState === WebSocket.OPEN) ws.send(buf);
        });
      }
    };

    recorder.onstop = () => {
      // Give the final ondataavailable a tick to flush before closing —
      // it fires synchronously before this handler in practice, but the
      // arrayBuffer() read above is async, so this is a small safety gap.
      setTimeout(() => {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close(1000, "client_stop");
        }
      }, 50);
    };

    recorder.start(CHUNK_MS);
    reportStatus("listening", { sessionId });
  };

  ws.onclose = (ev) => {
    if (active !== ctx) return; // already superseded/torn down elsewhere
    const wasIntentional = ctx.stopping;
    teardown(ctx);
    active = null;
    if (wasIntentional) {
      reportStatus("idle", { sessionId });
    } else {
      // Ticket is single-use — per spec, no auto-reconnect. Surface the
      // drop and let the popup offer a restart (which mints a fresh
      // ticket + session from scratch).
      reportStatus("disconnected", {
        sessionId,
        error: `Connection closed (code ${ev.code}${ev.reason ? `: ${ev.reason}` : ""}).`,
      });
    }
  };

  ws.onerror = () => {
    if (active === ctx) reportStatus("error", { sessionId, error: "WebSocket error." });
  };
}

async function stopCapture() {
  const ctx = active;
  if (!ctx) {
    reportStatus("idle", {});
    return;
  }
  ctx.stopping = true;

  if (ctx.recorder && ctx.recorder.state === "recording") {
    // recorder.onstop -> flush -> ws.close(1000) -> ws.onclose does the
    // rest (teardown + "idle" report). This is what lets LINDA finalize
    // the transcript server-side instead of finding a half-closed socket.
    ctx.recorder.stop();
    return;
  }

  if (ctx.ws) {
    ctx.ws.close(1000, "client_stop"); // -> ws.onclose does the rest
  } else {
    teardown(ctx);
    active = null;
    reportStatus("idle", {});
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.target !== "offscreen") return;
  if (message.type === "start-capture") {
    startCapture(message);
  } else if (message.type === "stop-capture") {
    stopCapture();
  }
});
