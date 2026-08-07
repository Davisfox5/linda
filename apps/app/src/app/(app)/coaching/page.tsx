"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
    useEffect,
    useMemo,
    useRef,
    useState,
    type FormEvent,
} from "react";

import {
    categoryIcon,
    formatElapsed,
    severityClass,
    useCoachingSessions,
    useLiveSession,
    useMintTicket,
    type CoachingSessionRow,
    type ConnectionStatus,
    type OutboundTagEvent,
    type SuggestionCard,
    type TicketResponse,
    type TranscriptLine,
} from "@/lib/live-coaching";
import { TranscriptView } from "@/components/transcript/transcript-view";
import { useInteractions } from "@/lib/interactions";
import { useApi } from "@/lib/api";
import { useMe } from "@/lib/me";
import { useQuery } from "@tanstack/react-query";

type InteractionSource = "live_phone" | "live_web" | "replay";
type TagCategory = OutboundTagEvent["category"];

interface UserLookupRow {
    id: string;
    name: string | null;
}

const SOURCE_LABELS: Record<InteractionSource, string> = {
    live_phone: "Live phone call",
    live_web: "Live web call",
    replay: "Replay an interaction",
};

const TAG_BUTTONS: { key: TagCategory; label: string }[] = [
    { key: "question", label: "Question" },
    { key: "coaching", label: "Coaching" },
    { key: "praise", label: "Praise" },
    { key: "issue", label: "Issue" },
];

function useUsersLookup() {
    const api = useApi();
    return useQuery({
        queryKey: ["users-lookup"],
        queryFn: () => api.get<UserLookupRow[]>("/users/lookup"),
    });
}

export default function CoachingPage() {
    const me = useMe();
    const ticketMutation = useMintTicket();
    const [issued, setIssued] = useState<TicketResponse | null>(null);
    const [agentName, setAgentName] = useState<string | null>(null);

    // Screen-pop deep link: /live/<sessionId> bounces here with
    // ?sessionId=<id> so dialers/CRMs can pop straight into the
    // monitor view for a specific call without walking the picker
    // form below. We mint the monitor ticket ourselves once /me has
    // resolved (monitor tickets require a manager/admin user_id) and
    // only attempt it once per sessionId.
    const searchParams = useSearchParams();
    const deepLinkSessionId = searchParams.get("sessionId");
    const autoJoinedRef = useRef<string | null>(null);
    const meUserId = me.data?.user?.id;

    useEffect(() => {
        if (!deepLinkSessionId || issued) return;
        if (me.isLoading || !meUserId) return;
        if (autoJoinedRef.current === deepLinkSessionId) return;
        autoJoinedRef.current = deepLinkSessionId;
        ticketMutation
            .mutateAsync({
                role: "monitor",
                session_id: deepLinkSessionId,
                user_id: meUserId,
            })
            .then((out) => {
                setIssued(out);
                setAgentName(null);
            })
            .catch(() => {
                // Surfaced to the user via ticketMutation.error in
                // IdleLayout, same as the manual "Start" flow.
            });
    }, [deepLinkSessionId, issued, me.isLoading, meUserId, ticketMutation]);

    const session = useLiveSession({
        ticket: issued?.ticket ?? null,
        sessionId: issued?.session_id ?? null,
        // /coaching is manager+; we always open the monitor side of the
        // bidi channel so the manager observes whatever room the agent
        // is in. The form's "interaction source" picker only governs
        // *which* session we're observing, not the role we connect as.
        role: "monitor",
    });

    const isActive =
        !!issued &&
        (session.status === "connecting" ||
            session.status === "live" ||
            session.status === "reconnecting");

    return (
        <div className="space-y-6">
            <header className="flex flex-wrap items-end justify-between gap-3">
                <div>
                    <h2 className="text-2xl font-bold">Live Coaching</h2>
                    <p className="text-text-muted mt-1">
                        Listen in on a live call, watch Linda&apos;s suggestions
                        in real time, and tag moments worth a follow-up.
                    </p>
                </div>
                {isActive ? (
                    <SessionHeaderStrip
                        status={session.status}
                        elapsedMs={session.elapsedMs}
                        agentName={agentName}
                        onEnd={() => {
                            session.close();
                        }}
                    />
                ) : null}
            </header>

            {!isActive ? (
                <IdleLayout
                    pending={ticketMutation.isPending}
                    error={ticketMutation.error}
                    sessionStatus={session.status}
                    sessionError={session.error}
                    me={me.data}
                    onStart={async ({ agentId, agentDisplay, source, interactionId }) => {
                        const userId = me.data?.user?.id;
                        const payload: {
                            role: "monitor";
                            user_id?: string;
                            session_id?: string;
                        } = { role: "monitor" };
                        if (userId) payload.user_id = userId;
                        if (source === "replay" && interactionId) {
                            // Replay sessions reuse the interaction id as
                            // the session id so the manager joins the
                            // call's own monitor channel rather than a
                            // fresh empty one.
                            payload.session_id = interactionId;
                        }
                        const out = await ticketMutation.mutateAsync(payload);
                        setIssued(out);
                        setAgentName(agentDisplay ?? agentId);
                    }}
                    onClearExpired={() => setIssued(null)}
                />
            ) : (
                <ActiveLayout
                    transcript={session.transcript}
                    suggestions={session.suggestions}
                    onSendTag={(category, note) =>
                        session.send({ type: "tag", category, note })
                    }
                />
            )}

            {/* Metric drilldowns. dashboard KPI cards deep-link here via
                hash anchors (#metric-sentiment / -qa / -rapport /
                -talk-listen). Plain-language explanation of each metric
                + how it's calculated; the live-coaching widgets above
                use these signals, so seeing the math helps reps trust
                the numbers. */}
            <MetricsDrilldown />
        </div>
    );
}

