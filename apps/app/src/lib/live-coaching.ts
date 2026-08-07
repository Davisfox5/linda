"use client";

import {
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
} from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useApi } from "./api";

/* ── Backend event shapes ─────────────────────────────────────────────
 *
 * Mirrored from ``backend/app/api/websocket.py``. The server emits a
 * mixed bag of message types on the same socket — keep this union
 * narrow on what the SPA *uses* so unhandled future kinds are caught
 * by the exhaustive ``never`` branch in the reducer below.
 *
 * NOTE: shapes here are verified against the source, not invented.
 * If the backend adds a field, add it here too — the reducer relies
 * on these literals to discriminate. */

export interface TranscriptFinalEvent {
    type: "final";
    text: string;
    speaker: number | string | null;
    timestamp: number;
}

export interface TranscriptPartialEvent {
    type: "partial";
    text: string;
    speaker: number | string | null;
    timestamp: number;
}

export interface CoachingEvent {
    type: "coaching";
    hint: string;
    source_doc_title: string | null;
    confidence: number;
}

export interface AlertEvent {
    type: "alert";
    kind: string;
    severity: "info" | "warn" | "alert";
    message: string;
    evidence?: Record<string, unknown>;
    t?: number;
}

export interface BriefAlertEvent {
    type: "brief_alert";
    kind: "churn" | "upsell" | "escalation" | "advocate" | "sentiment_drop";
    message: string;
    from?: number;
    to?: number;
}

export interface KbAnswerEvent {
    type: "kb_answer";
    pinned?: boolean;
    pin_id?: string;
    query?: string;
    snippet: string;
    chunk_id?: string;
    doc_id?: string;
    doc_title?: string;
    source_url?: string | null;
    confidence?: number;
    score?: number;
    source?: string;
    urgency?: string;
}

export interface SentimentUpdateEvent {
    type: "sentiment_update";
    score: number | null;
    trend: string | null;
}

export interface FeaturesEvent {
    type: "features";
    [k: string]: unknown;
}

export interface PongEvent {
    type: "pong";
}

export type LiveEvent =
    | TranscriptFinalEvent
    | TranscriptPartialEvent
    | CoachingEvent
    | AlertEvent
    | BriefAlertEvent
    | KbAnswerEvent
    | SentimentUpdateEvent
    | FeaturesEvent
    | PongEvent;

/* Outbound events the SPA pushes upstream. The ``tag`` shape isn't a
 * built-in backend message type today, so the live-call handler will
 * silently drop it if it doesn't recognise it — that's by design (see
 * the JSON-decode tolerance in ``websocket.py``'s receive loop). */
export interface OutboundTagEvent {
    type: "tag";
    category: "question" | "coaching" | "praise" | "issue";
    note: string;
    timestamp?: number;
}

export interface OutboundPingEvent {
    type: "ping";
}

export type OutboundEvent = OutboundTagEvent | OutboundPingEvent;

/* ── Ticket exchange ─────────────────────────────────────────────── */

export type TicketRole = "agent" | "monitor";

export interface TicketRequest {
    role: TicketRole;
    session_id?: string;
    user_id?: string;
}

export interface TicketResponse {
    ticket: string;
    session_id: string;
    role: TicketRole | string;
    expires_at: number;
}

export function useMintTicket() {
    const api = useApi();
    return useMutation({
        mutationFn: (payload: TicketRequest) =>
            api.post<TicketResponse>("/ws/tickets", payload),
    });
}

/* ── Coaching sessions list (manager+) ───────────────────────────── */

export interface CoachingSessionRow {
    id: string;
    tenant_id: string;
    agent_id: string;
    agent_name: string | null;
    interaction_id: string | null;
    interaction_title: string | null;
    source: string | null;
    status: string;
    started_at: string;
    ended_at: string | null;
    duration_seconds: number | null;
}

export interface CoachingSessionListPage {
    items: CoachingSessionRow[];
    total: number;
}

