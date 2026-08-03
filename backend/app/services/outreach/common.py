"""Pure helpers for the cold-outreach engine.

No DB, no HTTP, no LLM — everything here is deterministic and unit-testable,
shared by the API router (async) and the Celery scheduler/ingest hooks (sync).
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime, time, timedelta, timezone
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

# ── Pipeline statuses ───────────────────────────────────────────────────

PIPELINE_STATUSES = (
    "new",
    "queued",
    "contacted",
    "replied",
    "demo",
    "won",
    "lost",
    "do_not_contact",
)

# Ranking used to keep automatic transitions monotonic: a campaign event
# never moves a prospect *backwards* (a bump send must not demote a
# prospect the rep already advanced to "demo"). Manual PATCH may set any
# status; do_not_contact wins over everything.
_STATUS_RANK = {s: i for i, s in enumerate(PIPELINE_STATUSES)}

TERMINAL_STATUSES = ("won", "lost", "do_not_contact")


def advance_status(current: Optional[str], proposed: str) -> Optional[str]:
    """Return the status to write for an *automatic* transition, or None.

    - never leaves a terminal status
    - never moves backwards (rank-monotonic)
    - do_not_contact always applies
    """
    if proposed == "do_not_contact":
        return proposed
    if current in TERMINAL_STATUSES:
        return None
    if current is not None and _STATUS_RANK.get(proposed, 0) <= _STATUS_RANK.get(current, -1):
        return None
    return proposed


# ── Campaign config ─────────────────────────────────────────────────────


class OutreachStep(BaseModel):
    """One touch in the sequence. ``offset_days`` is measured from the
    previous touch (0 for the first)."""

    offset_days: int = Field(0, ge=0, le=90)
    # Optional per-step guidance appended to the drafting prompt
    # (e.g. "short bump, reference the original email").
    guidance: Optional[str] = None


# Email-safe font stacks. Keys are what campaign config stores; values are
# the CSS stacks rendered into the HTML body. Whitelisted so config can
# never inject arbitrary CSS.
EMAIL_FONTS = {
    "arial": "Arial, Helvetica, sans-serif",
    "helvetica": "Helvetica, Arial, sans-serif",
    "georgia": "Georgia, 'Times New Roman', serif",
    "times": "'Times New Roman', Times, serif",
    "verdana": "Verdana, Geneva, sans-serif",
    "tahoma": "Tahoma, Geneva, sans-serif",
    "trebuchet": "'Trebuchet MS', Helvetica, sans-serif",
    "courier": "'Courier New', Courier, monospace",
}


class OutreachTemplate(BaseModel):
    """Base template + the CAN-SPAM identity block.

    ``subject``/``body`` are the starting point the per-prospect drafts
    personalize from; ``{business_name}``-style placeholders are allowed
    and are substituted before the LLM sees the text. The identity fields
    are required before a campaign can activate — they render into the
    footer of every send.

    Body text supports lightweight formatting markers — ``**bold**``,
    ``*italic*``, ``_underline_``, ``[label](url)`` links — rendered into
    the HTML part at send time. The plain-text part strips the style
    markers; links become ``label: url`` so the destination survives.
    """

    subject: str = Field(..., min_length=1, max_length=400)
    body: str = Field(..., min_length=1)
    sender_name: str = Field(..., min_length=1, max_length=200)
    sender_business: str = Field(..., min_length=1, max_length=200)
    # CAN-SPAM: a valid physical postal address of the sender.
    physical_address: str = Field(..., min_length=1, max_length=500)
    # HTML styling. None → the recipient client's defaults.
    font_family: Optional[str] = None
    font_size_px: Optional[int] = Field(None, ge=10, le=24)
    # Embed the tenant's uploaded email logo (if any) at the bottom.
    include_logo: bool = True
    # Custom compliance-footer text. When set it replaces the default
    # three-line footer verbatim in BOTH the text and HTML parts, so the
    # tenant owns CAN-SPAM compliance of the override — it must still carry
    # the physical address and an opt-out instruction. The identity fields
    # above stay required either way.
    footer_text: Optional[str] = Field(None, max_length=1000)

    @field_validator("font_family")
    @classmethod
    def _font_known(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        key = v.strip().lower()
        if key not in EMAIL_FONTS:
            raise ValueError(
                "font_family must be one of: " + ", ".join(sorted(EMAIL_FONTS))
            )
        return key


class OutreachAttachment(BaseModel):
    """Reference to a file uploaded via ``POST /outreach/uploads`` that is
    attached to every send of the campaign."""

    s3_key: str = Field(..., min_length=1, max_length=512)
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: Optional[str] = Field(None, max_length=255)
    size_bytes: Optional[int] = Field(None, ge=0)


class SendWindow(BaseModel):
    """Local-time window inside which sends may fire."""

    start_hour: int = Field(9, ge=0, le=23)
    end_hour: int = Field(17, ge=1, le=24)
    timezone: str = "America/New_York"
    # ISO weekday numbers, 1=Mon … 7=Sun.
    days: List[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])

    @field_validator("timezone")
    @classmethod
    def _tz_exists(cls, v: str) -> str:
        ZoneInfo(v)  # raises on unknown key
        return v

    @field_validator("days")
    @classmethod
    def _days_valid(cls, v: List[int]) -> List[int]:
        if not v or any(d < 1 or d > 7 for d in v):
            raise ValueError("days must be non-empty ISO weekdays 1-7")
        return sorted(set(v))


class OutreachConfig(BaseModel):
    """Validated shape of ``Campaign.config`` for kind='outreach'."""

    template: OutreachTemplate
    send_window: SendWindow = Field(default_factory=SendWindow)
    steps: List[OutreachStep] = Field(
        default_factory=lambda: [
            OutreachStep(offset_days=0),
            OutreachStep(offset_days=4, guidance="Short, friendly bump."),
        ],
        min_length=1,
        max_length=6,
    )
    # None → settings.OUTREACH_DEFAULT_DAILY_LIMIT at send time.
    daily_limit: Optional[int] = Field(None, ge=1, le=200)
    max_touches: int = Field(3, ge=1, le=6)
    # review: drafts wait for human approval. auto: drafts queue
    # themselves as soon as they generate.
    mode: str = "review"
    # Preferred provider; None → google-then-microsoft fallback.
    provider: Optional[str] = None
    # Files attached to every send (uploaded via POST /outreach/uploads).
    attachments: List[OutreachAttachment] = Field(default_factory=list, max_length=5)
    # Per-recipient click tracking. When True, every ``[text](url)`` link
    # in the HTML part is rewritten at send time to an opaque LINDA
    # redirect (/t/{token}) that records the click before 302ing to the
    # real destination. The text/plain part ALWAYS keeps the original
    # URLs — rewritten links in plain text read as spam, and the click
    # signal comes from HTML clicks. Default off: redirect-wrapped links
    # can hurt cold-email deliverability, so first-touch campaigns may
    # deliberately leave this off and enable it for later-stage sequences.
    track_clicks: bool = False

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in ("review", "auto"):
            raise ValueError("mode must be 'review' or 'auto'")
        return v

    @field_validator("provider")
    @classmethod
    def _provider_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("google", "microsoft"):
            raise ValueError("provider must be 'google' or 'microsoft'")
        return v


def parse_config(raw: dict) -> OutreachConfig:
    """Validate a stored/incoming config dict. Raises pydantic ValidationError."""
    return OutreachConfig.model_validate(raw or {})


# ── Domain normalization (import idempotency key) ───────────────────────

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalize_domain(website: Optional[str]) -> Optional[str]:
    """Reduce a website/URL/domain string to a bare registrable host.

    ``https://www.Foo-Gym.com/pricing?x=1`` → ``foo-gym.com``. Returns
    None when nothing domain-shaped survives — the import falls back to
    name-based matching for those rows.
    """
    if not website:
        return None
    s = website.strip().lower()
    s = _SCHEME_RE.sub("", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split("@")[-1]  # tolerate pasted mailto:/user@host forms
    s = s.split(":", 1)[0]  # strip port
    if s.startswith("www."):
        s = s[4:]
    s = s.strip(".")
    if "." not in s or " " in s or not s:
        return None
    return s


# ── Opt-out detection ───────────────────────────────────────────────────

# Deliberately conservative: these mark the prospect do-not-contact and
# halt every sequence, so false positives are worse than false negatives.
# Soft negatives ("not interested") end the sequence via the normal reply
# path where a human (or the brief) decides what's next.
_STOP_PATTERNS = [
    r"\bunsubscribe\b",
    r"\bopt[ -]?out\b",
    r"\bopt (?:me|us) out\b",
    r"\bremove me\b",
    r"\btake me off\b",
    r"\bstop (?:e[- ]?mail|email|contact|messag)\w*\b",
    r"\bdo not (?:e[- ]?mail|email|contact)\b",
    r"\bdon'?t (?:e[- ]?mail|email|contact)\b",
    r"^\s*stop\s*[.!]?\s*$",
]
_STOP_RE = re.compile("|".join(_STOP_PATTERNS), re.IGNORECASE | re.MULTILINE)


def detect_opt_out(text: Optional[str]) -> bool:
    """True when a reply body reads as an opt-out request."""
    if not text:
        return False
    # Only scan the top of the message — quoted history below the reply
    # contains OUR footer ("Reply STOP…"), which must not self-trigger.
    head = "\n".join(text.splitlines()[:15])
    return bool(_STOP_RE.search(head))


# ── Bounce (DSN) heuristics ─────────────────────────────────────────────

_BOUNCE_FROM_RE = re.compile(
    r"(mailer-daemon|postmaster|mail delivery (?:subsystem|system))",
    re.IGNORECASE,
)
_MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def looks_like_bounce(from_address: Optional[str], subject: Optional[str]) -> bool:
    if from_address and _BOUNCE_FROM_RE.search(from_address):
        return True
    if subject and re.search(
        r"(undeliverable|delivery status notification|failure notice|"
        r"returned mail|mail delivery failed)",
        subject,
        re.IGNORECASE,
    ):
        return True
    return False


def extract_message_ids(text: Optional[str], limit: int = 20) -> List[str]:
    """Candidate RFC-822 Message-IDs embedded in a DSN body."""
    if not text:
        return []
    return _MSGID_RE.findall(text)[:limit]


# ── Send window / footer ────────────────────────────────────────────────


def in_send_window(window: SendWindow, now_utc: Optional[datetime] = None) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(ZoneInfo(window.timezone))
    if local.isoweekday() not in window.days:
        return False
    start = time(hour=window.start_hour)
    end = time(hour=window.end_hour - 1, minute=59, second=59) if window.end_hour < 24 else time(23, 59, 59)
    return start <= local.time() <= end


def local_day_bounds_utc(
    window: SendWindow, now_utc: Optional[datetime] = None
) -> Tuple[datetime, datetime]:
    """UTC bounds of "today" in the campaign's send-window timezone —
    the day the daily throttle counts against."""
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(window.timezone)
    local = now_utc.astimezone(tz)
    day_start = datetime(local.year, local.month, local.day, tzinfo=tz)
    return day_start.astimezone(timezone.utc), (day_start + timedelta(days=1)).astimezone(timezone.utc)


def compose_footer(template: OutreachTemplate) -> str:
    """CAN-SPAM footer appended to every outreach send: identify the
    sender + business, include the physical address, offer opt-out.
    A ``footer_text`` override replaces the default lines verbatim."""
    if template.footer_text:
        return "\n\n--\n" + template.footer_text
    return (
        "\n\n--\n"
        "{name} · {business}\n"
        "{address}\n"
        "If you'd rather not hear from me, just reply \"unsubscribe\" "
        "and I won't email you again."
    ).format(
        name=template.sender_name,
        business=template.sender_business,
        address=template.physical_address,
    )


# ── Lightweight formatting → HTML ───────────────────────────────────────
#
# The body supports four inline markers: **bold**, *italic*, _underline_,
# and [label](url) links. The HTML part renders them; the plain-text part
# strips the style markers (links keep their destination — see
# strip_markers). Nothing else in the body is treated as markup —
# everything is HTML-escaped before the markers are applied, so
# config/LLM text can never inject tags.

# Content must start and end on non-space (so "2 * 3 * 4" and stray
# underscores never read as formatting). Triple asterisks — what
# bold+italic nesting collapses to — must be handled first, or the
# bold/italic patterns each reject the leftover asterisk.
_TRIPLE_RE = re.compile(r"\*\*\*([^\s*](?:[^\n]*?[^\s*])?)\*\*\*")
_BOLD_RE = re.compile(r"\*\*([^\s*](?:[^\n]*?[^\s*])?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?!\*)")
_UNDERLINE_RE = re.compile(r"(?<![\w_])_([^\s_](?:[^_\n]*?[^\s_])?)_(?![\w_])")

# Inline links — ``[label](url)``, scheme optional. The HTML part renders
# an anchor, prepending ``https://`` when the scheme is missing; the
# plain-text part shows "label: url" so the destination stays visible in
# text-only clients. Only http(s) can ever render as a link — any other
# scheme (javascript:, mailto:, data:, …) fails the domain pattern and
# stays literal text. Applied AFTER HTML-escaping and BEFORE the style
# markers, so a label may itself carry those markers and can never
# inject markup.
_LINK_RE = re.compile(
    r"\[([^\]\n]+)\]\(((?:https?://)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s)]*)?)\)",
    re.IGNORECASE,
)
_HTTP_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)


def strip_markers(text: str) -> str:
    """Formatting markers removed — the plain-text alternative body.

    Links are the exception: they render as ``label: url`` rather than
    being stripped, so text-only readers still get the destination
    (exactly as typed — no scheme prepending here)."""
    out = _LINK_RE.sub(r"\1: \2", text or "")
    out = _TRIPLE_RE.sub(r"\1", out)
    out = _BOLD_RE.sub(r"\1", out)
    out = _ITALIC_RE.sub(r"\1", out)
    return _UNDERLINE_RE.sub(r"\1", out)


def _markers_to_html(
    escaped: str,
    link_rewriter: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    # `escaped` is already HTML-escaped, so the captured URL/label can't
    # break out of the href attribute or inject markup ('"' is &quot; here).
    # Hrefs sit behind \x00-framed placeholders while the style passes
    # run, so a URL's underscores/asterisks can never read as formatting;
    # the label stays inline — it MAY carry style markers.
    hrefs: List[str] = []

    def _link(m: "re.Match") -> str:
        label, href = m.group(1), m.group(2)
        if not _HTTP_SCHEME_RE.match(href):
            href = "https://" + href
        if link_rewriter is not None:
            # The capture is escaped text; the rewriter works on (and
            # stores) the real destination, so unescape before handing
            # it over and re-escape whatever comes back.
            replacement = link_rewriter(html_mod.unescape(href))
            if replacement:
                href = html_mod.escape(replacement)
        hrefs.append(href)
        return '<a href="\x00{0}\x00" style="color:#2563eb;">{1}</a>'.format(
            len(hrefs) - 1, label
        )

    out = _LINK_RE.sub(_link, escaped.replace("\x00", ""))
    out = _TRIPLE_RE.sub(r"<b><i>\1</i></b>", out)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    out = _UNDERLINE_RE.sub(r"<u>\1</u>", out)
    for i, href in enumerate(hrefs):
        out = out.replace("\x00{0}\x00".format(i), href, 1)
    return out


def _paragraphs_html(
    text: str,
    link_rewriter: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    blocks = re.split(r"\n\s*\n", text.strip()) if text.strip() else []
    rendered = []
    for block in blocks:
        inner = _markers_to_html(
            html_mod.escape(block), link_rewriter
        ).replace("\n", "<br>")
        rendered.append(f'<p style="margin:0 0 1em 0;">{inner}</p>')
    return "".join(rendered)


def render_email_html(
    body_text: str,
    template: OutreachTemplate,
    logo_cid: Optional[str] = None,
    link_rewriter: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """The HTML alternative for one outreach send: the (marker-formatted)
    body, the tenant logo when provided, and the CAN-SPAM footer —
    mirroring exactly what ``compose_footer`` appends to the text part.

    ``link_rewriter`` (click tracking) receives each body link's real
    destination URL and returns the replacement href, or None to keep the
    original. It sees BODY links only — the footer never renders anchors,
    so compliance/unsubscribe text is structurally out of reach — and only
    http(s) destinations (_LINK_RE never matches mailto:). Display text
    is untouched either way.
    """
    style = "line-height:1.45;"
    if template.font_family:
        style += f"font-family:{EMAIL_FONTS[template.font_family]};"
    if template.font_size_px:
        style += f"font-size:{template.font_size_px}px;"

    parts = [f'<div style="{style}">', _paragraphs_html(body_text, link_rewriter)]
    if logo_cid:
        parts.append(
            '<div style="margin-top:16px;">'
            f'<img src="cid:{logo_cid}" alt="{html_mod.escape(template.sender_business)}" '
            'style="max-height:64px;max-width:220px;border:0;"></div>'
        )
    if template.footer_text:
        footer_inner = html_mod.escape(template.footer_text).replace("\n", "<br>")
    else:
        footer_inner = (
            f"{html_mod.escape(template.sender_name)} &middot; "
            f"{html_mod.escape(template.sender_business)}<br>"
            f"{html_mod.escape(template.physical_address)}<br>"
            "If you&#x27;d rather not hear from me, just reply &quot;unsubscribe&quot; "
            "and I won&#x27;t email you again."
        )
    parts.append(
        '<div style="margin-top:24px;font-size:12px;color:#666666;">--<br>'
        f"{footer_inner}</div>"
    )
    parts.append("</div>")
    return "".join(parts)


def render_placeholders(text: str, values: dict) -> str:
    """Substitute ``{business_name}``-style placeholders, leaving unknown
    braces untouched (the text goes to an LLM next, not str.format)."""
    out = text
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val) if val is not None else "")
    return out