function MetricsDrilldown() {
    return (
        <section className="mt-10 space-y-8">
            <header>
                <h3 className="text-lg font-semibold">Metric reference</h3>
                <p className="mt-1 text-sm text-text-muted">
                    The dashboard KPIs link straight to these sections.
                    Each metric: what it measures, how Linda calculates
                    it, and what to do with it.
                </p>
            </header>

            <MetricBlock
                id="metric-sentiment"
                title="Average sentiment"
                scale="0-10 scale"
                description="How positive the customer's tone is on a call, scored 0 (frustrated, hostile) to 10 (delighted, advocating). The number on the dashboard is the simple mean across every analyzed call in the selected period."
                calculation={[
                    "Linda passes the full transcript to Claude with an instruction to score the customer's overall emotional valence on a 0-to-10 scale, calibrated against a small reference set of labeled calls.",
                    "Calls where the customer barely speaks (one-sided rep monologue) are still scored but with lower confidence. those get filtered out of the trend line if the customer-speech duration is under 10% of the call.",
                ]}
                howToUse={[
                    "Spotting drift: a 3-week downward slope is a stronger signal than any single call. Use the Trends chart for this.",
                    "Don't optimize for the number itself. chasing high sentiment can lead reps to avoid hard conversations. Customers giving you bad news is a HEALTHY signal, not a bad one.",
                ]}
            />

            <MetricBlock
                id="metric-qa"
                title="QA score"
                scale="0-100 scale"
                description="How well each call performs against your team's custom scorecards. Each scorecard is a weighted rubric (e.g. 'opened with permission' / 'recapped at the end') and every call gets one composite score."
                calculation={[
                    "Each scorecard rubric criterion is scored individually (0-5 typically) by Linda based on the transcript. Criteria are weight-multiplied and summed, then normalized to a 0-100 scale.",
                    "Calls without an applicable scorecard show as null. the metric average on the dashboard skips them. If your dashboard shows '-' for QA score, you likely haven't configured a scorecard yet.",
                ]}
                howToUse={[
                    "Coach to the criterion, not the total: 'your discovery score is low' is actionable; 'your QA score is low' is not.",
                    "Calibrate scorecards monthly. If 90% of calls score above 80, the scorecard isn't discriminating. make the criteria harder.",
                ]}
            />

            <MetricBlock
                id="metric-rapport"
                title="Rapport (LSM)"
                scale="0-100 scale (rescaled from 0-1)"
                description="Linguistic Style Matching. a measure of how much the rep mirrored the customer's word choice and rhythm. High LSM correlates with conversational connection and is one of the strongest predictors of repeat business."
                calculation={[
                    "We tokenize both speakers' turns and compute, for each function-word category (pronouns, articles, conjunctions, etc.), how often each speaker uses words in that category. LSM = 1 - normalized absolute difference, averaged across categories.",
                    "Result is 0 to 1; we display 0 to 100 for legibility. Single-speaker calls (earnings-call monologues) don't generate a score. they need two-sided dialogue.",
                ]}
                howToUse={[
                    "Below 60: rep is talking past the customer (using different vocabulary, formality). Coach toward active mirroring.",
                    "Above 85: very strong rapport. Cross-reference with sentiment. if rapport is high and sentiment is low, the customer is COMFORTABLE telling the rep bad news, which is its own kind of trust signal.",
                ]}
            />

            <MetricBlock
                id="metric-talk-listen"
                title="Talk %"
                scale="0-100% (rep share of speaking time)"
                description="What fraction of the call the rep was speaking, averaged across all reps in the selected period. Industry benchmarks for sales discovery: 40-55% rep is healthy; over 65% usually correlates with worse outcomes."
                calculation={[
                    "We diarize the call (separate speaker tracks) and compute the rep's total speaking seconds divided by total call seconds (excluding silence). Aggregation is a simple mean across calls.",
                    "Inbound support calls naturally skew higher rep-talk (rep is delivering information). The benchmark of 40-55% applies to sales discovery; calibrate to your context.",
                ]}
                howToUse={[
                    "Watch the per-rep distribution, not the tenant average. A single rep at 75% drags the team mean. coach that rep specifically.",
                    "Pair with sentiment: high talk % + low sentiment = rep is steamrolling. High talk % + high sentiment = customer was already sold and just wanted info.",
                ]}
            />
        </section>
    );
}

