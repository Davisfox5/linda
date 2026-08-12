"""Linda agent — system prompt, tool definitions, and tool-use dispatch for Ask Linda chat.

Read tools execute immediately. Draft tools create a ``WriteProposal`` row
(pending, expires in 24h) and return a preview payload to the LLM — they do
**not** mutate state. The frontend surfaces the proposal as a Confirm / Edit /
Cancel card; only on Confirm does the confirmation endpoint dispatch to the
real mutator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionItem,
    Campaign,
    LindaChatConversation,
    LindaChatMessage,
    Interaction,
    InteractionSnippet,
    Tenant,
    User,
    WriteProposal,
)
from backend.app.config import get_settings
from backend.app.services import campaign_stats, linda_context, linda_entity_lookup
from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.model_router import (
    CacheableBlock,
    LLMRequest,
    ModelRouter,
    TaskType,
    Tier,
)
from backend.app.services.search_service import SearchService

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048
PROPOSAL_TTL = timedelta(hours=24)

# Pre-rot context threshold: the most recent N replayed messages we SEND to the
# model each turn. Persistence is unbounded (full transcript stays in the DB);
# this only bounds the working-context window so a long-lived conversation
# doesn't grow context — and cost — every turn (context rot; §5 of the
# knowledge base). 40 ≈ 20 user/assistant turns, well beyond a normal session.
HISTORY_WINDOW_MESSAGES = 40

# ── System prompt (static portion — cached) ───────────────────────────────

PERSONA = (
    "You are Linda — Listening Intelligence and Natural Dialogue Assistant. "
    "You are the AI orchestrator behind this product, the same brain that "
    "analyzes calls, drafts coaching notes, and extracts action items. You "
    "speak in the first person, calm and attentive, like a thoughtful "
    "colleague who listened to every call.\n\n"
    "Tone: warm, honest, concise. You're not a mascot and you're not "
    "servile. Be direct when there's something worth saying.\n"
    "Never invent data — if you don't have it, use a tool to look it up, "
    "or say you don't know.\n"
    "When proposing a write (creating an action item, drafting an email, "
    "updating a CRM), describe what you're about to propose *before* "
    "calling the tool, so the user knows what's coming. The tool will "
    "return a proposal id — the user will Confirm or Cancel from the UI. "
    "Never pretend a write has happened until the user confirms."
)

PRODUCT_KNOWLEDGE = (
    "## About the product\n"
    "LINDA is a call intelligence platform for sales and support teams. "
    "Every customer call is transcribed, analyzed, and scored. Core "
    "surfaces the user might ask about:\n"
    "- Interactions: list of calls with sentiment, summary, topics\n"
    "- Action Items: follow-ups extracted from calls, with assignee and "
    "due date\n"
    "- Scorecards: QA rubrics applied to each call (Sales QA, CS QA)\n"
    "- Snippets: short clips of notable moments (pricing pushback, "
    "competitor mention, positive feedback)\n"
    "- Live Coaching: real-time hints during active calls\n"
    "- Email: when the tenant connects Gmail/Outlook, their emails with "
    "external contacts are captured as interactions (channel='email', "
    "direction inbound/outbound) and are searchable with search_interactions. "
    "You can ALSO read the user's most recent SENT emails live, on demand, "
    "with search_sent_email.\n"
    "- Integrations: Salesforce, HubSpot, Slack, Gmail, Zoom\n"
    "- Webhooks: outbound events for tenant systems\n"
    "- Campaigns: two kinds — external ESP campaigns tracked for analytics, "
    "and LINDA-sent cold-outreach campaigns (drafted, throttled, and sent "
    "by LINDA)\n\n"
    "## Email visibility — important\n"
    "You are NOT limited to call/meeting data. When the user asks about "
    "their sent emails, outbound messages, or whether they emailed someone, "
    "do NOT reply that you lack access — call search_sent_email (live Gmail) "
    "and/or search_interactions with channel='email'. Only tell the user "
    "Gmail is unavailable if a tool result reports the account isn't "
    "connected (connected=false) or needs re-authorization (auth_error).\n\n"
    "## Campaign visibility — important\n"
    "You are NOT limited to call/meeting data here either. Never claim you "
    "can't see campaigns. When the user asks about 'our latest/most recent "
    "campaign' without naming one, call list_campaigns FIRST to resolve it "
    "(it's ordered most-recent-first and covers both kinds). Then drill in "
    "with get_campaign_stats for funnel/quota/rollup numbers, or "
    "list_campaign_replies for 'what have the responses been' questions. "
    "Explain the external-vs-outreach distinction when it's relevant to the "
    "answer (e.g. outreach campaigns have a member funnel and daily send "
    "quota; external ones don't). Only tell the user there are no campaigns "
    "after list_campaigns actually returns an empty list.\n\n"
    "## Names vs ids — how to act on a person or company\n"
    "Users talk in names (\"Acme\", \"Dana\", \"the pricing thread\"); every "
    "write tool takes an id. Bridge the two with resolve_entity BEFORE "
    "proposing anything about a named person or company. It returns "
    "customers (whose id is the prospect_id for propose_queue_bump_email), "
    "contacts, and teammates (whose email is the assignee_email for action "
    "items and plans). When it returns several plausible candidates, ask "
    "the user which one — do not pick for them. When it returns nothing, "
    "say so plainly instead of inventing an id; an id you did not get from "
    "a tool is always wrong.\n\n"
    "## What each write tool actually does on confirm\n"
    "Be accurate about this — never overstate a write.\n"
    "- propose_action_item / propose_email_draft: both need an "
    "interaction_id, so find the call or email first with "
    "search_interactions. An email draft is STAGED for the user to send, "
    "not sent.\n"
    "- propose_action_plan: the right tool for a standalone follow-up with "
    "no related conversation — it's the only one that doesn't need an "
    "interaction.\n"
    "- propose_crm_update: really writes to the connected CRM on confirm. "
    "Say exactly which operation you're proposing and to which record.\n"
    "- propose_queue_bump_email: queues a send for the campaign scheduler; "
    "it goes out inside the campaign's window and daily throttle, not "
    "immediately."
)


def build_system_blocks(tenant: Tenant, user: Optional[User]) -> List[Dict[str, Any]]:
    """Build the system prompt as a list of blocks so the static portion can be cached."""
    static_text = f"{PERSONA}\n\n{PRODUCT_KNOWLEDGE}"
    user_line = (
        f"Signed in as: {user.name or user.email} ({user.role})"
        if user is not None
        else "Signed in via API key."
    )
    dynamic_text = (
        f"## Tenant context\n"
        f"- Tenant: {tenant.name} ({tenant.slug})\n"
        f"- {user_line}\n"
    )
    return [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": dynamic_text},
    ]


# ── Tool definitions (cached — static across turns) ───────────────────────

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_interactions",
        "description": "Full-text search across the tenant's call transcripts. Use this when the user asks about past calls, topics, competitors, or specific phrases.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "channel": {"type": "string", "description": "Optional channel filter (voice, email, transcript, zoom, meet)."},
                "date_from": {"type": "string", "description": "Optional ISO date lower bound."},
                "date_to": {"type": "string", "description": "Optional ISO date upper bound."},
                "limit": {"type": "integer", "description": "Max results (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "resolve_entity",
        "description": (
            "Turn a name, email address, or company domain into the ids "
            "other tools need. Search this FIRST whenever the user names a "
            "person or company — \"Acme\", \"Dana\", \"dana@acme.com\" — "
            "because every write tool is keyed on an id, not a name. "
            "Returns candidates across three kinds: 'customer' (an account "
            "or an outreach prospect — its id is what propose_queue_bump_email "
            "wants as prospect_id), 'contact' (a person at a customer), and "
            "'user' (a teammate on this tenant — use its email for "
            "assignee_email). Each candidate carries a confidence and the "
            "reason it matched; if several look plausible, ask the user "
            "which one rather than guessing. Customer rows also report "
            "pipeline_status and do_not_contact — never propose outreach to "
            "a prospect whose do_not_contact is true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name, email address, or domain to look up.",
                },
                "kinds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["customer", "contact", "user"],
                    },
                    "description": "Optional filter. Omit to search all three.",
                },
                "limit": {"type": "integer", "description": "Max candidates (default 8, max 25)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_action_items",
        "description": (
            "List action items for the tenant. Filter by status, assignee, "
            "or due-before. Statuses are 'open', 'done', and 'dismissed' "
            "— 'open' covers anything not yet finished."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "done", "dismissed"],
                    "description": "Filter by status.",
                },
                "assignee_email": {"type": "string", "description": "Filter by assignee email."},
                "due_before": {"type": "string", "description": "ISO date — only items due before this date."},
                "limit": {"type": "integer", "description": "Max results (default 20)."},
            },
        },
    },
    {
        "name": "get_interaction_detail",
        "description": "Fetch full detail for a single interaction: summary, sentiment, snippets, action items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string", "description": "Interaction UUID."},
            },
            "required": ["interaction_id"],
        },
    },
    {
        "name": "search_sent_email",
        "description": (
            "Read the user's OWN recently SENT emails, live from their connected "
            "Gmail account. Use this whenever the user asks what they've sent, "
            "whether they emailed someone, or wants to review outbound follow-ups "
            "— e.g. \"what did I send to Acme?\", \"did I reply to the pricing "
            "thread?\", \"show my sent emails this week\". Returns recipient, "
            "subject, date, and a snippet for each message. If the tenant hasn't "
            "connected Gmail the result says so — then tell the user to connect "
            "Gmail under Integrations; do NOT claim you fundamentally can't see "
            "sent email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional keywords to match in the subject or body."},
                "to": {"type": "string", "description": "Optional recipient filter — an email address or domain, e.g. 'acme.com'."},
                "newer_than_days": {"type": "integer", "description": "Optional: only messages sent within the last N days."},
                "limit": {"type": "integer", "description": "Max messages to return (default 10, max 25)."},
            },
        },
    },
    {
        "name": "list_campaigns",
        "description": (
            "List the tenant's email campaigns — both externally-run ESP "
            "campaigns (kind='external') and LINDA-sent cold-outreach "
            "campaigns (kind='outreach'), most recent first. Use this FIRST "
            "when the user says 'our latest/most recent campaign' without "
            "naming one, or asks to see what campaigns exist. Each row "
            "includes sent_count, status, and kind, so you rarely need "
            "get_campaign_stats just to identify the right campaign."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["external", "outreach"],
                    "description": "Optional filter by campaign kind.",
                },
                "status": {
                    "type": "string",
                    "enum": ["draft", "active", "paused", "completed", "archived"],
                    "description": "Optional filter by status. Omit to list all.",
                },
                "limit": {"type": "integer", "description": "Max results (default 10)."},
            },
        },
    },
    {
        "name": "get_campaign_stats",
        "description": (
            "Fetch the full analytics overview for one campaign: header "
            "fields plus the rollup (sent, opens, clicks, replies, bounces, "
            "unsubscribes, conversions, reply sentiment). For outreach "
            "campaigns (kind='outreach') it ALSO includes the member funnel "
            "(counts per sequence state) and today's send quota — these are "
            "not applicable to external campaigns and are omitted for them. "
            "Quote the rollup's 'sent' (recipient count) when asked how many "
            "were sent, not the header's sent_count, which can lag mid-flight."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string", "description": "Campaign UUID."},
            },
            "required": ["campaign_id"],
        },
    },
    {
        "name": "list_campaign_replies",
        "description": (
            "Get the replies/responses to one campaign. Use this for "
            "'what have the responses to our campaign been?' or similar. "
            "Returns attributed inbound replies (with sentiment, subject, "
            "and a summary snippet) plus a raw reply-event count for "
            "campaigns whose replies weren't matched to a full interaction "
            "record — report both if they differ, and explain the gap."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string", "description": "Campaign UUID."},
                "limit": {"type": "integer", "description": "Max replies to return (default 10)."},
            },
            "required": ["campaign_id"],
        },
    },
    {
        "name": "propose_action_item",
        "description": (
            "Propose a follow-up task attached to a specific call or email. "
            "Returns a proposal preview; the user confirms from the UI. "
            "``interaction_id`` is REQUIRED — an action item always hangs off "
            "an interaction. For a standalone follow-up that isn't tied to a "
            "conversation, use propose_action_plan instead, which supports "
            "one. Find the interaction with search_interactions first if you "
            "don't already have its id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {
                    "type": "string",
                    "description": "Related interaction UUID. Required.",
                },
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee_email": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date."},
                "priority": {"type": "string", "description": "high | medium | low"},
            },
            "required": ["interaction_id", "title"],
        },
    },
    {
        "name": "propose_email_draft",
        "description": (
            "Propose a follow-up email draft for a specific call or email. "
            "Returns a proposal preview with subject and body. On confirm the "
            "draft is staged on a new action item for the user to review and "
            "send — it is NOT sent automatically, so don't tell the user it "
            "went out. ``interaction_id`` is REQUIRED; find it with "
            "search_interactions first if you don't have it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {
                    "type": "string",
                    "description": "Related interaction UUID. Required.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "recipients": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["interaction_id", "subject", "body", "recipients"],
        },
    },
    {
        "name": "propose_queue_bump_email",
        "description": (
            "Propose queueing the next outreach follow-up (bump) email for "
            "a prospect who hasn't replied to a cold-outreach campaign. "
            "Returns a proposal preview; on confirm the bump is queued and "
            "the campaign scheduler sends it inside the campaign's send "
            "window and daily throttle. Use only for prospects enrolled in "
            "an outreach campaign."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {
                    "type": "string",
                    "description": "The prospect's customer UUID.",
                },
                "campaign_id": {
                    "type": "string",
                    "description": (
                        "Outreach campaign UUID (optional — defaults to the "
                        "prospect's most recent active enrollment)."
                    ),
                },
                "subject": {"type": "string", "description": "Optional draft subject override."},
                "body": {"type": "string", "description": "Optional draft body override."},
            },
            "required": ["prospect_id"],
        },
    },
    {
        "name": "propose_crm_update",
        "description": (
            "Propose writing something to the tenant's connected CRM "
            "(HubSpot / Salesforce / Pipedrive). On confirm this really "
            "executes against the CRM, so be precise about what you're "
            "proposing before you call it. The CRM is chosen automatically "
            "from the interaction's contact/account. Supported operations "
            "are exactly: 'create_note' (log a note — payload needs "
            "{body}), 'create_task' / 'create_activity' (payload takes "
            "{subject, note, due_date}), and 'update_deal_stage' (payload "
            "needs {deal_external_id, stage_external_id}). Anything else "
            "will be refused by the adapter. ``interaction_id`` is REQUIRED "
            "— it's how the CRM record to attach to is resolved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {
                    "type": "string",
                    "description": "Interaction UUID whose contact/account the write attaches to. Required.",
                },
                "operation": {
                    "type": "string",
                    "enum": [
                        "create_note",
                        "create_task",
                        "create_activity",
                        "update_deal_stage",
                    ],
                    "description": "Which CRM operation to run.",
                },
                "payload": {
                    "type": "object",
                    "description": "Operation arguments — see the tool description for the fields each operation takes.",
                },
            },
            "required": ["interaction_id", "operation", "payload"],
        },
    },
    {
        "name": "propose_action_plan",
        "description": (
            "Propose creating a 1-step Action Plan. Use this when the user "
            "asks Linda to set up a single follow-up task (rather than a "
            "multi-step workflow synthesized from a call). The user "
            "confirms from the UI to materialize the plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {
                    "type": "string",
                    "description": "Related interaction UUID, if any (optional).",
                },
                "title": {"type": "string"},
                "description": {"type": "string"},
                "intent": {
                    "type": "string",
                    "description": "One-sentence statement of what this step achieves.",
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "email | phone_call | meeting | document_send | "
                        "research | note. Defaults to 'note' when omitted."
                    ),
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "sales | customer_service | it_support | generic. "
                        "Defaults to the tenant or per-team default."
                    ),
                },
                "assignee_email": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date."},
                "priority": {"type": "string", "description": "high | medium | low"},
            },
            "required": ["title"],
        },
    },
]

READ_TOOLS = {
    "search_interactions",
    "resolve_entity",
    "get_action_items",
    "get_interaction_detail",
    "search_sent_email",
    "list_campaigns",
    "get_campaign_stats",
    "list_campaign_replies",
}
DRAFT_TOOLS = {
    "propose_action_item",
    "propose_email_draft",
    "propose_crm_update",
    "propose_action_plan",
    "propose_queue_bump_email",
}

DRAFT_KIND_BY_TOOL = {
    "propose_action_item": "action_item",
    "propose_email_draft": "email_draft",
    "propose_crm_update": "crm_update",
    "propose_action_plan": "action_plan",
    "propose_queue_bump_email": "queue_bump_email",
}


# ── Dispatcher ─────────────────────────────────────────────────────────────


@dataclass
class AgentContext:
    db: AsyncSession
    tenant: Tenant
    user: Optional[User]
    conversation_id: uuid.UUID


async def _exec_search_interactions(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    svc = SearchService()
    try:
        results = await svc.search(
            db=ctx.db,
            tenant_id=str(ctx.tenant.id),
            query=args["query"],
            channel=args.get("channel"),
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
            limit=int(args.get("limit", 10)),
        )
    except Exception as exc:
        logger.exception("search_interactions failed")
        return {"error": f"search failed: {exc}"}
    return {"results": results}


async def _exec_resolve_entity(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        candidates = await linda_entity_lookup.resolve_entity(
            db=ctx.db,
            tenant=ctx.tenant,
            query=str(args.get("query") or ""),
            kinds=args.get("kinds"),
            limit=args.get("limit", 8),
        )
    except Exception as exc:
        logger.exception("resolve_entity failed")
        return {"error": "entity lookup failed: %s" % exc}
    return {"query": args.get("query"), "count": len(candidates), "candidates": candidates}


# ActionItem.status is canonically {open, done, dismissed} (see the enum
# note in backend/app/api/action_items.py). The REST router tolerates the
# legacy spellings; this tool's executor must too, because a long-lived
# conversation — or a stale tool description — can still send them, and a
# raw `status == "completed"` filter silently returns zero rows.
_STATUS_ALIASES = {
    "pending": "open",
    "in_progress": "open",
    "snoozed": "open",
    "completed": "done",
    "rejected": "dismissed",
}


def _canonical_status(value: Any) -> Optional[str]:
    """Map any status spelling to {open, done, dismissed}, else None."""
    if not value:
        return None
    v = str(value).lower().strip()
    if v in ("open", "done", "dismissed"):
        return v
    return _STATUS_ALIASES.get(v)


async def _exec_get_action_items(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    stmt = select(ActionItem).where(ActionItem.tenant_id == ctx.tenant.id)
    if args.get("status"):
        status = _canonical_status(args["status"])
        if status is None:
            return {
                "error": "unknown status %r — use open, done, or dismissed"
                % args["status"]
            }
        # Rows created before the status enum was simplified (and by Linda
        # itself, prior to this fix) carry the legacy spellings, so the
        # "open" bucket has to match all of them or those items vanish.
        if status == "open":
            stmt = stmt.where(
                ActionItem.status.in_(("open", "pending", "in_progress"))
            )
        elif status == "done":
            stmt = stmt.where(ActionItem.status.in_(("done", "completed")))
        else:
            stmt = stmt.where(ActionItem.status.in_(("dismissed", "rejected")))
    if args.get("due_before"):
        stmt = stmt.where(ActionItem.due_date <= date.fromisoformat(args["due_before"]))
    if args.get("assignee_email"):
        assignee_stmt = select(User.id).where(
            User.tenant_id == ctx.tenant.id, User.email == args["assignee_email"]
        )
        assignee_id = (await ctx.db.execute(assignee_stmt)).scalar_one_or_none()
        if assignee_id is not None:
            stmt = stmt.where(ActionItem.assigned_to == assignee_id)
    stmt = stmt.order_by(ActionItem.created_at.desc()).limit(int(args.get("limit", 20)))
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return {
        "action_items": [
            {
                "id": str(r.id),
                "title": r.title,
                "description": r.description,
                "status": r.status,
                "priority": r.priority,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "interaction_id": str(r.interaction_id),
            }
            for r in rows
        ]
    }


async def _exec_get_interaction_detail(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        interaction_uuid = uuid.UUID(args["interaction_id"])
    except (KeyError, ValueError):
        return {"error": "invalid interaction_id"}

    interaction = (
        await ctx.db.execute(
            select(Interaction).where(
                Interaction.id == interaction_uuid, Interaction.tenant_id == ctx.tenant.id
            )
        )
    ).scalar_one_or_none()
    if interaction is None:
        return {"error": "interaction not found"}

    snippet_rows = (
        await ctx.db.execute(
            select(InteractionSnippet).where(InteractionSnippet.interaction_id == interaction_uuid)
        )
    ).scalars().all()

    insights = interaction.insights or {}
    return {
        "interaction": {
            "id": str(interaction.id),
            "title": interaction.title,
            "summary": insights.get("summary"),
            "sentiment_overall": insights.get("sentiment_overall"),
            "sentiment_score": insights.get("sentiment_score"),
            "channel": interaction.channel,
            "created_at": interaction.created_at.isoformat() if interaction.created_at else None,
        },
        "snippets": [
            {
                "title": s.title,
                "description": s.description,
                "quality": s.quality,
                "start_time": s.start_time,
                "end_time": s.end_time,
            }
            for s in snippet_rows
        ],
    }


async def _exec_list_campaigns(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    limit = _clamped_limit(args, default=10)
    rows = await campaign_stats.list_campaigns(
        ctx.db,
        ctx.tenant,
        kind=args.get("kind"),
        status=args.get("status"),
        limit=limit,
    )
    return {"campaigns": rows}


async def _exec_get_campaign_stats(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        campaign_uuid = uuid.UUID(args["campaign_id"])
    except (KeyError, ValueError):
        return {"error": "invalid campaign_id"}

    # Belt-and-suspenders tenant scoping — RLS also applies, but every
    # executor that takes an id checks tenant_id explicitly like this.
    exists = (
        await ctx.db.execute(
            select(Campaign.id).where(
                Campaign.id == campaign_uuid, Campaign.tenant_id == ctx.tenant.id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        return {"error": "campaign not found"}

    overview = await campaign_stats.campaign_overview(ctx.db, ctx.tenant, campaign_uuid)
    if overview is None:
        return {"error": "campaign not found"}
    return {"campaign": overview}


async def _exec_list_campaign_replies(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        campaign_uuid = uuid.UUID(args["campaign_id"])
    except (KeyError, ValueError):
        return {"error": "invalid campaign_id"}

    exists = (
        await ctx.db.execute(
            select(Campaign.id).where(
                Campaign.id == campaign_uuid, Campaign.tenant_id == ctx.tenant.id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        return {"error": "campaign not found"}

    replies = await campaign_stats.list_campaign_replies(
        ctx.db, ctx.tenant, campaign_uuid, limit=_clamped_limit(args, default=10)
    )
    return replies


def _clamped_limit(args: Dict[str, Any], default: int, lo: int = 1, hi: int = 50) -> int:
    """Model-supplied limits are clamped so a stray ``limit: 500`` can't
    bloat the context window (the snippet cap only bounds one field)."""
    try:
        limit = int(args.get("limit", default))
    except (TypeError, ValueError):
        limit = default
    return max(lo, min(hi, limit))


def _fetch_sent_gmail_sync(
    tenant_id: uuid.UUID,
    query: Optional[str],
    to: Optional[str],
    newer_than_days: Optional[int],
    limit: int,
) -> Dict[str, Any]:
    """Blocking read of a tenant's SENT Gmail folder.

    Reuses the ingest poller's proven auth path — Fernet-decrypt of the stored
    token plus OAuth refresh via ``refresh_if_expired_sync`` — and a synchronous
    SQLAlchemy session, so it MUST run in a worker thread (see
    ``_exec_search_sent_email``), never on the event loop.

    Tenant isolation: the ``Integration`` row is loaded filtered by
    ``tenant_id``, so a request can only ever reach its own tenant's mailbox.
    Messages are read with ``format="metadata"`` (headers + Gmail's own snippet)
    — no full bodies are pulled.
    """
    from googleapiclient.discovery import build

    from backend.app.models import Integration
    from backend.app.services.email_ingest import gmail as gmail_fetcher
    from backend.app.services.email_ingest.poller import refresh_if_expired_sync
    from backend.app.tasks import _get_sync_session
    from backend.app.tenant_ctx import tenant_context

    session = _get_sync_session()
    try:
        # RLS: the runtime role (linda_app) enforces row-level security, so every
        # query/write in this block must run under the tenant's GUC context — the
        # same wrapper the email-ingest poller uses. Without it, the token-rotation
        # UPDATE inside refresh_if_expired_sync fails the integrations policy.
        with tenant_context(tenant_id, session):
            integration = (
                session.query(Integration)
                .filter(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "google",
                )
                .first()
            )
            if integration is None:
                return {"connected": False, "provider": "gmail", "messages": []}

            try:
                access_token = refresh_if_expired_sync(session, integration)
                # Persist the rotated access token so the next call doesn't re-refresh.
                session.commit()
            except Exception as exc:  # includes IntegrationAuthError
                session.rollback()
                logger.warning("search_sent_email: token refresh failed: %s", exc)
                return {
                    "connected": True,
                    "provider": "gmail",
                    "auth_error": True,
                    "messages": [],
                }

            agent_email = None
            if integration.user_id is not None:
                user = session.query(User).filter(User.id == integration.user_id).first()
                agent_email = user.email if user is not None else None

        parts: List[str] = []
        if query:
            parts.append(query.strip())
        if to:
            parts.append("to:%s" % to.strip())
        if newer_than_days and newer_than_days > 0:
            parts.append("newer_than:%dd" % int(newer_than_days))
        q = " ".join(parts)

        service = build(
            "gmail",
            "v1",
            credentials=gmail_fetcher.build_credentials(access_token),
            cache_discovery=False,
        )
        listing = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["SENT"], q=q, maxResults=limit)
            .execute()
        )
        ids = [m["id"] for m in listing.get("messages", [])][:limit]

        messages: List[Dict[str, Any]] = []
        for mid in ids:
            raw = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["To", "Cc", "Subject", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"]
                for h in raw.get("payload", {}).get("headers", [])
            }
            messages.append(
                {
                    "to": headers.get("To"),
                    "cc": headers.get("Cc"),
                    "subject": headers.get("Subject"),
                    "date": headers.get("Date"),
                    "snippet": raw.get("snippet"),
                }
            )
        return {
            "connected": True,
            "provider": "gmail",
            "mailbox": agent_email,
            "query": q or None,
            "count": len(messages),
            "messages": messages,
        }
    finally:
        session.close()


async def _exec_search_sent_email(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        limit = int(args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 25))

    newer = args.get("newer_than_days")
    try:
        newer = int(newer) if newer is not None else None
    except (TypeError, ValueError):
        newer = None

    try:
        return await asyncio.to_thread(
            _fetch_sent_gmail_sync,
            ctx.tenant.id,
            args.get("query"),
            args.get("to"),
            newer,
            limit,
        )
    except Exception as exc:
        logger.exception("search_sent_email failed")
        return {"error": "sent-email lookup failed: %s" % exc}


async def _create_proposal(
    ctx: AgentContext, kind: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    proposal = WriteProposal(
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant.id,
        user_id=ctx.user.id if ctx.user is not None else None,
        kind=kind,
        payload=payload,
        status="pending",
        expires_at=datetime.now(timezone.utc) + PROPOSAL_TTL,
    )
    ctx.db.add(proposal)
    await ctx.db.flush()
    return {
        "proposal_id": str(proposal.id),
        "kind": kind,
        "status": "pending",
        "preview": payload,
        "expires_at": proposal.expires_at.isoformat(),
    }


async def dispatch_tool(
    ctx: AgentContext, name: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Execute a tool by name. Read tools run; draft tools create WriteProposal rows."""
    if name == "search_interactions":
        return await _exec_search_interactions(ctx, args)
    if name == "resolve_entity":
        return await _exec_resolve_entity(ctx, args)
    if name == "get_action_items":
        return await _exec_get_action_items(ctx, args)
    if name == "get_interaction_detail":
        return await _exec_get_interaction_detail(ctx, args)
    if name == "search_sent_email":
        return await _exec_search_sent_email(ctx, args)
    if name == "list_campaigns":
        return await _exec_list_campaigns(ctx, args)
    if name == "get_campaign_stats":
        return await _exec_get_campaign_stats(ctx, args)
    if name == "list_campaign_replies":
        return await _exec_list_campaign_replies(ctx, args)
    if name in DRAFT_TOOLS:
        return await _create_proposal(ctx, DRAFT_KIND_BY_TOOL[name], args)
    return {"error": f"unknown tool: {name}"}


# ── Orchestration / streaming ──────────────────────────────────────────────


def _is_tool_result_content(content: Any) -> bool:
    """True if a message's content is a tool_result continuation (a list whose
    blocks are tool_result). Such a message can't legally START a request."""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _window_history(
    messages: List[Dict[str, Any]],
    max_messages: int = HISTORY_WINDOW_MESSAGES,
) -> List[Dict[str, Any]]:
    """Keep the most-recent ``max_messages`` messages, but start the window on a
    clean user turn.

    Anthropic requires the first message to be a ``user`` turn and forbids a
    dangling tool exchange at the head. So after taking the recency tail we drop
    any leading assistant message (or tool_result continuation) until the window
    begins with a genuine user turn. Order is preserved; the result is always a
    suffix of the input.
    """
    if len(messages) <= max_messages:
        return messages
    window = messages[-max_messages:]
    while window and (
        window[0].get("role") != "user"
        or _is_tool_result_content(window[0].get("content"))
    ):
        window.pop(0)
    return window


def _tool_use_ids(content: Any) -> List[str]:
    """The ids of every ``tool_use`` block in an assistant message's content."""
    if not isinstance(content, list):
        return []
    return [
        b["id"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
    ]


def _repair_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Guarantee the Anthropic tool contract: every assistant ``tool_use`` block
    is immediately followed by a user message carrying the matching
    ``tool_result`` (same ``tool_use_id``), and no ``tool_result`` is dangling.

    Real persisted conversations always write a tool row after a tool_use turn,
    so this is normally a straight pass-through. It is defensive against a turn
    that was interrupted mid-tool-exchange (assistant row committed, tool row
    not): rather than replay an orphaned ``tool_use`` — which the Messages API
    rejects with a 400 — we synthesize a stub error ``tool_result`` so the pair
    is complete, drop stray results, and drop any tool_result message that has
    no preceding tool_use.
    """
    out: List[Dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant":
            use_ids = _tool_use_ids(content)
            out.append(msg)
            if use_ids:
                results: List[Dict[str, Any]] = []
                # Consume the immediately-following tool_result message, if any.
                if i + 1 < n:
                    nxt = messages[i + 1]
                    if nxt.get("role") == "user" and _is_tool_result_content(
                        nxt.get("content")
                    ):
                        results = [
                            b
                            for b in nxt["content"]
                            if isinstance(b, dict) and b.get("type") == "tool_result"
                        ]
                        i += 1
                have = {b.get("tool_use_id") for b in results}
                for tid in use_ids:
                    if tid not in have:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": "Tool result was not recorded for this call.",
                                "is_error": True,
                            }
                        )
                valid = set(use_ids)
                results = [b for b in results if b.get("tool_use_id") in valid]
                out.append({"role": "user", "content": results})
        elif role == "user" and _is_tool_result_content(content):
            # A tool_result with no preceding tool_use — drop it.
            pass
        else:
            out.append(msg)
        i += 1
    return out


def _next_created_at(after: Optional[datetime] = None) -> datetime:
    """A strictly-increasing UTC timestamp for chat-message ordering.

    Every row written during one chat turn shares a single DB transaction, so a
    ``func.now()`` server default stamps them all with the *same* transaction
    time. Replay then orders by ``created_at`` with no way to break the tie, and
    a ``tool_use`` turn can come back NOT immediately followed by its
    ``tool_result`` — the exact shape the Messages API rejects with a 400. We
    stamp ``created_at`` in Python instead, advancing at least 1µs past the
    previous row so insertion order within a turn is always recoverable.
    """
    now = datetime.now(timezone.utc)
    if after is not None and now <= after:
        return after + timedelta(microseconds=1)
    return now


async def _load_history(db: AsyncSession, conversation_id: uuid.UUID) -> List[Dict[str, Any]]:
    """Replay prior messages in Anthropic message format.

    ``role="tool"`` rows hold the ``tool_result`` blocks for the preceding
    assistant ``tool_use`` turn and MUST be replayed as a user message — omitting
    them leaves the assistant's tool_use blocks unanswered and the next turn's
    request 400s. ``_repair_tool_pairs`` then enforces the pairing invariant.
    """
    rows = (
        await db.execute(
            select(LindaChatMessage)
            .where(LindaChatMessage.conversation_id == conversation_id)
            .order_by(LindaChatMessage.created_at.asc())
        )
    ).scalars().all()
    messages: List[Dict[str, Any]] = []
    for row in rows:
        if row.role == "assistant":
            messages.append(
                {"role": "assistant", "content": row.tool_calls or row.content}
            )
        elif row.role == "tool":
            if row.tool_calls:
                messages.append({"role": "user", "content": row.tool_calls})
        elif row.role == "user":
            messages.append({"role": "user", "content": row.content})
    return _repair_tool_pairs(messages)


async def run_chat_turn(
    ctx: AgentContext, user_message: str
) -> AsyncIterator[Dict[str, Any]]:
    """Run one chat turn, yielding SSE-friendly event dicts.

    Event shapes:
      {"type": "text", "delta": str}            — assistant text delta
      {"type": "tool_use", "tool": str, "input": dict}
      {"type": "tool_result", "tool": str, "result": dict}
      {"type": "proposal", "proposal": dict}    — write proposal created
      {"type": "done"}                          — end of turn
      {"type": "error", "message": str}
    """
    client = get_async_anthropic()
    router = ModelRouter(client)
    # Read at call time, not import time, so a settings override in a test
    # or a restart-free config change takes effect on the next turn.
    settings = get_settings()
    system_blocks = build_system_blocks(ctx.tenant, ctx.user)
    # Cacheable system blocks for the router (same content, same cache flags).
    req_system = [
        CacheableBlock(text=b["text"], cache=("cache_control" in b))
        for b in system_blocks
    ]

    # Window the replayed history to the pre-rot threshold before appending the
    # current turn (the new user message is always kept). Persistence is
    # untouched — only the working context we send to the model is bounded.
    history = _window_history(await _load_history(ctx.db, ctx.conversation_id))
    history.append({"role": "user", "content": user_message})

    # Persist the incoming user message immediately so it's durable even if the
    # stream fails. ``created_at`` is stamped in Python (strictly increasing per
    # row) so the whole turn — user, assistant tool_use, tool_result — replays in
    # insertion order; see _next_created_at.
    last_ts = _next_created_at()
    ctx.db.add(
        LindaChatMessage(
            conversation_id=ctx.conversation_id,
            tenant_id=ctx.tenant.id,
            user_id=ctx.user.id if ctx.user is not None else None,
            role="user",
            content=user_message,
            created_at=last_ts,
        )
    )
    await ctx.db.flush()

    max_loops = 5  # guard against runaway tool-use cycles
    stopped_cleanly = False
    for loop_idx in range(max_loops):
        assistant_text_parts: List[str] = []
        final_content: List[Dict[str, Any]] = []

        # Route the streaming tool-use call through the router (model
        # selection + open-time failover); the tool-dispatch loop below is
        # unchanged. temperature=0.7: warm but coherent enough for reliable
        # tool-argument construction (chat previously ran at the default 1.0).
        async with router.astream(
            LLMRequest(
                task_type=TaskType.GENERIC,
                forced_tier=Tier.SONNET,
                user_message="",
                messages=history,
                tools=TOOLS,
                system_blocks=req_system,
                max_tokens=MAX_TOKENS,
                temperature=0.7,
                call_site="linda_chat",
            )
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    assistant_text_parts.append(event.delta.text)
                    yield {"type": "text", "delta": event.delta.text}

            final = await stream.get_final_message()

        # Serialize assistant content blocks for history + DB
        for block in final.content:
            if block.type == "text":
                final_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                final_content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )

        history.append({"role": "assistant", "content": final_content})

        last_ts = _next_created_at(last_ts)
        ctx.db.add(
            LindaChatMessage(
                conversation_id=ctx.conversation_id,
                tenant_id=ctx.tenant.id,
                role="assistant",
                content="".join(assistant_text_parts),
                tool_calls=final_content,
                created_at=last_ts,
            )
        )
        await ctx.db.flush()

        if final.stop_reason != "tool_use":
            stopped_cleanly = True
            break

        # Execute tool calls and append their results for the next loop
        tool_results: List[Dict[str, Any]] = []
        for block in final.content:
            if block.type != "tool_use":
                continue
            yield {"type": "tool_use", "tool": block.name, "input": block.input}
            try:
                result = await dispatch_tool(ctx, block.name, dict(block.input))
            except Exception as exc:
                logger.exception("tool dispatch failed: %s", block.name)
                result = {"error": str(exc)}

            if block.name in DRAFT_TOOLS and "proposal_id" in result:
                # Proposals are small, and their payload is what the user
                # confirms — never reshape them.
                yield {"type": "proposal", "proposal": result}
            else:
                # Fit the result to the working-context budget BEFORE it
                # enters history or the transcript: whatever we persist here
                # is replayed on every later turn, so an unfitted result
                # bloats the context for the rest of the conversation.
                result = await linda_context.fit_tool_result(
                    result,
                    tool_name=block.name,
                    question=user_message,
                    budget=settings.LINDA_TOOL_RESULT_BUDGET_CHARS,
                    router=router,
                    condense_enabled=settings.LINDA_CONDENSE_ENABLED,
                )
                yield {"type": "tool_result", "tool": block.name, "result": result}

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )

        history.append({"role": "user", "content": tool_results})
        last_ts = _next_created_at(last_ts)
        ctx.db.add(
            LindaChatMessage(
                conversation_id=ctx.conversation_id,
                tenant_id=ctx.tenant.id,
                role="tool",
                content="",
                tool_calls=tool_results,
                created_at=last_ts,
            )
        )
        await ctx.db.flush()

    if not stopped_cleanly:
        logger.warning(
            "linda_agent: tool-use loop guard tripped after %d cycles (conversation %s)",
            max_loops,
            ctx.conversation_id,
        )
        yield {
            "type": "error",
            "message": (
                f"I hit my tool-use cycle limit ({max_loops}) before finishing. "
                "Try rephrasing or asking me to focus on one thing at a time."
            ),
        }

    yield {"type": "done"}


async def get_or_create_conversation(
    db: AsyncSession,
    tenant: Tenant,
    user: Optional[User],
    conversation_id: Optional[uuid.UUID],
) -> LindaChatConversation:
    if conversation_id is not None:
        existing = (
            await db.execute(
                select(LindaChatConversation).where(
                    LindaChatConversation.id == conversation_id,
                    LindaChatConversation.tenant_id == tenant.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    convo = LindaChatConversation(
        tenant_id=tenant.id, user_id=user.id if user is not None else None
    )
    db.add(convo)
    await db.flush()
    return convo
