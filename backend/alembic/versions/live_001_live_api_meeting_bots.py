"""Live-transcription API productization + meeting-bot connector schema.

Two additive changes, both safe under Fly's auto-migrate deploy:

* ``live_sessions.external_call_id`` — nullable provider-side call id
  (Twilio CallSid, SIPREC session id, meeting-bot id) so dialers/CRMs
  can screen-pop the live view via ``GET /live-sessions/lookup`` with
  the id they already hold. Indexed for that lookup; plus a composite
  ``(tenant_id, status)`` index serving the active-session listing.

* ``meeting_bot_jobs`` — lifecycle table for vendor meeting bots
  (Zoom / Meet / Teams meetings). One row per dispatched bot; the
  vendor bot id anchors webhook correlation and the linked
  ``LiveSession`` carries the transcript.

Schema mirrors :class:`MeetingBotJob` in ``backend/app/models.py``.
Branches off the telephony-streams merge point; deploys run
``alembic upgrade heads`` so parallel heads are expected.

Revision ID: live_001_live_api_meeting_bots
Revises: 9b8d07fc6ee9
Create Date: 2026-08-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "live_001_live_api_meeting_bots"
down_revision: Union[str, None] = "9b8d07fc6ee9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGAL_STATES = ("requested", "joining", "in_call", "done", "failed")


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column("external_call_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_live_sessions_external_call_id",
        "live_sessions",
        ["external_call_id"],
    )
    op.create_index(
        "ix_live_sessions_tenant_status",
        "live_sessions",
        ["tenant_id", "status"],
    )

    op.create_table(
        "meeting_bot_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "live_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("live_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "requested_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "provider",
            sa.String(),
            nullable=False,
            server_default=sa.text("'recall'"),
        ),
        sa.Column("bot_id", sa.String(), nullable=True),
        sa.Column("meeting_url", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'requested'"),
        ),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN (" + ", ".join("'{0}'".format(s) for s in _LEGAL_STATES) + ")",
            name="ck_meeting_bot_jobs_status",
        ),
    )
    op.create_index(
        "ix_meeting_bot_jobs_tenant_id", "meeting_bot_jobs", ["tenant_id"]
    )
    op.create_index(
        "ix_meeting_bot_jobs_bot_id", "meeting_bot_jobs", ["bot_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_meeting_bot_jobs_bot_id", table_name="meeting_bot_jobs")
    op.drop_index("ix_meeting_bot_jobs_tenant_id", table_name="meeting_bot_jobs")
    op.drop_table("meeting_bot_jobs")
    op.drop_index("ix_live_sessions_tenant_status", table_name="live_sessions")
    op.drop_index("ix_live_sessions_external_call_id", table_name="live_sessions")
    op.drop_column("live_sessions", "external_call_id")