export function useCoachingSessions(limit = 25) {
    const api = useApi();
    return useQuery({
        queryKey: ["coaching-sessions", { limit }],
        queryFn: () =>
            api.get<CoachingSessionListPage>(
                `/coaching/sessions?limit=${limit}`,
            ),
    });
}

/* ── WebSocket session hook ──────────────────────────────────────── */

export type ConnectionStatus =
    | "idle"
    | "connecting"
    | "live"
    | "reconnecting"
    | "ended"
    | "expired"
    | "error";

export interface TranscriptLine {
    /** Stable id — partials reuse a single id and finals get a new one
     *  so the React list keys stay sane across the partial→final swap. */
    id: string;
    text: string;
    speaker: number | string | null;
    timestamp: number;
    isFinal: boolean;
}

export interface SuggestionCard {
    id: string;
    /** Logical category we render as an icon + tone. Sourced from
     *  whatever the backend gave us — coaching hints come in as
     *  ``coaching``, ``alert.kind`` and ``brief_alert.kind`` widen the
     *  vocabulary at runtime. We don't enum these on the SPA so a new
     *  backend kind doesn't silently disappear. */
    category: string;
    severity: "info" | "warn" | "critical";
    message: string;
    detail?: string;
    sourceDocTitle?: string | null;
    confidence?: number;
    /** Wallclock receipt time so the 60s fade-out can pick stale cards. */
    receivedAt: number;
}

export interface LiveSessionState {
    status: ConnectionStatus;
    transcript: TranscriptLine[];
    suggestions: SuggestionCard[];
    sentiment: { score: number | null; trend: string | null } | null;
    elapsedMs: number;
    /** Most recent ``code`` returned by the WebSocket close event. Useful
     *  for the UI to distinguish 4401 (ticket expired) vs other closes. */
    closeCode: number | null;
    error: string | null;
    send: (event: OutboundEvent) => void;
    close: () => void;
}

interface UseLiveSessionArgs {
    ticket: string | null;
    sessionId: string | null;
    role: TicketRole;
    /** Override the WebSocket origin. Defaults to the page origin —
     *  works when the FastAPI backend is reverse-proxied at the same
     *  hostname (the production deployment). Override via
     *  ``NEXT_PUBLIC_WS_BASE_URL`` for local dev where the SPA is on
     *  :3001 and the backend is on :8000. */
    wsBaseUrl?: string;
}

const DEFAULT_RECONNECT_DELAYS_MS = [1_000, 3_000, 9_000];

function buildWsUrl(args: {
    base: string | undefined;
    role: TicketRole;
    sessionId: string;
    ticket: string;
}): string {
    let origin = args.base;
    if (!origin && typeof window !== "undefined") {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        origin = `${proto}//${window.location.host}`;
    }
    if (!origin) {
        // Last-ditch fallback for non-browser callers (the build step).
        origin = "ws://localhost:8000";
    }
    const path = args.role === "monitor" ? "/ws/monitor/" : "/ws/live/";
    return `${origin}${path}${encodeURIComponent(args.sessionId)}?ticket=${encodeURIComponent(args.ticket)}`;
}

function severityFromAlert(severity: string): "info" | "warn" | "critical" {
    if (severity === "alert") return "critical";
    if (severity === "warn") return "warn";
    return "info";
}

function severityFromBriefAlertKind(
    kind: BriefAlertEvent["kind"],
): "info" | "warn" | "critical" {
    if (kind === "escalation" || kind === "churn") return "critical";
    if (kind === "sentiment_drop") return "warn";
    if (kind === "advocate") return "info";
    return "warn";
}

let _suggestionCounter = 0;
function nextId(prefix: string): string {
    _suggestionCounter += 1;
    return `${prefix}-${Date.now().toString(36)}-${_suggestionCounter}`;
}

/**
 * Manage a single live-coaching WebSocket connection.
 *
 * Lifecycle:
 *   - Returns ``status: "idle"`` until ``ticket`` and ``sessionId`` are
 *     both supplied.
 *   - Opens once; on unexpected close (anything except 1000 or 4401)
 *     reconnects with the 1s/3s/9s backoff schedule.
 *   - 4401 promotes status to ``expired`` and stops trying — the UI
 *     should mint a fresh ticket and pass it back in to retry.
 *   - Cleans up the socket on unmount or when ``ticket`` changes.
 */