function MetricBlock({
    id,
    title,
    scale,
    description,
    calculation,
    howToUse,
}: {
    id: string;
    title: string;
    scale: string;
    description: string;
    calculation: string[];
    howToUse: string[];
}) {
    return (
        <article
            id={id}
            // ``scroll-mt-20`` keeps the anchor target clear of the
            // top-of-page sticky nav when the user lands here from a
            // KPI-card hash link.
            className="scroll-mt-20 rounded-lg border border-border bg-bg-card p-6"
        >
            <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border pb-3">
                <h4 className="text-base font-semibold">{title}</h4>
                <span className="text-xs uppercase tracking-wide text-text-subtle">
                    {scale}
                </span>
            </header>
            <p className="mt-3 text-sm text-text-muted">{description}</p>
            <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
                <div>
                    <h5 className="text-xs font-semibold uppercase tracking-wide text-text-subtle">
                        How it&apos;s calculated
                    </h5>
                    <div className="mt-2 space-y-2 text-sm text-text-muted">
                        {calculation.map((p, i) => (
                            <p key={i}>{p}</p>
                        ))}
                    </div>
                </div>
                <div>
                    <h5 className="text-xs font-semibold uppercase tracking-wide text-text-subtle">
                        How to use it
                    </h5>
                    <div className="mt-2 space-y-2 text-sm text-text-muted">
                        {howToUse.map((p, i) => (
                            <p key={i}>{p}</p>
                        ))}
                    </div>
                </div>
            </div>
        </article>
    );
}

/* ── Idle layout ─────────────────────────────────────────────────── */

interface IdleProps {
    pending: boolean;
    error: unknown;
    sessionStatus: ConnectionStatus;
    sessionError: string | null;
    me: ReturnType<typeof useMe>["data"];
    onStart: (args: {
        agentId: string;
        agentDisplay: string | null;
        source: InteractionSource;
        interactionId?: string;
    }) => Promise<void> | void;
    onClearExpired: () => void;
}

