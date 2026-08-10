"""Catalog of outbound webhook event names.

Kept separate from the dispatcher so services can import constants without
pulling the delivery machinery. Every event the platform emits should land
here with a short description — the admin UI reads this list to render
the "which events should this webhook receive?" picker.
"""

from __future__ import annotations

from typing import Dict, List


# event_name → description. Order matters for the picker — group by domain.
WEBHOOK_EVENTS: Dict[str, str] = {
    # Interactions
    "interaction.analyzed":        "AI analysis finished for a call / transcript / email.",
    "interaction.outcome_inferred": "Call outcome (won/lost/resolved/…) classified.",

    # Customer lifecycle (maps 1:1 with CustomerOutcomeEvent.event_type)
    "customer.became_customer":  "A prospect just closed as a new customer.",
    "customer.upsold":           "Existing customer expanded their contract.",
    "customer.renewed":          "Existing customer renewed.",
    "customer.churned":          "Customer churned.",
    "customer.at_risk_flagged":  "Churn risk just fired on a call.",
    "customer.advocate_signal":  "Customer showed strong advocate signal.",
    "customer.escalation":       "Caller asked to escalate mid-call.",
    "customer.satisfaction_change": "CSAT / NPS delta recorded.",
    "customer_brief.updated":    "LINDA's per-customer brief was rebuilt.",

    # Live-call signals (mirrors WebSocket brief_alert kinds for external
    # integrations like Slack/Jira that subscribe to the same events).
    "brief_alert.churn":           "Live call: new churn signal.",
    "brief_alert.upsell":          "Live call: upsell opportunity.",
    "brief_alert.escalation":      "Live call: caller requested escalation.",
    "brief_alert.advocate":        "Live call: advocate moment.",
    "brief_alert.sentiment_drop":  "Live call: sentiment dropped sharply.",

    # Tenant brief
    "tenant_brief.suggestion_created":  "Infer-From-Sources proposed an update.",
    "tenant_brief.updated":             "Tenant brief was rebuilt.",

    # KB
    "kb.document_uploaded":     "A new KB document was ingested.",

    # Action items
    "action_item.created":        "A new action item was created.",
    "action_item.completed":      "An action item was marked done.",
    "action_item.dismissed":      "An action item was dismissed.",

    # Action plans
    "action_plan.created":            "A new action plan was created.",
    "action_plan.updated":            "An action plan's goal/status/steps changed.",
    "action_plan.step_completed":     "An action-plan step was marked complete.",
    "action_plan.step_skipped":       "An action-plan step was skipped.",
    "action_plan.completed":          "An action plan reached its customer endpoint.",

    # Cold outreach (LINDA-originated campaigns; see docs/webhooks.md)
    "outreach.email.sent":       "An outreach campaign email was sent to a prospect.",
    "outreach.email.replied":    "A prospect replied to an outreach send.",
    "outreach.email.bounced":    "An outreach send bounced (DSN detected).",
    "outreach.email.opted_out":  "A prospect opted out (stop reply or manual DNC).",
    "outreach.link_clicked":     "A prospect clicked a tracked link in an outreach email.",
    "prospect.status_changed":   "A prospect moved in the outreach pipeline.",
    "campaign.completed":        "An outreach campaign has no actionable members left.",

    # Support cases
    "support_case.opened":            "A new support case was opened.",
    "support_case.status_changed":    "A support case transitioned (open / in_progress / resolved / closed / escalated).",
    "support_case.assigned":          "A support case was assigned (or unassigned).",

    # Manager alerts
    "manager_alert.created": "A manager alert opened (anomaly scan, trend detector, or campaign monitor).",

    # System
    "webhook.test":             "Manual test ping from the admin UI.",
}


def all_event_names() -> List[str]:
    """List of supported event names for UI pickers."""
    return list(WEBHOOK_EVENTS.keys())


def is_known_event(name: str) -> bool:
    return name in WEBHOOK_EVENTS or name == "*"


# Mapping from CustomerOutcomeEvent.event_type → webhook event.
CUSTOMER_OUTCOME_EVENT_MAP: Dict[str, str] = {
    "became_customer":     "customer.became_customer",
    "upsold":              "customer.upsold",
    "renewed":             "customer.renewed",
    "churned":             "customer.churned",
    "at_risk_flagged":     "customer.at_risk_flagged",
    "advocate_signal":     "customer.advocate_signal",
    "escalation":          "customer.escalation",
    "satisfaction_change": "customer.satisfaction_change",
}


# Mapping from WebSocket brief_alert.kind → webhook event.
BRIEF_ALERT_EVENT_MAP: Dict[str, str] = {
    "churn":          "brief_alert.churn",
    "upsell":         "brief_alert.upsell",
    "escalation":     "brief_alert.escalation",
    "advocate":       "brief_alert.advocate",
    "sentiment_drop": "brief_alert.sentiment_drop",
}