export function useLiveSession(args: UseLiveSessionArgs): LiveSessionState {
    const { ticket, sessionId, role, wsBaseUrl } = args;

    const [status, setStatus] = useState<ConnectionStatus>("idle");
    const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
    const [suggestions, setSuggestions] = useState<SuggestionCard[]>([]);
    const [sentiment, setSentiment] = useState<
        LiveSessionState["sentiment"]
    >(null);
    const [elapsedMs, setElapsedMs] = useState(0);
    const [closeCode, setCloseCode] = useState<number | null>(null);
    const [error, setError] = useState<string | null>(null);

    const wsRef = useRef<WebSocket | null>(null);
    const startedAtRef = useRef<number | null>(null);
    const reconnectAttemptsRef = useRef(0);
    const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const intentionalCloseRef = useRef(false);
    /** Active partial line index keyed by speaker — when a final lands
     *  for the same speaker we *replace* the partial in-place so the UI
     *  doesn't double-render the same words. */
    const partialIdsBySpeakerRef = useRef<Map<string, string>>(new Map());

    const wsUrl = useMemo(() => {
        if (!ticket || !sessionId) return null;
        return buildWsUrl({ base: wsBaseUrl, role, sessionId, ticket });
    }, [ticket, sessionId, role, wsBaseUrl]);

    const handleEvent = useCallback((event: LiveEvent) => {
        switch (event.type) {
            case "partial": {
                const speakerKey =
                    event.speaker == null ? "?" : String(event.speaker);
                setTranscript((cur) => {
                    const existing =
                        partialIdsBySpeakerRef.current.get(speakerKey);
                    if (existing) {
                        return cur.map((t) =>
                            t.id === existing
                                ? { ...t, text: event.text, isFinal: false }
                                : t,
                        );
                    }
                    const id = nextId("p");
                    partialIdsBySpeakerRef.current.set(speakerKey, id);
                    return [
                        ...cur,
                        {
                            id,
                            text: event.text,
                            speaker: event.speaker,
                            timestamp: event.timestamp,
                            isFinal: false,
                        },
                    ];
                });
                break;
            }
            case "final": {
                const speakerKey =
                    event.speaker == null ? "?" : String(event.speaker);
                setTranscript((cur) => {
                    const existing =
                        partialIdsBySpeakerRef.current.get(speakerKey);
                    partialIdsBySpeakerRef.current.delete(speakerKey);
                    if (existing) {
                        return cur.map((t) =>
                            t.id === existing
                                ? {
                                      ...t,
                                      id: nextId("f"),
                                      text: event.text,
                                      timestamp: event.timestamp,
                                      isFinal: true,
                                  }
                                : t,
                        );
                    }
                    return [
                        ...cur,
                        {
                            id: nextId("f"),
                            text: event.text,
                            speaker: event.speaker,
                            timestamp: event.timestamp,
                            isFinal: true,
                        },
                    ];
                });
                break;
            }
            case "coaching": {
                setSuggestions((cur) => [
                    {
                        id: nextId("c"),
                        category: "coaching",
                        severity: "info",
                        message: event.hint,
                        sourceDocTitle: event.source_doc_title,
                        confidence: event.confidence,
                        receivedAt: Date.now(),
                    },
                    ...cur,
                ]);
                break;
            }
            case "alert": {
                setSuggestions((cur) => [
                    {
                        id: nextId("a"),
                        category: event.kind || "alert",
                        severity: severityFromAlert(event.severity),
                        message: event.message,
                        receivedAt: Date.now(),
                    },
                    ...cur,
                ]);
                break;
            }
            case "brief_alert": {
                setSuggestions((cur) => [
                    {
                        id: nextId("b"),
                        category: event.kind,
                        severity: severityFromBriefAlertKind(event.kind),
                        message: event.message,
                        receivedAt: Date.now(),
                    },
                    ...cur,
                ]);
                break;
            }
            case "kb_answer": {
                const title = event.doc_title || "Knowledge base";
                setSuggestions((cur) => [
                    {
                        id: nextId("k"),
                        category: "kb",
                        severity: "info",
                        message: event.snippet,
                        sourceDocTitle: title,
                        confidence: event.confidence ?? event.score,
                        detail: event.query,
                        receivedAt: Date.now(),
                    },
                    ...cur,
                ]);
                break;
            }
            case "sentiment_update": {
                setSentiment({ score: event.score, trend: event.trend });
                break;
            }
            case "features":
            case "pong":
                // Visible feature snapshots don't surface in the v1 UI;
                // pongs are housekeeping. Both still satisfy the union.
                break;
            default: {
                // Unknown event types are ignored — the union above is
                // intentionally narrow so we notice in tsc when the
                // server adds something new.
                const _exhaustive: never = event;
                void _exhaustive;
            }
        }
    }, []);

    /* Connect / reconnect ─────────────────────────────────────────── */
    useEffect(() => {
        if (!wsUrl) {
            setStatus("idle");
            return;
        }

        intentionalCloseRef.current = false;
        reconnectAttemptsRef.current = 0;

        let cancelled = false;

        const connect = () => {
            if (cancelled) return;
            setStatus(reconnectAttemptsRef.current === 0 ? "connecting" : "reconnecting");
            setError(null);

            let socket: WebSocket;
            try {
                socket = new WebSocket(wsUrl);
            } catch (e) {
                setError(e instanceof Error ? e.message : "Failed to open socket");
                setStatus("error");
                return;
            }
            wsRef.current = socket;

            socket.onopen = () => {
                if (cancelled) return;
                startedAtRef.current = Date.now();
                reconnectAttemptsRef.current = 0;
                setStatus("live");
            };

            socket.onmessage = (msg) => {
                if (cancelled) return;
                try {
                    const data = JSON.parse(msg.data) as LiveEvent;
                    handleEvent(data);
                } catch {
                    // Non-JSON frames (raw text from the monitor relay)
                    // aren't part of the structured event protocol — skip.
                }
            };

            socket.onerror = () => {
                if (cancelled) return;
                setError("WebSocket error");
            };

            socket.onclose = (ev) => {
                if (cancelled) return;
                wsRef.current = null;
                setCloseCode(ev.code);

                if (intentionalCloseRef.current) {
                    setStatus("ended");
                    return;
                }
                if (ev.code === 4401) {
                    setStatus("expired");
                    setError("Session ticket expired or unauthorized.");
                    return;
                }
                if (ev.code === 4429) {
                    setStatus("error");
                    setError("Rate limited — try again shortly.");
                    return;
                }
                if (ev.code === 1000) {
                    setStatus("ended");
                    return;
                }

                // Auto-reconnect with backoff. The same ticket is
                // single-use server-side, so we can only reconnect if
                // the ticket layer stays valid through the close — in
                // practice this catches transient network blips before
                // the ticket TTL elapses; otherwise the next attempt
                // returns 4401 and we fall through to ``expired``.
                const attempt = reconnectAttemptsRef.current;
                if (attempt >= DEFAULT_RECONNECT_DELAYS_MS.length) {
                    setStatus("error");
                    setError(
                        "Connection lost. Get a new ticket to retry.",
                    );
                    return;
                }
                const delay = DEFAULT_RECONNECT_DELAYS_MS[attempt];
                reconnectAttemptsRef.current = attempt + 1;
                setStatus("reconnecting");
                reconnectTimerRef.current = setTimeout(connect, delay);
            };
        };

        connect();

        return () => {
            cancelled = true;
            if (reconnectTimerRef.current) {
                clearTimeout(reconnectTimerRef.current);
                reconnectTimerRef.current = null;
            }
            if (wsRef.current) {
                intentionalCloseRef.current = true;
                try {
                    wsRef.current.close(1000, "client_unmount");
                } catch {
                    /* ignore */
                }
                wsRef.current = null;
            }
            partialIdsBySpeakerRef.current.clear();
        };
    }, [wsUrl, handleEvent]);

    /* Reset transient state when (ticket, sessionId) changes — so a new
     * session starts from a blank UI without bleeding the previous run's
     * transcript or suggestion stack. */
    useEffect(() => {
        if (!ticket || !sessionId) return;
        setTranscript([]);
        setSuggestions([]);
        setSentiment(null);
        setElapsedMs(0);
        setCloseCode(null);
        startedAtRef.current = null;
    }, [ticket, sessionId]);

    /* Elapsed-time ticker. Only runs while the socket is live. */
    useEffect(() => {
        if (status !== "live") return;
        const t = setInterval(() => {
            if (startedAtRef.current != null) {
                setElapsedMs(Date.now() - startedAtRef.current);
            }
        }, 500);
        return () => clearInterval(t);
    }, [status]);

    /* Fade out cards older than 60s. Done in state so the UI
     * automatically re-renders when a card crosses the threshold. */
    useEffect(() => {
        if (suggestions.length === 0) return;
        const t = setInterval(() => {
            setSuggestions((cur) => {
                const cutoff = Date.now() - 60_000;
                if (cur.every((c) => c.receivedAt >= cutoff)) return cur;
                return cur.filter((c) => c.receivedAt >= cutoff);
            });
        }, 5_000);
        return () => clearInterval(t);
    }, [suggestions.length]);

    const send = useCallback((event: OutboundEvent) => {
        const sock = wsRef.current;
        if (!sock || sock.readyState !== WebSocket.OPEN) return;
        try {
            const stamped =
                event.type === "tag"
                    ? { ...event, timestamp: event.timestamp ?? Date.now() / 1000 }
                    : event;
            sock.send(JSON.stringify(stamped));
        } catch {
            /* silently drop — UI still has its local copy */
        }
    }, []);

    const close = useCallback(() => {
        intentionalCloseRef.current = true;
        if (reconnectTimerRef.current) {
            clearTimeout(reconnectTimerRef.current);
            reconnectTimerRef.current = null;
        }
        const sock = wsRef.current;
        if (sock && sock.readyState !== WebSocket.CLOSED) {
            try {
                sock.close(1000, "client_end");
            } catch {
                /* ignore */
            }
        }
        setStatus("ended");
    }, []);

    return {
        status,
        transcript,
        suggestions,
        sentiment,
        elapsedMs,
        closeCode,
        error,
        send,
        close,
    };
}