function IdleLayout(props: IdleProps) {
    const users = useUsersLookup();
    const [agentId, setAgentId] = useState("");
    const [source, setSource] = useState<InteractionSource>("live_phone");
    const [interactionId, setInteractionId] = useState("");
    const interactionsQuery = useInteractions({ limit: 25 });
    const sessions = useCoachingSessions(15);

    const showReplayPicker = source === "replay";
    const replayInteractions = useMemo(
        () => (interactionsQuery.data ?? []).filter((i) => i.channel === "voice"),
        [interactionsQuery.data],
    );

    const canStart =
        agentId.length > 0 &&
        (source !== "replay" || interactionId.length > 0) &&
        !props.pending;

    const submit = (e: FormEvent) => {
        e.preventDefault();
        const display =
            users.data?.find((u) => u.id === agentId)?.name ?? null;
        props.onStart({
            agentId,
            agentDisplay: display,
            source,
            interactionId: showReplayPicker ? interactionId : undefined,
        });
    };

    return (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <section className="rounded-lg border border-border bg-bg-card p-5 space-y-4">
                <div>
                    <h3 className="text-lg font-semibold">
                        Start a coaching session.
                    </h3>
                    <p className="text-sm text-text-muted mt-1">
                        Pick an agent and an interaction source. Linda
                        will start streaming once the WebSocket opens.
                    </p>
                </div>

                <form onSubmit={submit} className="space-y-3">
                    <label className="block text-sm space-y-1">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Agent
                        </span>
                        <select
                            value={agentId}
                            onChange={(e) => setAgentId(e.target.value)}
                            className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                            required
                        >
                            <option value="">Select an agent…</option>
                            {(users.data ?? []).map((u) => (
                                <option key={u.id} value={u.id}>
                                    {u.name ?? u.id}
                                </option>
                            ))}
                        </select>
                    </label>

                    <label className="block text-sm space-y-1">
                        <span className="text-xs uppercase tracking-wide text-text-subtle">
                            Interaction source
                        </span>
                        <select
                            value={source}
                            onChange={(e) =>
                                setSource(e.target.value as InteractionSource)
                            }
                            className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                        >
                            {Object.entries(SOURCE_LABELS).map(([k, label]) => (
                                <option key={k} value={k}>
                                    {label}
                                </option>
                            ))}
                        </select>
                    </label>

                    {showReplayPicker ? (
                        <label className="block text-sm space-y-1">
                            <span className="text-xs uppercase tracking-wide text-text-subtle">
                                Interaction
                            </span>
                            <select
                                value={interactionId}
                                onChange={(e) =>
                                    setInteractionId(e.target.value)
                                }
                                className="w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                                required
                            >
                                <option value="">Pick an interaction…</option>
                                {replayInteractions.map((i) => (
                                    <option key={i.id} value={i.id}>
                                        {i.title ??
                                            `Call from ${new Date(i.created_at).toLocaleString()}`}
                                    </option>
                                ))}
                            </select>
                        </label>
                    ) : null}

                    <button
                        type="submit"
                        disabled={!canStart}
                        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover disabled:opacity-50"
                    >
                        {props.pending ? "Starting…" : "Start"}
                    </button>
                </form>

                {props.error ? (
                    <div className="rounded-md border border-accent-rose/40 bg-accent-rose/10 p-3 text-xs text-accent-rose">
                        Couldn&apos;t mint a ticket:{" "}
                        {(props.error as Error).message}
                    </div>
                ) : null}

                {props.sessionStatus === "expired" ? (
                    <div className="rounded-md border border-accent-amber/40 bg-accent-amber/10 p-3 text-xs text-accent-amber space-y-2">
                        <div className="font-medium">Session expired.</div>
                        <p>
                            The ticket Linda issued has been consumed or
                            timed out. Click below to mint a fresh one and
                            try again.
                        </p>
                        <button
                            type="button"
                            onClick={props.onClearExpired}
                            className="rounded-md border border-accent-amber/40 px-3 py-1 hover:bg-accent-amber/10"
                        >
                            Get new ticket
                        </button>
                    </div>
                ) : null}

                {props.sessionStatus === "error" && props.sessionError ? (
                    <div className="rounded-md border border-accent-rose/40 bg-accent-rose/10 p-3 text-xs text-accent-rose">
                        {props.sessionError}
                    </div>
                ) : null}
            </section>

            <section className="rounded-lg border border-border bg-bg-card overflow-hidden">
                <div className="px-5 py-4 border-b border-border flex items-baseline justify-between">
                    <h3 className="text-lg font-semibold">Recent sessions</h3>
                    <span className="text-xs text-text-subtle">
                        {sessions.data?.total ?? 0} total
                    </span>
                </div>
                {sessions.isLoading && !sessions.data ? (
                    <div className="px-5 py-4 text-sm text-text-muted">
                        Loading…
                    </div>
                ) : sessions.error ? (
                    <div className="px-5 py-4 text-sm text-accent-rose">
                        Couldn&apos;t load sessions:{" "}
                        {(sessions.error as Error).message}
                    </div>
                ) : (sessions.data?.items?.length ?? 0) === 0 ? (
                    <div className="px-5 py-6 text-sm text-text-muted">
                        No coaching sessions yet. start one to the left
                        and the recent list will fill in.
                    </div>
                ) : (
                    <ul className="divide-y divide-border">
                        {sessions.data!.items.map((s) => (
                            <SessionRow key={s.id} session={s} />
                        ))}
                    </ul>
                )}
            </section>
        </div>
    );
}

