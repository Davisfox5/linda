"use client";

/**
 * Embeddable live-call widget — /embed/live/<sessionId>.
 *
 * Standalone, iframe-friendly surface. NOT part of the (app) route
 * group and NOT gated on Clerk — see ``src/middleware.ts`` where
 * ``/embed`` is added to the public matcher, and ``next.config.mjs``
 * for the ``frame-ancestors *`` header that lets third-party origins
 * frame this route.
 *
 * Auth is a pre-minted single-use WebSocket ticket: the embedding
 * page's own server calls ``POST /api/v1/ws/tickets`` with
 * ``role=monitor`` and passes the result into the iframe src as
 * ``?ticket=``. We hand that straight to ``useLiveSession`` — the
 * same WS-URL builder and event reducer /coaching uses — so this
 * widget and the authenticated app never drift on the wire protocol.
 *
 * Query params:
 *   ticket  (required) single-use monitor-role WS ticket
 *   alerts  "1" renders the coaching/brief-alert banner strip
 *   theme   "light" (default) | "dark"
 */

import { useParams, useSearchParams } from "next/navigation";
import { useRef } from "react";

import {
    categoryIcon,
    severityClass,
    useLiveSession,
    type ConnectionStatus,
    type SuggestionCard,
} from "@/lib/live-coaching";
import { TranscriptView } from "@/components/transcript/transcript-view";

/** Latches ``true`` the first time ``status`` reaches "live" — lets us
 *  tell "never connected" (ticket invalid/expired) apart from
 *  "connected, then the call ended" (still show the transcript we
 *  already have, just banner the terminal state). */
function useHasBeenLive(status: ConnectionStatus): boolean {
    const ref = useRef(false);
    if (status === "live") ref.current = true;
    return ref.current;
}

export default function EmbedLivePage() {
    const params = useParams<{ sessionId: string }>();
    const sessionId = params?.sessionId ?? null;
    const searchParams = useSearchParams();
    const ticket = searchParams.get("ticket");
    const showAlerts = searchParams.get("alerts") === "1";
    const theme = searchParams.get("theme") === "dark" ? "dark" : "light";

    const session = useLiveSession({
        ticket,
        sessionId,
        role: "monitor",
    });

    const hasBeenLive = useHasBeenLive(session.status);
    // "idle" only happens transiently right after mount, before the
    // connect effect flips to "connecting" — treat it as in-flight so
    // we don't flash "unavailable" for one render while a valid
    // ticket is still opening its socket.
    const inFlight =
        session.status === "idle" ||
        session.status === "connecting" ||
        session.status === "live" ||
        session.status === "reconnecting";

    const unavailable = !ticket || !sessionId || (!hasBeenLive && !inFlight);

    return (
        <div
            data-theme={theme}
            className="flex h-screen w-screen flex-col overflow-hidden bg-bg-main text-text"
        >
            {unavailable ? (
                <UnavailableNotice
                    hasTicket={!!ticket && !!sessionId}
                    error={session.error}
                />
            ) : (
                <>
                    {hasBeenLive && !inFlight ? (
                        <StatusBanner status={session.status} />
                    ) : null}
                    {showAlerts ? (
                        <AlertStrip suggestions={session.suggestions} />
                    ) : null}
                    <div className="min-h-0 flex-1">
                        <TranscriptView
                            transcript={session.transcript}
                            heightClassName="h-full"
                            emptyMessage="Waiting for the call to start…"
                        />
                    </div>
                </>
            )}
        </div>
    );
}

function UnavailableNotice({
    hasTicket,
    error,
}: {
    hasTicket: boolean;
    error: string | null;
}) {
    const message = !hasTicket
        ? "This session link is missing a ticket."
        : error ?? "This session is unavailable.";
    return (
        <div className="flex flex-1 items-center justify-center p-6 text-center">
            <div className="max-w-sm space-y-2">
                <p className="text-sm font-medium text-text">
                    Session unavailable
                </p>
                <p className="text-xs text-text-muted">{message}</p>
            </div>
        </div>
    );
}

function StatusBanner({ status }: { status: ConnectionStatus }) {
    const label = status === "ended" ? "Call ended" : "Connection lost";
    return (
        <div className="border-b border-border bg-bg-secondary px-4 py-2 text-center text-xs font-medium text-text-muted">
            {label}
        </div>
    );
}

function AlertStrip({ suggestions }: { suggestions: SuggestionCard[] }) {
    const latest = suggestions.slice(0, 3);
    if (latest.length === 0) return null;
    return (
        <div className="flex flex-col gap-1 border-b border-border bg-bg-secondary px-3 py-2">
            {latest.map((s) => (
                <div
                    key={s.id}
                    className={`flex items-center gap-2 rounded-md border px-2 py-1 text-xs ${severityClass(
                        s.severity,
                    )}`}
                >
                    <span
                        className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-bg-secondary text-[10px] font-semibold text-text-muted"
                        aria-hidden
                    >
                        {categoryIcon(s.category)}
                    </span>
                    <span className="truncate">{s.message}</span>
                </div>
            ))}
        </div>
    );
}
