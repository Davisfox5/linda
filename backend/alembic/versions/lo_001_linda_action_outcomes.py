"""linda_action_outcomes — did Linda's confirmed actions actually work?

Revision ID: lo_001_linda_outcomes
Revises: cmp_001_campaign_kinds
Create Date: 2026-08-12 00:00:00.000000

One row per decided write proposal (docs/plans/ask-linda-agentic-gaps.md,
gap G5). ``WriteProposal`` records that a write was staged and decided;
nothing recorded whether the action then worked, and cancels — the
strongest negative signal available — were discarded entirely. This table
is the chat-side equivalent of ``insight_quality_scores``: the evidence
base for tuning tool descriptions, tiers and proposal thresholds.

``outcome`` starts 'pending' and is resolved by the ``linda_outcome_scan``
beat task from deterministic downstream state (an action item closing, an
outreach member replying, a step completing). Nothing here is judged by a
model.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision: str = "lo_001_linda_outcomes"
down_revision: Union[str, None] = "cmp_001_campaign_kinds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "linda_action_outcomes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("resulting_entity_id", sa.UUID(), nullable=True),
        sa.Column(
            "outcome", sa.String(length=16), nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "outcome_detail", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "observation_attempts", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["write_proposals.id"], ondelete="CASCADE"
        ),
        # One outcome per proposal — the observer is idempotent and the
        # recorder is called from the confirm/cancel paths, which can be
        # retried.
        sa.UniqueConstraint("proposal_id", name="uq_linda_outcome_proposal"),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'cancelled', 'expired')",
            name="ck_linda_action_outcomes_decision",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'succeeded', 'failed', 'rejected', 'no_signal')",
            name="ck_linda_action_outcomes_outcome",
        ),
    )
    op.create_index(
        "ix_linda_action_outcomes_tenant_id", "linda_action_outcomes", ["tenant_id"]
    )
    op.create_index(
        "ix_linda_action_outcomes_created_at", "linda_action_outcomes", ["created_at"]
    )
    # The observer's hot query: pending rows for one tenant.
    op.create_index(
        "ix_linda_outcomes_tenant_outcome",
        "linda_action_outcomes",
        ["tenant_id", "outcome"],
    )
    op.create_index(
        "ix_linda_outcomes_tenant_kind",
        "linda_action_outcomes",
        ["tenant_id", "kind"],
    )

    # RLS for the new table — the new-table checklist in
    # tests/test_rls_scoping_guard.py + rls_002_all_tables's docstring.
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    import os

    from backend.app import rls

    for stmt in rls.rls_statements(tables=["linda_action_outcomes"]):
        conn.execute(sa.text(stmt))

    role = os.environ.get("APP_DB_ROLE", "linda_app")
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar()
    if exists:
        conn.execute(
            sa.text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON linda_action_outcomes "
                "TO {r}".format(r=role)
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        for policy in (
            "tenant_isolation_select",
            "tenant_isolation_insert",
            "tenant_isolation_update",
            "tenant_isolation_delete",
        ):
            conn.execute(
                sa.text(
                    "DROP POLICY IF EXISTS {p} ON linda_action_outcomes".format(
                        p=policy
                    )
                )
            )
    op.drop_index(
        "ix_linda_outcomes_tenant_kind", table_name="linda_action_outcomes"
    )
    op.drop_index(
        "ix_linda_outcomes_tenant_outcome", table_name="linda_action_outcomes"
    )
    op.drop_index(
        "ix_linda_action_outcomes_created_at", table_name="linda_action_outcomes"
    )
    op.drop_index(
        "ix_linda_action_outcomes_tenant_id", table_name="linda_action_outcomes"
    )
    op.drop_table("linda_action_outcomes")