/* ── Misc helpers used by the page ───────────────────────────────── */

export function formatElapsed(ms: number): string {
    const total = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
}

export function speakerLabel(speaker: number | string | null): string {
    if (speaker === 0 || speaker === "0") return "Agent";
    if (speaker == null) return "Speaker";
    return `Caller${typeof speaker === "number" ? ` ${speaker}` : ""}`;
}

/** Tailwind border/bg pairing for a suggestion card's severity. Shared
 *  by the /coaching suggestions panel and the embed widget's alert
 *  strip (``/embed/live/[sessionId]``) so the two never drift. */
export function severityClass(severity: SuggestionCard["severity"]): string {
    if (severity === "critical")
        return "border-accent-rose/40 bg-accent-rose/5";
    if (severity === "warn")
        return "border-accent-amber/40 bg-accent-amber/5";
    return "border-border bg-bg-elevated";
}

/** Single-glyph icon for a suggestion card's category. See
 *  ``severityClass`` above for why this lives here instead of the
 *  page component. */
export function categoryIcon(category: string): string {
    switch (category) {
        case "objection":
            return "!";
        case "competitor":
            return "C";
        case "next_step":
        case "next-step":
        case "next-step-required":
            return "→";
        case "compliance":
            return "✓";
        case "sentiment":
        case "sentiment_drop":
            return "♥";
        case "kb":
            return "?";
        case "churn":
            return "⚠";
        case "upsell":
            return "$";
        case "escalation":
            return "↑";
        case "advocate":
            return "★";
        default:
            return "•";
    }
}
