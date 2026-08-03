"""Pure-logic tests for the cold-outreach helpers (no DB, no LLM)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.services.outreach.common import (
    OutreachConfig,
    OutreachTemplate,
    SendWindow,
    advance_status,
    compose_footer,
    detect_opt_out,
    extract_message_ids,
    in_send_window,
    local_day_bounds_utc,
    looks_like_bounce,
    normalize_domain,
    parse_config,
    render_email_html,
    render_placeholders,
    strip_markers,
)


# ── normalize_domain ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.Foo-Gym.com/pricing?x=1", "foo-gym.com"),
        ("http://foo.com", "foo.com"),
        ("foo.com", "foo.com"),
        ("www.foo.com/", "foo.com"),
        ("foo.com:8080/path", "foo.com"),
        ("mailto:owner@foo.com", "foo.com"),
        ("FOO.COM", "foo.com"),
        ("", None),
        (None, None),
        ("not a domain", None),
        ("localhost", None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


# ── opt-out detection ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "unsubscribe",
        "Please remove me from your list",
        "STOP",
        "stop.",
        "Take me off this list",
        "please opt me out\nthanks",
        "Do not contact me again",
        "don't email me",
    ],
)
def test_detect_opt_out_positive(text):
    assert detect_opt_out(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Sounds interesting, tell me more",
        "We already stopped using MindBody last year",  # 'stopped using X' ≠ opt-out
        "not interested right now, maybe next quarter",
        None,
        "",
    ],
)
def test_detect_opt_out_negative(text):
    assert detect_opt_out(text) is False


def test_detect_opt_out_ignores_quoted_footer():
    # Our own compliance footer appears in the quoted history of every
    # reply — it must not self-trigger an opt-out.
    reply = "Sure, let's talk Tuesday!\n" + "\n" * 20 + (
        "> If you'd rather not hear from me, just reply \"unsubscribe\"\n"
    )
    assert detect_opt_out(reply) is False


# ── status transitions ──────────────────────────────────────────────────


def test_advance_status_monotonic():
    assert advance_status("new", "contacted") == "contacted"
    assert advance_status("contacted", "replied") == "replied"
    # never backwards
    assert advance_status("demo", "contacted") is None
    assert advance_status("replied", "replied") is None


def test_advance_status_terminal_and_dnc():
    assert advance_status("won", "contacted") is None
    assert advance_status("lost", "replied") is None
    # do_not_contact always wins, even from terminal
    assert advance_status("won", "do_not_contact") == "do_not_contact"
    assert advance_status(None, "do_not_contact") == "do_not_contact"


def test_advance_status_from_null():
    assert advance_status(None, "contacted") == "contacted"


# ── config validation ───────────────────────────────────────────────────


def _valid_config() -> dict:
    return {
        "template": {
            "subject": "Quick question about {business_name}",
            "body": "Hi — noticed you use {current_software}.",
            "sender_name": "Davis Fox",
            "sender_business": "Flex",
            "physical_address": "123 Main St, Nashville, TN 37201",
        },
    }


def test_parse_config_defaults():
    cfg = parse_config(_valid_config())
    assert cfg.mode == "review"
    assert cfg.max_touches == 3
    assert cfg.daily_limit is None
    assert len(cfg.steps) == 2
    assert cfg.send_window.start_hour == 9


def test_parse_config_requires_can_spam_identity():
    raw = _valid_config()
    del raw["template"]["physical_address"]
    with pytest.raises(ValidationError):
        parse_config(raw)


def test_parse_config_rejects_bad_mode_and_provider():
    raw = _valid_config()
    raw["mode"] = "yolo"
    with pytest.raises(ValidationError):
        parse_config(raw)
    raw = _valid_config()
    raw["provider"] = "sendgrid"
    with pytest.raises(ValidationError):
        parse_config(raw)


def test_parse_config_rejects_unknown_timezone():
    raw = _valid_config()
    raw["send_window"] = {"timezone": "Mars/Olympus_Mons"}
    with pytest.raises((ValidationError, Exception)):
        parse_config(raw)


# ── send window / day bounds ────────────────────────────────────────────


def test_in_send_window_weekday_hours():
    window = SendWindow(start_hour=9, end_hour=17, timezone="UTC", days=[1, 2, 3, 4, 5])
    # Wednesday 2026-07-08 10:00 UTC — inside
    assert in_send_window(window, datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc))
    # Wednesday 08:59 — before open
    assert not in_send_window(window, datetime(2026, 7, 8, 8, 59, tzinfo=timezone.utc))
    # Wednesday 17:00 — after close (end_hour exclusive)
    assert not in_send_window(window, datetime(2026, 7, 8, 17, 0, tzinfo=timezone.utc))
    # Saturday 10:00 — weekend
    assert not in_send_window(window, datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc))


def test_in_send_window_respects_timezone():
    window = SendWindow(
        start_hour=9, end_hour=17, timezone="America/New_York", days=[1, 2, 3, 4, 5]
    )
    # 14:00 UTC on a Wednesday == 10:00 New York (EDT) — inside.
    assert in_send_window(window, datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc))
    # 12:00 UTC == 08:00 New York — before open.
    assert not in_send_window(window, datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc))


def test_local_day_bounds_utc():
    window = SendWindow(timezone="America/New_York")
    start, end = local_day_bounds_utc(
        window, datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    )
    # NY midnight EDT == 04:00 UTC
    assert start == datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 9, 4, 0, tzinfo=timezone.utc)


# ── footer / placeholders / bounce ──────────────────────────────────────


def test_compose_footer_contains_can_spam_fields():
    cfg = parse_config(_valid_config())
    footer = compose_footer(cfg.template)
    assert "Davis Fox" in footer
    assert "Flex" in footer
    assert "123 Main St" in footer
    assert "unsubscribe" in footer.lower()


def test_render_placeholders():
    out = render_placeholders(
        "Hi {business_name}, you use {current_software}?",
        {"business_name": "Iron Works", "current_software": None},
    )
    assert out == "Hi Iron Works, you use ?"
    # unknown placeholders stay untouched
    assert render_placeholders("{unknown}", {}) == "{unknown}"


def test_looks_like_bounce():
    assert looks_like_bounce("MAILER-DAEMON@googlemail.com", None)
    assert looks_like_bounce("postmaster@example.com", "anything")
    assert looks_like_bounce("someone@example.com", "Undeliverable: hello")
    assert not looks_like_bounce("owner@gym.com", "Re: quick question")


def test_extract_message_ids():
    body = (
        "The following message to <owner@gym.com> was undeliverable.\n"
        "Message-ID: <abc.123@mail.gmail.com>\n"
    )
    # Plain addresses in angle brackets are indistinguishable from
    # Message-IDs — they're harmless candidates (filtered by the DB
    # lookup); what matters is the real Message-ID is captured.
    assert "<abc.123@mail.gmail.com>" in extract_message_ids(body)
    assert extract_message_ids(None) == []


# ── Formatting markers / HTML rendering ─────────────────────────────────


def _template(**overrides) -> OutreachTemplate:
    base = dict(
        subject="Quick question",
        body="Hi.",
        sender_name="Davis Fox",
        sender_business="Flex & Co",
        physical_address="123 Main St, Nashville, TN 37201",
    )
    base.update(overrides)
    return OutreachTemplate(**base)


def test_strip_markers():
    assert strip_markers("**bold**, *ital*, _under_") == "bold, ital, under"
    # Bold+italic nesting collapses to triple asterisks.
    assert strip_markers("***both***") == "both"
    # Intra-word underscores and spaced asterisks are not formatting.
    assert strip_markers("snake_case_name") == "snake_case_name"
    assert strip_markers("2 * 3 * 4 = 24") == "2 * 3 * 4 = 24"
    assert strip_markers("") == ""
    assert strip_markers(None) == ""


def test_render_email_html_markers_paragraphs_and_escaping():
    t = _template(font_family="georgia", font_size_px=16)
    html = render_email_html(
        "Hi **there**,\n\nTry *this* and _that_ — <script>alert(1)</script>.",
        t,
    )
    assert "<b>there</b>" in html
    assert "<i>this</i>" in html
    assert "<u>that</u>" in html
    assert "<b><i>both</i></b>" in render_email_html("***both***", t)
    # Raw HTML in the body never survives as markup.
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # Two paragraphs.
    assert html.count("<p ") == 2
    # Font settings render as inline style from the whitelist.
    assert "Georgia" in html and "font-size:16px" in html


def test_render_email_html_footer_and_logo():
    t = _template(sender_name="Davis <Fox>")
    html = render_email_html("Hi.", t, logo_cid="tenant-logo")
    assert 'src="cid:tenant-logo"' in html
    # CAN-SPAM identity present and escaped.
    assert "Davis &lt;Fox&gt;" in html
    assert "Flex &amp; Co" in html
    assert "123 Main St, Nashville, TN 37201" in html
    assert "unsubscribe" in html.lower()


def test_render_email_html_defaults_have_no_logo_or_font():
    html = render_email_html("Hi.", _template())
    assert "cid:" not in html
    # No font overrides on the wrapper — recipient client defaults apply.
    assert html.startswith('<div style="line-height:1.45;">')
    assert "font-family" not in html


def test_template_font_family_whitelist():
    assert _template(font_family="Arial").font_family == "arial"
    assert _template(font_family=None).font_family is None
    with pytest.raises(ValidationError):
        _template(font_family="comic sans")
    with pytest.raises(ValidationError):
        _template(font_size_px=99)


def test_parse_config_attachments():
    raw = {
        "template": {
            "subject": "s",
            "body": "b",
            "sender_name": "n",
            "sender_business": "biz",
            "physical_address": "addr",
        },
        "attachments": [
            {"s3_key": "tenants/x/outreach/ab-deck.pdf", "filename": "deck.pdf"}
        ],
    }
    cfg = parse_config(raw)
    assert cfg.attachments[0].filename == "deck.pdf"
    assert parse_config({"template": raw["template"]}).attachments == []
    with pytest.raises(ValidationError):
        parse_config({**raw, "attachments": raw["attachments"] * 6})
    with pytest.raises(ValidationError):
        parse_config({**raw, "attachments": [{"filename": "deck.pdf"}]})


# ── Inline links + custom footer (2026-07 campaign additions) ───────────


def test_link_marker_renders_anchor_and_plain_text():
    t = _template(body="x")
    html = render_email_html("book a meeting with me [here](https://flexonline.net/book).", t)
    assert '<a href="https://flexonline.net/book" style="color:#2563eb;">here</a>' in html
    # The plain part keeps the destination — "label: url", never stripped.
    assert strip_markers("book [here](https://flexonline.net/book).") == (
        "book here: https://flexonline.net/book."
    )


def test_link_marker_prepends_missing_scheme():
    t = _template(body="x")
    html = render_email_html("[Book a 15-minute demo](flexonline.net/book)", t)
    assert (
        '<a href="https://flexonline.net/book" style="color:#2563eb;">'
        "Book a 15-minute demo</a>"
    ) in html
    # The plain part shows the URL exactly as typed — no prepending.
    assert strip_markers("[Book a 15-minute demo](flexonline.net/book)") == (
        "Book a 15-minute demo: flexonline.net/book"
    )


def test_link_marker_http_only_and_no_injection():
    # Non-http(s) schemes fail the domain pattern and stay literal in
    # BOTH parts — javascript: can never become an href.
    assert strip_markers("[x](javascript:alert(1))") == "[x](javascript:alert(1))"
    t = _template(body="x")
    html = render_email_html("[x](javascript:alert(1))", t)
    assert "<a" not in html and "javascript:alert(1)" in html
    # Link text is escaped before the anchor is built.
    html = render_email_html('[<img>](https://a.io/p)', t)
    assert "&lt;img&gt;" in html and "<img>" not in html


def test_link_label_may_carry_style_markers():
    t = _template(body="x")
    html = render_email_html("[**Book** a demo](flexonline.net/book)", t)
    assert (
        '<a href="https://flexonline.net/book" style="color:#2563eb;">'
        "<b>Book</b> a demo</a>"
    ) in html
    assert strip_markers("[**Book** a demo](flexonline.net/book)") == (
        "Book a demo: flexonline.net/book"
    )


def test_link_url_with_path_and_query():
    t = _template(body="x")
    html = render_email_html("[pricing](https://gym.example/p?a=1&b=2)", t)
    assert '<a href="https://gym.example/p?a=1&amp;b=2"' in html
    assert strip_markers("[pricing](https://gym.example/p?a=1&b=2)") == (
        "pricing: https://gym.example/p?a=1&b=2"
    )


def test_two_adjacent_links():
    t = _template(body="x")
    html = render_email_html("[a](one.example/x)[b](two.example/y)", t)
    assert '<a href="https://one.example/x" style="color:#2563eb;">a</a>' in html
    assert '<a href="https://two.example/y" style="color:#2563eb;">b</a>' in html
    assert strip_markers("[a](one.example/x) [b](two.example/y)") == (
        "a: one.example/x b: two.example/y"
    )


def test_square_bracket_text_without_url_stays_literal():
    t = _template(body="x")
    body = "per the notes [see appendix] and [x](not a url) and [n](1.55)"
    assert strip_markers(body) == body
    html = render_email_html(body, t)
    assert "<a" not in html
    assert "[see appendix]" in html and "[x](not a url)" in html


def test_link_inside_bold_and_url_marker_chars_not_formatting():
    t = _template(body="x")
    html = render_email_html("**see [docs](https://a.io/my_page_here) now**", t)
    assert "<b>" in html and '<a href="https://a.io/my_page_here"' in html
    assert "<u>" not in html
    # Marker characters at a path segment's edges must not corrupt the
    # href either — the style passes never see the URL.
    html = render_email_html("[docs](https://a.io/_v2_/guide) and [x](a.io/*y*)", t)
    assert '<a href="https://a.io/_v2_/guide"' in html
    assert '<a href="https://a.io/*y*"' in html
    assert "<u>" not in html and "<i>" not in html


def test_footer_text_overrides_default_everywhere():
    custom = (
        '2026 DF Consulting LLC · Flex Online · 155 Marine St., St. Augustine, FL 32084 · '
        'Reply "Unsubscribe" to be removed from our mailing list'
    )
    t = _template(footer_text=custom)
    footer = compose_footer(t)
    assert footer == "\n\n--\n" + custom
    assert "rather not hear from me" not in footer
    html = render_email_html("Hi.", t)
    assert "DF Consulting LLC" in html
    assert "&quot;Unsubscribe&quot;" in html
    assert "rather not hear from me" not in html
    # Default (no override) still renders the stock three-liner.
    assert "rather not hear from me" in render_email_html("Hi.", _template())
