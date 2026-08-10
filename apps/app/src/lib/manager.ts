"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Domain } from "./me";
import { useApi } from "./api";

// ── Types ─────────────────────────────────────────────────────────────

export interface ManagerNarrative {
    as_of: string | null;
    summary: string;
    top_factors: unknown[];
    confidence: number | null;
    version: number;
    playbook_insights: Record<string, unknown>;
}

// Union of every alert kind across all three motions. Each row carries
// its own ``domain`` so the Manager portal can filter to the active
// tab and the Journey view can mix them.
export type AlertKind =
    | "topic_spike"
    | "sentiment_drop"
    | "churn_surge"
    | "methodology_drop"
    | "renewal_risk_spike"
    | "health_score_drop"
    | "csat_drop_support"
    | "escalation_surge"
    | "ttr_drift"
    | "recurring_issue_detected"
    | "broken_commitment_detected"
    | "sales_trend_detected"
    | "cs_trend_detected"
    | "customer_concern_trend_detected"
    | "campaign_bounce_spike"
    | "campaign_optout_spike"
    | "campaign_no_engagement"
    | "campaign_stalled"
    | "campaign_quota_starved"
    | "campaign_completed_summary";

export type Severity = "high" | "medium" | "low";

export interface ManagerAlert {
    id: string;
    kind: AlertKind;
    severity: Severity;
    title: string;
    body: string | null;
    evidence: Record<string, unknown>;
    domain: Domain | null;
    opened_at: string;
    acknowledged_at: string | null;
    dismissed_at: string | null;
    resolved_at: string | null;
}

export type RecommendationCategory =
    // Sales
    | "coach_rep"
    | "run_campaign"
    | "outreach_at_risk_customer"
    | "promote_winning_script"
    // CS
    | "schedule_qbr"
    | "flag_renewal_risk"
    | "assign_expansion_play"
    | "coach_csm"
    // Support
    | "update_kb_article"
    | "route_to_specialist"
    | "coach_support_agent"
    | "escalate_recurring_issue"
    // Predictive / cohort-derived
    | "prevent_no_touch_churn"
    | "prevent_lead_stall"
    | "proactive_outreach_repeat_support"
    // AI cross-customer trend detector
    | "address_recurring_issue";

// Dynamic per-section content composed onto a customer-targeted
// recommendation. ``kind`` is one of a known set (situation, why_now,
// play, talking_points, draft_message, watch_out, evidence, playbook,
// commitments, success) but new kinds may appear over time — rendering
// must stay generic and never switch per-kind for layout.
export interface BriefSection {
    kind: string;
    title: string;
    body?: string;
    items?: string[];
}

export interface RecommendationBrief {
    headline: string;
    sections: BriefSection[];
}

export interface ManagerRecommendation {
    id: string;
    category: RecommendationCategory;
    title: string;
    rationale: string | null;
    evidence: Record<string, unknown>;
    target: Record<string, unknown>;
    score: number;
    status: "open" | "applied" | "dismissed" | "expired";
    domain: Domain | null;
    applied_artifact_type: string | null;
    applied_artifact_id: string | null;
    expires_at: string;
    created_at: string;
    brief: RecommendationBrief | null;
    enriched_at: string | null;
}

export interface AlertConfig {
    inapp_enabled: boolean;
    slack_enabled: boolean;
    slack_min_severity: Severity;
    topic_spike_pct_change_threshold: number | null;
    topic_spike_min_volume: number | null;
    sentiment_drop_threshold: number | null;
    churn_surge_multiplier: number | null;
    methodology_drop_threshold: number | null;
}

export interface TrainingGapRow {
    rep_id: string | null;
    rep_name: string | null;
    call_count: number;
    reflection_rate: number | null;
    open_question_rate: number | null;
    avg_methodology_coverage: number | null;
}

export interface TrainingGapReport {
    window_days: number;
    rows: TrainingGapRow[];
}

// ── Journey (cross-motion) ────────────────────────────────────────────

export interface JourneyHandoffRow {
    transition: "sales_to_cs" | "cs_to_support" | "support_to_renewal";
    customer_count: number;
    avg_days_stalled: number | null;
}

export interface JourneyAccountRow {
    customer_id: string;
    customer_name: string;
    last_sales_at: string | null;
    last_cs_at: string | null;
    last_support_at: string | null;
    open_support_cases: number;
    health_signal: "high" | "medium" | "low" | "none" | null;
}

export interface JourneyReport {
    handoffs: JourneyHandoffRow[];
    accounts_at_risk: JourneyAccountRow[];
    motions_seen: Domain[];
}

// ── Hooks ─────────────────────────────────────────────────────────────

// Optional ``domain`` on every list hook: pass it to scope a Manager
// tab to one motion; omit it on the Journey view where we want
// everything.

export function useManagerNarrative() {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "narrative"],
        queryFn: () => api.get<ManagerNarrative>("/manager/narrative"),
        refetchOnWindowFocus: false,
    });
}

export function useRefreshNarrative() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: () => api.post<{ enqueued: boolean }>("/manager/narrative/refresh"),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "narrative"] });
        },
    });
}