function SessionRow({ session }: { session: CoachingSessionRow }) {
    const dt = new Date(session.started_at);
    const dur =
        session.duration_seconds != null
            ? `${Math.floor(session.duration_seconds / 60)}m ${session.duration_seconds % 60}s`
            : session.status === "active"
              ? "live"
              : "-";
    return (
        <li className="px-5 py-3 flex items-center justify-between gap-4">
            <div className="min-w-0">
                <div className="text-sm font-medium truncate">
                    {session.interaction_title ??
                        session.agent_name ??
                        "Coaching session"}
                </div>
                <div className="text-xs text-text-muted">
                    {session.agent_name ? `${session.agent_name} • ` : ""}
                    {dt.toLocaleString()} • {dur}
                </div>
            </div>
            <div className="flex items-center gap-2">
                <StatusPill status={session.status} />
                {session.interaction_id ? (
                    <Link
                        href={`/interactions/${session.interaction_id}`}
                        className="rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-secondary"
                    >
                        Open call
                    </Link>
                ) : null}
            </div>
        </li>
    );
}

function StatusPill({ status }: { status: string }) {
    const tone =
        status === "active"
            ? "bg-accent-emerald/10 text-accent-emerald border-accent-emerald/30"
            : status === "completed"
              ? "bg-bg-secondary text-text-muted border-border"
              : "bg-bg-secondary text-text-subtle border-border";
    return (
        <span
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${tone}`}
        >
            {status}
        </span>
    );
}

/* ── Active layout ───────────────────────────────────────────────── */

interface ActiveProps {
    transcript: TranscriptLine[];
    suggestions: SuggestionCard[];
    onSendTag: (category: TagCategory, note: string) => void;
}

function ActiveLayout(props: ActiveProps) {
    return (
        <div className="grid gap-4 md:grid-cols-5">
            <TranscriptPanel transcript={props.transcript} />
            <SuggestionsPanel suggestions={props.suggestions} />
            <div className="md:col-span-5">
                <TaggingComposer onSend={props.onSendTag} />
            </div>
        </div>
    );
}

function TranscriptPanel({ transcript }: { transcript: TranscriptLine[] }) {
    return (
        <section className="md:col-span-3 rounded-lg border border-border bg-bg-card overflow-hidden">
            <div className="px-4 py-2 border-b border-border flex items-center justify-between">
                <h3 className="text-sm font-semibold">Transcript</h3>
            </div>
            {/* Scroll/autoscroll/jump-to-live + speaker-labeled line
                rendering live in the shared component so the embed
                widget (/embed/live/[sessionId]) doesn't reimplement
                it. */}
            <TranscriptView transcript={transcript} />
        </section>
    );
}

function SuggestionsPanel({ suggestions }: { suggestions: SuggestionCard[] }) {
    return (
        <section className="md:col-span-2 rounded-lg border border-border bg-bg-card overflow-hidden">
            <div className="px-4 py-2 border-b border-border flex items-center justify-between">
                <h3 className="text-sm font-semibold">Live suggestions</h3>
                <span className="text-xs text-text-subtle">
                    {suggestions.length} active
                </span>
            </div>
            <div className="h-[60vh] overflow-y-auto px-3 py-3 space-y-2">
                {suggestions.length === 0 ? (
                    <div className="text-text-muted px-1 py-2 text-sm">
                        No suggestions yet. Linda will surface them as
                        the call progresses.
                    </div>
                ) : (
                    suggestions.map((s) => (
                        <SuggestionCardView key={s.id} card={s} />
                    ))
                )}
            </div>
        </section>
    );
}

function SuggestionCardView({ card }: { card: SuggestionCard }) {
    const ageSec = Math.max(0, Math.floor((Date.now() - card.receivedAt) / 1000));
    const fade = ageSec > 50 ? "opacity-50" : "";

    return (
        <article
            className={`rounded-md border px-3 py-2 transition-opacity ${severityClass(
                card.severity,
            )} ${fade}`}
        >
            <div className="flex items-start gap-2">
                <span
                    className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-bg-secondary text-xs font-semibold text-text-muted"
                    aria-hidden
                >
                    {categoryIcon(card.category)}
                </span>
                <div className="min-w-0 flex-1">
                    <div className="text-xs uppercase tracking-wide text-text-subtle">
                        {card.category.replace(/_/g, " ")}
                    </div>
                    <p className="text-sm mt-0.5">{card.message}</p>
                    {card.sourceDocTitle ? (
                        <p className="text-xs text-text-muted mt-1">
                            Source: {card.sourceDocTitle}
                        </p>
                    ) : null}
                </div>
            </div>
        </article>
    );
}

/* ── Footer composer ─────────────────────────────────────────────── */

function TaggingComposer({
    onSend,
}: {
    onSend: (category: TagCategory, note: string) => void;
}) {
    const [note, setNote] = useState("");
    const [pending, setPending] = useState<TagCategory | null>(null);
    const submit = (category: TagCategory) => {
        const trimmed = note.trim();
        if (!trimmed) return;
        onSend(category, trimmed);
        setNote("");
        setPending(category);
        setTimeout(() => setPending(null), 600);
    };
    return (
        <section className="rounded-lg border border-border bg-bg-card p-4">
            <div className="text-xs uppercase tracking-wide text-text-subtle mb-2">
                Quick tag
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <input
                    type="text"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Note this moment for the post-call review…"
                    className="flex-1 rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm"
                />
                <div className="flex flex-wrap gap-2">
                    {TAG_BUTTONS.map((b) => (
                        <button
                            key={b.key}
                            type="button"
                            onClick={() => submit(b.key)}
                            disabled={!note.trim() || pending === b.key}
                            className="rounded-md border border-border px-3 py-2 text-sm hover:bg-bg-secondary disabled:opacity-50"
                        >
                            {pending === b.key ? "Sent" : b.label}
                        </button>
                    ))}
                </div>
            </div>
        </section>
    );
}

/* ── Header strip ────────────────────────────────────────────────── */

function SessionHeaderStrip({
    status,
    elapsedMs,
    agentName,
    onEnd,
}: {
    status: ConnectionStatus;
    elapsedMs: number;
    agentName: string | null;
    onEnd: () => void;
}) {
    const tone =
        status === "live"
            ? "bg-accent-emerald/10 text-accent-emerald border-accent-emerald/30"
            : status === "connecting"
              ? "bg-bg-secondary text-text-muted border-border"
              : status === "reconnecting"
                ? "bg-accent-amber/10 text-accent-amber border-accent-amber/30"
                : "bg-bg-secondary text-text-muted border-border";
    return (
        <div className="flex items-center gap-3 text-sm">
            <span
                className={`inline-flex items-center rounded-full border px-3 py-0.5 text-xs font-medium capitalize ${tone}`}
            >
                {status}
            </span>
            <span className="text-text-muted">
                {formatElapsed(elapsedMs)} elapsed
            </span>
            {agentName ? (
                <span className="text-text-muted">• {agentName}</span>
            ) : null}
            <button
                type="button"
                onClick={onEnd}
                className="rounded-md border border-accent-rose/40 px-3 py-1 text-xs text-accent-rose hover:bg-accent-rose/10"
            >
                End session
            </button>
        </div>
    );
}