export function useManagerAlerts(
    opts: { onlyOpen?: boolean; domain?: Domain } = {},
) {
    const api = useApi();
    const onlyOpen = opts.onlyOpen ?? true;
    const domain = opts.domain;
    return useQuery({
        queryKey: ["manager", "alerts", { onlyOpen, domain }],
        queryFn: () => {
            const params = new URLSearchParams({ only_open: String(onlyOpen) });
            if (domain) params.set("domain", domain);
            return api.get<ManagerAlert[]>(`/manager/alerts?${params.toString()}`);
        },
        // 5 min — alerts also push over SSE; this poll is a safety net.
        refetchInterval: 5 * 60_000,
    });
}

export function useAcknowledgeAlert() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (alertId: string) =>
            api.post(`/manager/alerts/${alertId}/acknowledge`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "alerts"] });
        },
    });
}

export function useDismissAlert() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
            api.post(`/manager/alerts/${id}/dismiss`, { reason }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "alerts"] });
        },
    });
}

export function useManagerRecommendations(
    status: string = "open",
    opts: { domain?: Domain } = {},
) {
    const api = useApi();
    const domain = opts.domain;
    return useQuery({
        queryKey: ["manager", "recommendations", { status, domain }],
        queryFn: () => {
            const params = new URLSearchParams({ status });
            if (domain) params.set("domain", domain);
            return api.get<ManagerRecommendation[]>(
                `/manager/recommendations?${params.toString()}`,
            );
        },
        refetchOnWindowFocus: false,
    });
}

export interface ApplyResult {
    artifact_type: string;
    artifact_id: string;
}

export function useApplyRecommendation() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (id: string) =>
            api.post<ApplyResult>(`/manager/recommendations/${id}/apply`),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "recommendations"] });
        },
    });
}

export function useDismissRecommendation() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
            api.post(`/manager/recommendations/${id}/dismiss`, { reason }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "recommendations"] });
        },
    });
}

export function useAlertConfig() {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "alert-config"],
        queryFn: () => api.get<AlertConfig>("/manager/alert-config"),
    });
}

export function useUpdateAlertConfig() {
    const api = useApi();
    const qc = useQueryClient();
    return useMutation({
        mutationFn: (patch: Partial<AlertConfig>) =>
            api.put<AlertConfig>("/manager/alert-config", patch),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ["manager", "alert-config"] });
        },
    });
}

export function useTrainingGap(windowDays: number) {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "training-gap", windowDays],
        queryFn: () =>
            api.get<TrainingGapReport>(
                `/manager/training-gap?window_days=${windowDays}`,
            ),
    });
}

export function useJourney(windowDays: number = 90) {
    const api = useApi();
    return useQuery({
        queryKey: ["manager", "journey", windowDays],
        queryFn: () =>
            api.get<JourneyReport>(`/manager/journey?window_days=${windowDays}`),
        refetchOnWindowFocus: false,
    });
}

// ── Vocabulary helpers ────────────────────────────────────────────────

// Human-readable labels for the domain tabs and the recommendation
// categories. Keep here so the page components don't drift.

export const DOMAIN_LABEL: Record<Domain, string> = {
    sales: "Sales",
    customer_service: "Customer Success",
    it_support: "IT Support",
    generic: "General",
};

export const CATEGORY_LABEL: Record<RecommendationCategory, string> = {
    coach_rep: "Coach a rep",
    run_campaign: "Run a campaign",
    outreach_at_risk_customer: "Reach out to at-risk customer",
    promote_winning_script: "Promote a winning script",
    schedule_qbr: "Schedule a QBR",
    flag_renewal_risk: "Flag a renewal risk",
    assign_expansion_play: "Assign an expansion play",
    coach_csm: "Coach a CSM",
    update_kb_article: "Update a KB article",
    route_to_specialist: "Route to a specialist",
    coach_support_agent: "Coach a support agent",
    escalate_recurring_issue: "Escalate a recurring issue",
    prevent_no_touch_churn: "Reach out before renewal",
    prevent_lead_stall: "Re-engage warm prospect",
    proactive_outreach_repeat_support: "Check in on repeat customer",
    address_recurring_issue: "Investigate recurring issue",
};

export const ALERT_KIND_LABEL: Record<AlertKind, string> = {
    topic_spike: "Topic spike",
    sentiment_drop: "Sentiment drop",
    churn_surge: "Churn surge",
    methodology_drop: "Methodology drop",
    renewal_risk_spike: "Renewal risk spike",
    health_score_drop: "Account health drop",
    csat_drop_support: "CSAT drop",
    escalation_surge: "Escalation surge",
    ttr_drift: "Time to resolve drift",
    recurring_issue_detected: "Recurring issue",
    broken_commitment_detected: "Broken commitment",
    sales_trend_detected: "Sales trend",
    cs_trend_detected: "CS trend",
    customer_concern_trend_detected: "Customer concern trend",
    campaign_bounce_spike: "Campaign bounce spike",
    campaign_optout_spike: "Campaign opt-out spike",
    campaign_no_engagement: "Campaign not engaging",
    campaign_stalled: "Campaign stalled",
    campaign_quota_starved: "Campaign quota-starved",
    campaign_completed_summary: "Campaign wrap-up",
};
