"""Celery worker and task definitions — batch processing pipeline.

All async service calls are wrapped with ``asyncio.run()`` because Celery
tasks execute synchronously.  Database access uses a synchronous
SQLAlchemy session created via :func:`_get_sync_session`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from celery import Celery, Task
from celery.schedules import crontab
from celery.signals import (
    beat_init,
    task_failure,
    task_postrun,
    task_prerun,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import get_settings
from backend.app.logging_setup import (
    bind_context,
    configure_logging,
    reset_context,
)
from backend.app.observability import init_sentry
from backend.app.services.pipeline_ledger import StepHeldError

configure_logging()
init_sentry()

logger = logging.getLogger(__name__)

settings = get_settings()

# ── Celery app ───────────────────────────────────────────────────────────


class _LindaBaseTask(Task):
    """Base task: an open LLM credit breaker defers work, never errors.

    ``llm_circuit_breaker.LLMCallsSuspended`` marks an EXPECTED state
    (credit balance deliberately low). The pollers that do heavy LLM
    work handle it locally with a rollback; this backstop covers every
    other task (orchestrator, digests, KB refiners, …) so none of them
    turns the paused state into a Sentry-visible failure. Breaker
    open/close each report exactly once — a per-task ERROR here would
    reintroduce the noise the breaker exists to stop.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().__call__(*args, **kwargs)
        except Exception as exc:
            from backend.app.services.llm_circuit_breaker import LLMCallsSuspended

            if isinstance(exc, LLMCallsSuspended):
                logger.info(
                    "task %s deferred: LLM credit breaker open", self.name
                )
                return {"status": "deferred", "reason": "llm_credit_breaker_open"}
            raise


celery_app = Celery(
    "linda",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    task_cls=_LindaBaseTask,
)

# TLS Redis (rediss://) requires Celery to be told how to validate
# certs explicitly. redis-py and Celery accept different URL-query
# spellings for this, so we set it via celery config rather than the
# URL. CERT_NONE matches redis-py's default behaviour and is what
# most managed Redis providers (Upstash, ElastiCache) end up using
# in practice. Tighten to CERT_REQUIRED if + when you provision a
# trusted CA bundle in the worker image.
if settings.REDIS_URL.startswith("rediss://"):
    import ssl as _ssl

    celery_app.conf.update(
        broker_use_ssl={"ssl_cert_reqs": _ssl.CERT_NONE},
        redis_backend_use_ssl={"ssl_cert_reqs": _ssl.CERT_NONE},
    )

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # task_track_started was True historically — Celery wrote a STARTED
    # state to the result backend on dequeue, then SUCCESS/FAILURE on
    # completion. We never read STARTED anywhere (grep -r STARTED
    # confirms zero callers in app code; only CRM/audiohook string
    # literals match). Disabling halves task result-backend writes,
    # which is the second-largest contributor to Redis command volume
    # on Upstash's per-command billing.
    task_track_started=False,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Expire task results after 6h. Without this, every Celery result
    # accumulates in Redis forever (default behavior) and bloats memory
    # over weeks. 6h is well above the longest in-flight chord aggregator
    # (the daily orchestrator can run > 1h on a slow tenant cohort) while
    # still bounding Redis growth.
    result_expires=21600,
    # ── Redis broker tuning for per-command billing ──
    # Celery's default Redis transport polls BRPOP with a ~1s block,
    # which generates one command per worker per second even when the
    # queues are completely idle (4 workers = 345K commands/day on
    # staging baseline, ~70% of Upstash's free-tier 500K cap before
    # any actual work).
    # Raising the BRPOP block timeout to 30s drops that floor by 30x.
    # Trade-off: task pickup latency on an empty queue can be up to
    # 30s; with task_acks_late this only affects how quickly an
    # already-running worker reaches for the *next* task, never how
    # quickly an enqueued task starts running on an idle worker
    # (LPUSH wakes any blocked BRPOP immediately).
    # visibility_timeout is the existing default; we name it
    # explicitly so it doesn't drift if Celery changes upstream.
    broker_transport_options={
        "socket_timeout": 30,
        "socket_connect_timeout": 30,
        "socket_keepalive": True,
        "visibility_timeout": 3600,
        # The poll/BRPOP block window. See comment above.
        "polling_interval": 30.0,
    },
    redis_backend_transport_options={
        "socket_timeout": 30,
        "socket_connect_timeout": 30,
        "socket_keepalive": True,
    },
    # mingle/gossip are worker-to-worker coordination over Redis
    # pub/sub. On a single-worker fly process they're pure overhead
    # (and they spike on every deploy). Disabling saves a startup
    # pub/sub burst per machine restart.
    worker_enable_remote_control=False,
    worker_send_task_events=False,
    # ── Queue routing ────────────────────────────────────────────
    # Three queues so SLA-sensitive customer-facing work (voice / email
    # interaction processing, push notifications) can't queue behind
    # a slow nightly backup or weekly aggregation. The worker process
    # is started with ``-Q priority,default,batch`` and Celery drains
    # them in that listed order, so priority always pre-empts default
    # which always pre-empts batch.
    #
    # Routing here uses glob patterns evaluated against the task
    # ``name`` (the value passed to ``@celery_app.task(name=...)`),
    # not the Python function name — keeps the routes intelligible
    # without sprinkling ``queue=`` on every decorator.
    task_default_queue="default",
    task_routes={
        # Customer-facing realtime path.
        "process_voice_interaction": {"queue": "priority"},
        "process_text_interaction": {"queue": "priority"},
        "email_push_process_gmail": {"queue": "priority"},
        "email_push_process_graph": {"queue": "priority"},
        # Heavy nightly/weekly batch work — keeps it off the path of
        # customer-facing interaction processing.
        "tenant_backup_*": {"queue": "batch"},
        "tenant_export_to_s3": {"queue": "batch"},
        "audio_retention_sweep": {"queue": "batch"},
        "event_retention_sweep": {"queue": "batch"},
        "cross_tenant_aggregate_metrics": {"queue": "batch"},
        "recompute_llm_ceilings": {"queue": "batch"},
        "support_trend_scan": {"queue": "batch"},
        "cohort_recommendation_scan": {"queue": "batch"},
        "sales_trend_scan": {"queue": "batch"},
        "cs_trend_scan": {"queue": "batch"},
        "concern_aggregation_scan": {"queue": "batch"},
        "broken_commitment_scan": {"queue": "batch"},
        "orchestrator_daily_all_tenants": {"queue": "batch"},
        "orchestrator_weekly_all_tenants": {"queue": "batch"},
        "tenant_insights_weekly": {"queue": "batch"},
        "calibration_fit_all_tenants": {"queue": "batch"},
        "irt_fit_all_tenants": {"queue": "batch"},
        "churn_train_all_tenants": {"queue": "batch"},
        "outcomes_backfill_all_tenants": {"queue": "batch"},
        # User-triggered but long-running (up to 2000 provider fetches) —
        # keep it off the customer-facing priority lane.
        "email_backfill_run": {"queue": "batch"},
        # Cold-outreach draft fan-out: one Sonnet call per prospect —
        # long-running and never latency-sensitive.
        "outreach_generate_drafts": {"queue": "batch"},
        "refresh_few_shot_pools": {"queue": "batch"},
        "compute_wer_weekly": {"queue": "batch"},
        "discover_vocabulary_candidates": {"queue": "batch"},
        "vocabulary_digest_weekly": {"queue": "batch"},
        "tenant_brief_refiner_weekly": {"queue": "batch"},
        "infer_from_sources_weekly": {"queue": "batch"},
        # Background embed of a single support case — non-realtime but
        # not a giant nightly sweep either. Default queue keeps it off
        # the priority lane while still draining quickly.
        "embed_support_case_subject": {"queue": "default"},
    },
    beat_schedule={
        # Weekly rollup: every Monday 00:15 UTC, covering the prior Mon–Sun.
        "tenant-insights-weekly": {
            "task": "tenant_insights_weekly",
            "schedule": crontab(minute=15, hour=0, day_of_week=1),
        },
        # ── Scoring / orchestrator cadences ──────────────────────────
        "orchestrator-daily": {
            "task": "orchestrator_daily_all_tenants",
            "schedule": crontab(minute=0, hour=4),
        },
        "orchestrator-weekly": {
            "task": "orchestrator_weekly_all_tenants",
            "schedule": crontab(minute=0, hour=5, day_of_week=1),
        },
        "outcomes-backfill-daily": {
            "task": "outcomes_backfill_all_tenants",
            "schedule": crontab(minute=30, hour=3),
        },
        # 3c detect-and-heal: re-run entity resolution for interactions
        # whose resolution step failed (ledger status 'failed'). Hourly;
        # bounded batch, idempotent, concurrency-safe via the ledger.
        "reconcile-orphan-interactions": {
            "task": "reconcile_orphan_interactions",
            "schedule": crontab(minute=25),
        },
        "calibration-weekly": {
            "task": "calibration_fit_all_tenants",
            "schedule": crontab(minute=0, hour=6, day_of_week=1),
        },
        "irt-calibration-weekly": {
            "task": "irt_fit_all_tenants",
            "schedule": crontab(minute=30, hour=6, day_of_week=1),
        },
        "churn-model-weekly": {
            "task": "churn_train_all_tenants",
            "schedule": crontab(minute=0, hour=7, day_of_week=1),
        },
        # Teams Graph subscriptions expire in as little as ~60 minutes
        # and Microsoft never retries a lapsed one — renew on a cadence
        # tighter than the sweep's within_minutes=15 slack.
        "renew-teams-subscriptions": {
            "task": "renew_teams_subscriptions",
            "schedule": crontab(minute="*/15"),
        },
        # Audio retention runs daily — tenants that care about < 24h audio
        # windows are rare; daily amortizes the S3 list-delete against 23
        # near-noop hourly sweeps.
        "audio-retention-sweep": {
            "task": "audio_retention_sweep",
            "schedule": crontab(minute=15, hour=4),
        },
        # QBR-overdue scan: daily after the orchestrator + audio sweep so
        # last-CS-interaction reads see fresh data. 06:00 UTC keeps the
        # ping out of late-night CSM hours; per-customer dedup (14d) keeps
        # the same overdue account from re-pinging every day.
        "qbr-overdue-scan": {
            "task": "qbr_overdue_scan",
            "schedule": crontab(minute=0, hour=6),
        },
        # Cohort-driven predictive recommendation scan: daily at 06:30
        # UTC, after the QBR scan + the existing recommendation builder
        # (04:30, reads BusinessProfile). Deterministic detectors, no
        # LLM cost; dedup over a 14-day window per (category, customer).
        "cohort-recommendation-scan": {
            "task": "cohort_recommendation_scan",
            "schedule": crontab(minute=30, hour=6),
        },
        # AI cross-customer trend detection. Daily at 07:00 UTC, after
        # the cohort scan, so the recommendation queue reflects both
        # cohort-derived and trend-derived predictive recommendations
        # on the same refresh cycle. Voyage embedding is the dominant
        # per-tenant cost; nightly batches are cheap.
        "support-trend-scan": {
            "task": "support_trend_scan",
            "schedule": crontab(minute=0, hour=7),
        },
        # Same AI trend-detection idea, generalized to Sales / CS / cross-
        # customer concerns (see ``trend_engine.py``). Staggered 15 min
        # apart after the support scan so they don't all hit Voyage/the
        # DB in the same instant.
        "sales-trend-scan": {
            "task": "sales_trend_scan",
            "schedule": crontab(minute=15, hour=7),
        },
        "cs-trend-scan": {
            "task": "cs_trend_scan",
            "schedule": crontab(minute=30, hour=7),
        },
        "concern-aggregation-scan": {
            "task": "concern_aggregation_scan",
            "schedule": crontab(minute=45, hour=7),
        },
        # Deterministic, no LLM/Voyage cost — a commitment is "broken"
        # purely by the clock. Runs after the trend scans, still well
        # before business hours.
        "broken-commitment-scan": {
            "task": "broken_commitment_scan",
            "schedule": crontab(minute=0, hour=8),
        },
        # ── Email ingestion ───────────────────────────────────────────
        # Real-time delivery comes from Gmail Pub/Sub + Graph push. This
        # poll is a safety net for integrations whose push subscription
        # hasn't been set up yet — see email_ingest_poll() for the filter.
        "email-ingest-poll": {
            "task": "email_ingest_poll",
            "schedule": 900.0,  # 15 minutes
        },
        # ── Cold outreach ─────────────────────────────────────────────
        # Sends due, approved campaign email inside each campaign's send
        # window, respecting the per-campaign daily limit and the tenant-
        # wide cap. 10 min × OUTREACH_MAX_SENDS_PER_TICK spreads a 25/day
        # quota across the window instead of bursting it at window open.
        "outreach-scheduler-tick": {
            "task": "outreach_scheduler_tick",
            "schedule": 600.0,
        },
        # Sample Celery queue depths into Prometheus gauges. Previously
        # every 30s, which generated ~5.7K commands/day for a gauge
        # nobody watches in real time. 5 min is plenty for backpressure
        # alerts (a queue going from 0 to "spiking" is a multi-minute
        # event in practice, not a per-second one) and drops the cost
        # by 10x.
        "sample-queue-depth": {
            "task": "sample_queue_depth",
            "schedule": 300.0,
        },
        # Nightly GDPR-export backup per tenant — lands in the staging
        # bucket under backups/{tenant}/{timestamp}.ndjson.gz. Tenants
        # can opt out via features_enabled.scheduled_backups = False.
        "nightly-tenant-backup": {
            "task": "tenant_backup_all_tenants",
            "schedule": crontab(minute=0, hour=2),
        },
        "email-push-renew": {
            "task": "email_push_renew_subscriptions",
            "schedule": 43200.0,
        },
        # ── Continuous AI improvement ─────────────────────────────────
        # Drain the feedback Redis stream every minute. Previously ran every
        # 30s; the stream is rarely hot and each run costs a task schedule +
        # Redis RTT.
        "consume-feedback-stream": {
            "task": "consume_feedback_stream",
            "schedule": 60.0,
        },
        # Daily sweep of webhook_deliveries + feedback_events. Raw rows
        # age out; feedback_events roll up into feedback_daily_rollup so
        # calibration never loses historical volume.
        "event-retention-daily": {
            "task": "event_retention_sweep",
            "schedule": crontab(minute=45, hour=4),
        },
        # Safety net for webhook deliveries orphaned in ``pending`` (task
        # raced the caller's commit, or a scheduled retry was lost to a
        # worker restart). Re-enqueues young orphans, dead-letters stale
        # ones. See webhook_pending_sweep.
        "webhook-pending-sweep": {
            "task": "webhook_pending_sweep",
            "schedule": 600.0,
        },
        "refresh-few-shot-pools": {
            "task": "refresh_few_shot_pools",
            "schedule": crontab(minute=0, hour=3),
        },
        "compute-wer-weekly": {
            "task": "compute_wer_weekly",
            "schedule": crontab(minute=0, hour=2, day_of_week=0),
        },
        "discover-vocabulary-candidates": {
            "task": "discover_vocabulary_candidates",
            "schedule": crontab(minute=0, hour=3, day_of_week=0),
        },
        "vocabulary-digest-weekly": {
            "task": "vocabulary_digest_weekly",
            "schedule": crontab(minute=0, hour=9, day_of_week=1),
        },
        "cross-tenant-aggregate-metrics": {
            "task": "cross_tenant_aggregate_metrics",
            "schedule": crontab(minute=30, hour=0, day_of_week=1),
        },
        "quality-regression-check": {
            "task": "quality_regression_check",
            "schedule": 3600.0,
        },
        "variant-winner-selection": {
            "task": "variant_winner_selection",
            "schedule": crontab(minute=0, hour=4, day_of_week="2,5"),
        },
        "campaign-variant-winner-selection": {
            "task": "campaign_variant_winner_selection",
            "schedule": crontab(minute=15, hour=4, day_of_week="2,5"),
        },
        # ── KB / CRM / telephony cadences ─────────────────────────────
        "vector-health-daily": {
            "task": "vector_health_daily",
            "schedule": crontab(minute=30, hour=0),
        },
        "tenant-brief-refiner-weekly": {
            "task": "tenant_brief_refiner_weekly",
            "schedule": crontab(minute=45, hour=1, day_of_week=1),
        },
        "infer-from-sources-weekly": {
            "task": "infer_from_sources_weekly",
            "schedule": crontab(minute=15, hour=2, day_of_week=1),
        },
        "crm-sync-daily": {
            "task": "crm_sync_daily",
            "schedule": crontab(minute=0, hour=3),
        },
        # Daily trial-expiry sweep at 09:00 UTC (mid-EU day, post-US
        # close). Emits 3/1/0-day notices for sandbox tenants and
        # flips subscription_status="expired" on day 0.
        "trial-expiry-daily": {
            "task": "trial_expiry_daily",
            "schedule": crontab(minute=0, hour=9),
        },
        # ── Manager-view (Phase: manager-view-overhaul) ───────────────
        # Anomaly scan every 15 minutes. Three SQL detectors per tenant
        # against ``interactions.insights``; each detector is a single
        # window query. Cheap. Dial to longer if the false-positive
        # rate climbs after two weeks of real traffic.
        "manager-anomaly-scan": {
            "task": "manager_anomaly_scan_all_tenants",
            "schedule": 900.0,
        },
        # Recommendation builder runs daily right after the orchestrator
        # finishes refreshing the BusinessProfile. One Haiku call per
        # tenant, capped at 5 recommendations.
        "manager-recommendations-build": {
            "task": "manager_recommendations_build",
            "schedule": crontab(minute=30, hour=4),
        },
        # Sweep old recommendations whose 14-day expires_at has passed.
        "manager-recommendations-expire": {
            "task": "manager_recommendations_expire",
            "schedule": crontab(minute=0, hour=3),
        },
        # Auto-resolve manager alerts whose underlying spike has subsided.
        # Frees the partial-unique fingerprint slot so a recurring spike
        # can re-fire.
        "manager-anomaly-resolve": {
            "task": "manager_anomaly_resolve",
            "schedule": crontab(minute=0, hour="*/6"),
        },
        # Nightly: customer concerns not mentioned in 90 days go dormant
        # so stale worries stop flooding briefs and token budgets.
        "customer-memory-dormant-sweep": {
            "task": "customer_memory_dormant_sweep",
            "schedule": crontab(minute=20, hour=1),
        },
        # Recompute the adaptive ``max_tokens`` ceiling per (call_site, tier)
        # from the rolling 14-day usage window. Daily, after the nightly
        # backup window settles. Requires at least 200 samples OR 14 days
        # of history per (call_site, tier) before publishing a learned
        # ceiling; until then call sites keep the static tier cap.
        "recompute-llm-ceilings": {
            "task": "recompute_llm_ceilings",
            "schedule": crontab(minute=50, hour=4),
        },
    },
)

# ── Worker lifecycle hooks ───────────────────────────────────────────────


def _start_process_metrics_server() -> None:
    """Expose /metrics on this machine so Fly's Prometheus can scrape the
    celery worker / beat (neither runs the FastAPI app, so they'd otherwise
    have no metrics endpoint). Fires once in the main process — the prefork
    children record into PROMETHEUS_MULTIPROC_DIR and this server aggregates
    them. Failures must never block the worker from starting."""
    try:
        from backend.app.services.metrics import start_metrics_server

        start_metrics_server(int(os.environ.get("METRICS_PORT", "8000")))
    except Exception:  # pragma: no cover - telemetry must not crash the worker
        logger.warning("metrics: failed to start worker metrics server", exc_info=True)


@worker_init.connect
def _on_worker_init(**_kwargs: Any) -> None:
    _start_process_metrics_server()


@beat_init.connect
def _on_beat_init(**_kwargs: Any) -> None:
    _start_process_metrics_server()


@worker_process_shutdown.connect
def _on_worker_child_exit(pid: Optional[int] = None, **_kwargs: Any) -> None:
    """Keep multiprocess gauges accurate as celery recycles prefork children."""
    try:
        from backend.app.services.metrics import mark_process_dead

        if pid is not None:
            mark_process_dead(pid)
    except Exception:  # pragma: no cover
        pass


@worker_process_init.connect
def _on_worker_start(**_kwargs: Any) -> None:
    """Warm up heavy models so the first task doesn't pay a cold-start tax.

    Two models are worth pre-loading:

    * pyannote.audio speaker-diarization-3.1 (~500 MB) — used by the
      Whisper transcription path for speaker labels.
    * SpeechBrain emotion-recognition-wav2vec2-IEMOCAP (~1 GB) — used
      when a tenant has ``emotion_classification`` enabled.

    We swallow failures so a model fetch outage doesn't refuse the
    worker from starting — individual tasks degrade gracefully when
    the model isn't loaded.

    Set ``LINDA_WORKER_WARMUP=0`` in the env to skip (useful for
    beat-only workers that never run audio tasks).
    """
    if os.environ.get("LINDA_WORKER_WARMUP", "1") == "0":
        logger.info("Worker warmup skipped (LINDA_WORKER_WARMUP=0)")
        return

    # Reconfigure logging + sentry in the fresh process.
    configure_logging()
    init_sentry()

    try:
        from backend.app.services.transcription import _get_diarization_pipeline

        if _get_diarization_pipeline() is not None:
            logger.info("pyannote diarization pipeline preloaded")
    except Exception:
        logger.debug("pyannote warmup failed (non-fatal)", exc_info=True)

    try:
        from backend.app.services.paralinguistics_emotion import (
            prefetch_emotion_classifier,
        )

        if prefetch_emotion_classifier():
            logger.info("speechbrain emotion classifier preloaded")
    except Exception:
        logger.debug("speechbrain warmup failed (non-fatal)", exc_info=True)


@task_prerun.connect
def _on_task_prerun(sender: Any = None, task_id: str = "", args=None, kwargs=None, **_: Any) -> None:
    """Bind correlation ids onto the per-task context.

    We set ``request_id`` to the Celery task id so logs fan-out under
    a single key across the pipeline. Some tasks also take an
    interaction id as the first positional arg — we pick that up
    automatically to keep per-interaction grepping easy.
    """
    values: Dict[str, Optional[str]] = {"request_id": task_id}
    if args:
        first = args[0] if not isinstance(args[0], (int, float, bool)) else None
        if isinstance(first, str) and len(first) == 36:  # likely a UUID
            values["interaction_id"] = first
    # Stash tokens on the task request so postrun can reset them.
    task_request = getattr(sender, "request", None)
    if task_request is not None:
        task_request.linda_context_tokens = bind_context(**values)
    else:
        bind_context(**values)

    # RLS: bind the task's tenant context from its arguments so every DB
    # query the task runs is scoped (fail closed otherwise). Tasks whose
    # tenant isn't derivable from arguments (all-tenant orchestrators)
    # bind per iteration in their own bodies instead.
    try:
        rls_token = _rls_binding_for_task(sender, args, kwargs)
    except Exception:
        logger.exception(
            "RLS tenant binding failed for task %s — task will run "
            "UNSCOPED and see zero tenant-scoped rows",
            getattr(sender, "name", sender),
        )
        rls_token = None
    if task_request is not None:
        task_request.linda_rls_token = rls_token


# Task argument names whose value identifies the tenant indirectly; each
# maps to the table whose SECURITY DEFINER resolver turns it into a
# tenant id (see backend.app.rls.TENANT_RESOLVER_FUNCTIONS).
_RLS_ARG_RESOLVERS = (
    ("interaction_id", "interactions"),
    ("interaction_id_str", "interactions"),
    ("integration_id", "integrations"),
    ("job_id", "email_backfill_jobs"),
    ("case_id", "support_cases"),
    ("rec_id", "manager_recommendations"),
    ("delivery_id", "webhook_deliveries"),
)


def _rls_binding_for_task(sender: Any, args, kwargs) -> Optional[Any]:
    """Bind the tenant ContextVar from task arguments; return the reset
    token (None when no tenant is derivable from the signature)."""
    import inspect

    from backend.app.tenant_ctx import resolve_tenant_via, set_current_tenant

    run = getattr(sender, "run", None)
    if run is None:
        return None
    try:
        params = list(inspect.signature(run).parameters)
    except (TypeError, ValueError):
        return None
    if params and params[0] == "self":
        params = params[1:]
    bound: Dict[str, Any] = dict(zip(params, args or ()))
    bound.update(kwargs or {})

    tenant_id = bound.get("tenant_id")
    if tenant_id:
        return set_current_tenant(str(tenant_id))

    for arg_name, table in _RLS_ARG_RESOLVERS:
        row_id = bound.get(arg_name)
        if not row_id:
            continue
        session = _get_sync_session()
        try:
            resolved = resolve_tenant_via(session, table, row_id)
        finally:
            session.close()
        if resolved is not None:
            return set_current_tenant(resolved)
        return None  # row not found — the task body will report it
    return None


@task_failure.connect
def _on_task_failure(
    sender: Any = None,
    task_id: str = "",
    exception: Optional[BaseException] = None,
    einfo: Any = None,
    args: Any = None,
    kwargs: Any = None,
    **_: Any,
) -> None:
    """Dead-letter logging for terminal task failures.

    Celery fires ``task_failure`` whenever an exception propagates out of
    a task — including the final retry. Detecting "this was the last
    attempt" requires checking ``sender.request.retries`` against the
    task's ``max_retries``: when they're equal *and* the task isn't
    asking for another retry, the work is permanently lost without a
    dead-letter signal.

    The previous behaviour was that terminal failures landed only as a
    state=FAILURE marker in Redis + a generic ``failure`` counter; there
    was no visibility into *which* task instance / args were dropped.
    """
    try:
        request = getattr(sender, "request", None)
        retries = getattr(request, "retries", 0) if request is not None else 0
        max_retries = getattr(sender, "max_retries", None)
        is_terminal = max_retries is None or retries >= int(max_retries)
        if not is_terminal:
            return  # an intermediate-retry failure; not dead-lettered yet

        task_name = getattr(sender, "name", "unknown")
        logger.error(
            "celery dlq: task=%s task_id=%s retries=%s/%s exc=%r args=%r kwargs=%r",
            task_name, task_id, retries, max_retries, exception, args, kwargs,
        )
        # Prometheus counter for "terminal failure" alerts.
        from backend.app.services.metrics import CELERY_TASK_RUNS
        CELERY_TASK_RUNS.labels(task_name=task_name, status="dead_letter").inc()
    except Exception:  # pragma: no cover — telemetry is best-effort
        logger.debug("dlq handler failed", exc_info=True)


@task_postrun.connect
def _on_task_postrun(
    sender: Any = None,
    task_id: str = "",
    state: str = "",
    runtime: Optional[float] = None,
    **_: Any,
) -> None:
    task_request = getattr(sender, "request", None)
    tokens = getattr(task_request, "linda_context_tokens", None) if task_request else None
    if tokens:
        reset_context(tokens)

    rls_token = getattr(task_request, "linda_rls_token", None) if task_request else None
    if rls_token is not None:
        try:
            from backend.app.tenant_ctx import reset_current_tenant

            reset_current_tenant(rls_token)
        except Exception:
            logger.debug("RLS token reset failed", exc_info=True)

    # Metrics — task name is the Celery-registered name, not the Python
    # function name (matters for aliased tasks).
    try:
        from backend.app.services.metrics import (
            CELERY_TASK_LATENCY,
            CELERY_TASK_RUNS,
        )

        task_name = getattr(sender, "name", "unknown")
        # When state == SUCCESS we additionally check ``request.retries``;
        # a non-zero retry count means we succeeded only after at least
        # one failure, which is what the operator wants to track separately
        # from "succeeded first try". The ``retry_success`` status lets
        # alerts distinguish "retry logic is working" from "task is flaky".
        request = getattr(sender, "request", None)
        retries = getattr(request, "retries", 0) if request is not None else 0
        if state == "SUCCESS":
            status = "retry_success" if retries > 0 else "success"
        elif state == "RETRY":
            status = "retry"
        else:
            status = "failure"
        CELERY_TASK_RUNS.labels(task_name=task_name, status=status).inc()
        if runtime is not None:
            CELERY_TASK_LATENCY.labels(task_name=task_name).observe(float(runtime))
    except Exception:
        logger.debug("task metrics emission failed", exc_info=True)


# ── Synchronous SQLAlchemy session for Celery tasks ──────────────────────

# Same non-owner role as the API engine when APP_DATABASE_URL is set —
# table owners bypass RLS, so Celery must not run as the owner either.
_sync_db_url = settings.APP_DATABASE_URL or settings.DATABASE_URL
# Ensure we use the synchronous driver (psycopg2) rather than asyncpg.
if _sync_db_url.startswith("postgresql+asyncpg://"):
    _sync_db_url = _sync_db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
elif _sync_db_url.startswith("postgres://"):
    pass  # already sync-compatible

# asyncpg accepts ``?ssl=true|require|...``; psycopg2 doesn't — it uses
# ``?sslmode=...``. The async engine in ``db.py`` strips the asyncpg-style
# query params and passes an SSLContext through ``connect_args``; we have
# to do the equivalent for the sync engine. Without this, every Celery
# task fails before its first DB read with "invalid connection option
# 'ssl'", which is silent because tasks just retry forever.
_sync_connect_args: Dict[str, Any] = {
    # TCP keepalive — the worker holds a connection through the whole
    # pipeline, which now includes TWO long idle gaps where no DB
    # traffic flows: (1) the LLM speaker-segmenter (~30-60s Haiku
    # call) and (2) the main analysis (~30-90s Sonnet call). Neon
    # (and any pgbouncer in between) kills idle TCP connections.
    #
    # ``keepalives_idle=10`` — start probing after only 10 seconds
    # of idle, well below Neon's apparent idle cutoff. The earlier
    # 30s threshold left long-text rows still failing intermittently
    # (~1 in 3 attempts) because the connection died before the
    # first probe fired.
    # ``keepalives_interval=5`` — probe every 5s once started.
    # ``keepalives_count=6`` — give up after 6 missed probes (30s).
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 6,
}
if "ssl=" in _sync_db_url or "sslmode=" in _sync_db_url:
    _sync_db_url = _sync_db_url.split("?")[0]
    _sync_connect_args["sslmode"] = "require"

_sync_engine = create_engine(
    _sync_db_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    # Recycle pooled connections after 4 minutes. Neon closes long-
    # idle connections server-side; pool_pre_ping catches the corpse
    # on checkout. The TCP keepalive settings above handle the
    # mid-task idle case (long LLM call with no DB activity).
    pool_recycle=240,
    connect_args=_sync_connect_args,
)
_SyncSessionFactory = sessionmaker(bind=_sync_engine, expire_on_commit=False)


def _get_sync_session() -> Session:
    """Return a new synchronous SQLAlchemy session."""
    return _SyncSessionFactory()


def _worker_id() -> str:
    """Stable identity for step-ledger claims: host + pid of this
    prefork child. Two concurrent claimers always differ in at least
    one component."""
    import os
    import socket

    return "%s:%s" % (socket.gethostname(), os.getpid())


# ── Per-task event loop reuse ─────────────────────────────────────────────
#
# ``_run_pipeline`` hits ~8 async entrypoints (triage, analysis, webhook
# emit, scorecards, search-index, …). Each previously called ``asyncio.run``
# directly, which spins up a fresh event loop, re-initializes httpx pools,
# and tears everything down. Running them all inside one loop per task lets
# the anthropic + httpx clients reuse their connection pool for the whole
# run. Typical savings per pipeline: 100–300 ms plus reconnect RTT.
#
# We keep the loop scoped to the task invocation (Celery may run tasks on
# a thread pool; a module-level loop would cross threads).


class _TaskEventLoop:
    """Context manager owning a single event loop for a Celery task.

    Usage::

        with _TaskEventLoop() as loop:
            a = loop.run(_some_coroutine())
            b = loop.run(_another_coroutine())
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def __enter__(self) -> "_TaskEventLoop":
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Structural 3b fix: reset the shared async engine's pool ONCE
        # at loop entry, so every ``_loop.run(...)`` site in the pipeline
        # (lifecycle webhook emit, plan synthesis, search indexing, …)
        # gets asyncpg connections bound to THIS loop by construction.
        # ``close=False`` because any connections still pooled here are
        # bound to an already-closed loop (a previous task that crashed
        # before its exit dispose) — awaiting their graceful close from
        # THIS loop is a cross-loop operation that raises "Event loop is
        # closed"; abandoning them to GC terminates the sockets instead.
        # No-op on an empty pool, which is the steady state now that
        # ``__exit__`` disposes on the way out. Import inside the
        # method: backend.app.db must not be a hard import-time dep here.
        try:
            from backend.app.db import engine as _async_engine

            self._loop.run_until_complete(_async_engine.dispose(close=False))
        except Exception:
            logger.warning(
                "async engine dispose at task-loop entry failed (non-fatal)",
                exc_info=True,
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._loop is None:
            return
        try:
            # Close the engine's connections ON the loop that owns them,
            # BEFORE the loop goes away — the counterpart of the entry
            # reset above, and what keeps stale-loop connections from
            # ever accumulating in the shared pool.
            try:
                from backend.app.db import engine as _async_engine

                self._loop.run_until_complete(_async_engine.dispose())
            except Exception:
                logger.warning(
                    "async engine dispose at task-loop exit failed (non-fatal)",
                    exc_info=True,
                )
            # Cancel anything still pending so the loop closes cleanly.
            pending = asyncio.all_tasks(self._loop)
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)
            self._loop = None

    def run(self, coro):
        assert self._loop is not None, "_TaskEventLoop used outside of `with`"
        return self._loop.run_until_complete(coro)


def _run_async(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run a coroutine from a sync Celery task body on a fresh event loop.

    The module-level async engine in ``backend.app.db`` is created once
    per worker process, but each ``asyncio.run`` here is a fresh loop —
    and asyncpg connections are bound to the loop that created them. Two
    rules keep the pool loop-safe:

    * **Dispose at the END of every tick, on the tick's own loop**
      (the ``finally`` below). Connections are closed gracefully while
      the loop that owns them is still running, so the pool is empty by
      the time the loop shuts down. This is the actual fix for the
      "RuntimeError: Event loop is closed" spam from
      ``event_retention_sweep`` et al.: the old dispose-*first* pattern
      closed the PREVIOUS tick's connections from the NEXT tick's loop,
      which is exactly the cross-loop close it was trying to prevent —
      sqlalchemy's pool logged an ERROR per stale connection.
    * **Entry dispose uses ``close=False``** — a belt-and-braces reset
      for the crash case where a previous tick died before its
      ``finally`` ran. It abandons any stale-loop connections to GC
      (which terminates the socket) instead of awaiting a graceful
      close they can no longer perform. No-op when the pool is empty,
      i.e. always, in steady state.

    ``coro_factory`` is the ``async def _runner`` itself (passed
    uncalled), so the coroutine is created inside the new loop.
    """

    async def _wrapped() -> Any:
        from backend.app.db import engine as _async_engine

        await _async_engine.dispose(close=False)
        try:
            return await coro_factory()
        finally:
            await _async_engine.dispose()

    return asyncio.run(_wrapped())


# Keep Contact.sentiment_trend bounded so the JSONB column doesn't grow
# without limit for long-running customer relationships.
CONTACT_SENTIMENT_TREND_CAP = 50


def update_contact_rollup(contact, insights: Dict[str, Any], created_at) -> None:
    """Append the latest sentiment_score to a contact's trend and bump counts.

    Used both in the live pipeline and in the backfill script so behavior
    stays consistent.  Silently skips non-numeric sentiment_score values.
    """
    sentiment_score = insights.get("sentiment_score") if insights else None
    if sentiment_score is not None:
        try:
            trend = list(contact.sentiment_trend or [])
            trend.append(float(sentiment_score))
            contact.sentiment_trend = trend[-CONTACT_SENTIMENT_TREND_CAP:]
        except (TypeError, ValueError):
            logger.warning(
                "Non-numeric sentiment_score on contact %s: %r",
                getattr(contact, "id", "?"), sentiment_score,
            )
    contact.interaction_count = (contact.interaction_count or 0) + 1
    contact.last_seen_at = created_at


# ── Helper: convert Segment dataclass list → list of dicts ───────────────

def _cleanup_staged_audio(
    session: Session,
    interaction: Any,
    staged_path: Optional[str],
    staged_key: Optional[str],
) -> None:
    """Delete the local tempfile and the S3 staging object.

    Called from the voice task after transcription + paralinguistic
    extraction (or on transcription failure). Idempotent — safe to call
    when either argument is None.
    """
    if staged_path:
        try:
            import os as _os

            _os.unlink(staged_path)
        except Exception:
            logger.debug("tempfile unlink failed: %s", staged_path, exc_info=True)
    if staged_key:
        try:
            from backend.app.services import s3_audio

            s3_audio.delete_object(staged_key)
            if getattr(interaction, "audio_s3_key", None) == staged_key:
                interaction.audio_s3_key = None
                session.commit()
        except Exception:
            logger.warning(
                "S3 staging cleanup failed for %s", staged_key, exc_info=True
            )


def _coerce_seconds(value: Any) -> float:
    """Best-effort convert a snippet/segment time to float seconds.

    The analysis LLM occasionally returns timestamps as ``"MM:SS"`` or
    ``"HH:MM:SS"`` strings instead of float seconds. A naive ``float()``
    raises ``ValueError`` and used to fail step 16 of the pipeline (snippet
    insert), wasting an entire successful Sonnet analysis. Accepts:

    * ``None`` / empty / non-string-non-number → 0.0
    * int / float → coerced to float
    * numeric strings (``"123"``, ``"123.4"``) → float
    * ``"MM:SS"`` / ``"M:SS"`` → minutes * 60 + seconds
    * ``"HH:MM:SS"`` → hours * 3600 + minutes * 60 + seconds

    Anything unparseable returns 0.0 — snippet rows are still useful even
    without precise offsets, and the alternative (raising) takes the
    whole pipeline down.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    if ":" in s:
        try:
            parts = [float(p) for p in s.split(":")]
        except ValueError:
            return 0.0
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _segments_to_dicts(segments: list) -> List[Dict[str, Any]]:
    """Convert transcription Segment objects to plain dicts."""
    result: List[Dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            result.append(seg)
        else:
            result.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "confidence": seg.confidence,
            })
    return result


def _compressed_segments_to_text(segments: list) -> str:
    """Join segment texts into a single string for triage."""
    texts: List[str] = []
    for seg in segments:
        if isinstance(seg, dict):
            texts.append(seg.get("text", ""))
        else:
            texts.append(seg.text)
    return " ".join(texts)


def _time_str(seconds: float) -> str:
    """Format seconds as MM:SS string for transcript display."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _segments_for_llm(segments: list) -> List[Dict[str, Any]]:
    """Convert segments to the dict format expected by AI services."""
    result: List[Dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            d = dict(seg)
        else:
            d = {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "confidence": seg.confidence,
            }
        # Add formatted time and speaker keys expected by AI services.
        start = d.get("start", 0)
        d.setdefault("time", _time_str(float(start)))
        d.setdefault("speaker", d.get("speaker_id", "Unknown"))
        result.append(d)
    return result


# ── Lazy service instantiation ───────────────────────────────────────────
# Services are created on first use to avoid import-time side effects.

_pii_service = None
_metrics_service = None
_compressor = None
_triage_service = None
_analysis_service = None
_scorecard_service = None
_snippet_service = None
_search_service = None


def _get_pii_service():
    global _pii_service
    if _pii_service is None:
        from backend.app.services.pii_redaction import PIIRedactionService
        _pii_service = PIIRedactionService()
    return _pii_service


def _get_metrics_service():
    global _metrics_service
    if _metrics_service is None:
        from backend.app.services.call_metrics import CallMetricsService
        _metrics_service = CallMetricsService()
    return _metrics_service


def _get_compressor():
    global _compressor
    if _compressor is None:
        from backend.app.services.transcript_compressor import TranscriptCompressor
        _compressor = TranscriptCompressor()
    return _compressor


def _get_triage_service():
    global _triage_service
    if _triage_service is None:
        from backend.app.services.triage_service import TriageService
        _triage_service = TriageService()
    return _triage_service


def _get_analysis_service():
    global _analysis_service
    if _analysis_service is None:
        from backend.app.services.ai_analysis import AIAnalysisService
        _analysis_service = AIAnalysisService()
    return _analysis_service


def _get_scorecard_service():
    global _scorecard_service
    if _scorecard_service is None:
        from backend.app.services.scorecard_service import ScorecardService
        _scorecard_service = ScorecardService()
    return _scorecard_service


def _get_snippet_service():
    global _snippet_service
    if _snippet_service is None:
        from backend.app.services.snippet_service import SnippetService
        _snippet_service = SnippetService()
    return _snippet_service


def _get_search_service():
    global _search_service
    if _search_service is None:
        from backend.app.services.search_service import SearchService
        _search_service = SearchService()
    return _search_service


# ── Core pipeline logic ─────────────────────────────────────────────────

def _run_pipeline(
    session: Session,
    interaction_id: str,
    segments_dicts: List[Dict[str, Any]],
    tenant: Any,
    interaction: Any,
    audio_path: Optional[str] = None,
) -> None:
    """Shared pipeline logic for both voice and text interactions.

    Runs steps 5–16 of the batch pipeline (everything after transcription).

    ``audio_path``, when supplied, is the local file still on disk from
    the transcription step. Used by the paralinguistic extractor (step
    17a). Unused for text interactions.

    All async subcalls (triage, analysis, scorecard, webhook emit, search
    indexing, delta reports, brief rebuild…) run inside a single event
    loop owned by :class:`_TaskEventLoop` so the anthropic + httpx
    clients can reuse their connection pool across steps. See the class
    docstring for why this matters.
    """
    with _TaskEventLoop() as _loop:
        _run_pipeline_impl(
            session,
            interaction_id,
            segments_dicts,
            tenant,
            interaction,
            _loop,
            audio_path=audio_path,
        )


def _run_pipeline_impl(
    session: Session,
    interaction_id: str,
    segments_dicts: List[Dict[str, Any]],
    tenant: Any,
    interaction: Any,
    _loop: "_TaskEventLoop",
    *,
    audio_path: Optional[str] = None,
) -> None:
    from backend.app.models import (
        Contact,
        Conversation,
        InteractionScore,
        InteractionSnippet,
        ScorecardTemplate,
    )
    from backend.app.services.transcription import Segment

    tenant_id = str(tenant.id)
    agent_id = str(interaction.agent_id) if interaction.agent_id else ""

    # ── Step 5: PII redaction ────────────────────────────────────────
    # Catch BaseException (not just Exception): heavy ML init paths
    # (spaCy/transformers/presidio) can raise SystemExit on internal
    # failure, which would otherwise terminate the prefork child with
    # exitcode=1 and infinite-loop the task via Celery's acks_late
    # redelivery. PII is a soft step — fall through with the original
    # text and pii_redacted=False if init misbehaves.
    pii_redacted = False
    if tenant.pii_redaction_enabled:
        try:
            pii_config = tenant.pii_redaction_config or {}
            segments_dicts = _get_pii_service().redact_segments(
                segments_dicts, config=pii_config
            )
            pii_redacted = True
            logger.info("PII redaction complete for interaction %s", interaction_id)
        except BaseException:  # noqa: BLE001 — guards against SystemExit
            logger.exception(
                "PII redaction failed for interaction %s — continuing with non-redacted text",
                interaction_id,
            )
            pii_redacted = False

    # ── Step 6: Call metrics ─────────────────────────────────────────
    # Convert dicts back to Segment objects for the metrics service.
    segment_objects: List[Segment] = []
    for sd in segments_dicts:
        segment_objects.append(Segment(
            start=float(sd.get("start", 0)),
            end=float(sd.get("end", 0)),
            text=sd.get("text", ""),
            speaker_id=sd.get("speaker_id"),
            confidence=sd.get("confidence"),
        ))

    call_metrics = _get_metrics_service().compute(segment_objects)
    logger.info("Call metrics computed for interaction %s", interaction_id)

    # ── Step 7: Compress transcript for LLM ──────────────────────────
    compressed_segments = _get_compressor().compress(segment_objects)
    compressed_text = _compressed_segments_to_text(compressed_segments)
    compressed_for_llm = _segments_for_llm(compressed_segments)
    logger.info("Transcript compressed for interaction %s", interaction_id)

    # ── Step 8: Triage — complexity scoring ──────────────────────────
    metadata = {
        "channel": interaction.channel,
        "duration": interaction.duration_seconds,
        "caller_info": interaction.caller_phone or "",
    }
    # Release the connection before this LLM call so Neon doesn't
    # kill it during the 5-15s Haiku round-trip. The next DB query
    # (variant lookup) will check out a fresh connection.
    session.commit()
    triage_result: Dict[str, Any] = _loop.run(
        _get_triage_service().score_complexity(compressed_text, metadata)
    )
    complexity_score = float(triage_result.get("complexity_score", 0.5))
    recommended_tier = triage_result.get("recommended_tier", "sonnet")
    logger.info(
        "Triage complete for interaction %s: score=%.2f tier=%s",
        interaction_id, complexity_score, recommended_tier,
    )

    # ── Step 7.5: Paralinguistic extraction (Phase 2) ────────────────
    # Moved here from the old step 17a so the AI-analysis prompt can
    # consume acoustic features. The flag check happens BEFORE any
    # audio decoding so a tenant who has paralinguistics off pays no
    # cost. Decision matrix Q3/Q4: feature gate + silent fallback.
    paralinguistic_block = None
    paralinguistic_raw: Optional[Dict[str, Any]] = None
    paralinguistic_notable: List[Any] = []
    tenant_features = getattr(tenant, "features_enabled", None) or {}
    if audio_path and tenant_features.get("paralinguistic_analysis", True):
        try:
            from backend.app.services.paralinguistics import (
                SpeakerAudioSegment,
                get_paralinguistic_extractor,
            )
            from backend.app.services.paralinguistic_baseline import (
                analyze as _para_baseline_analyze,
            )
            from backend.app.services.paralinguistic_prompt import (
                build_prompt_block as _build_para_prompt_block,
            )

            para_segments = [
                SpeakerAudioSegment(
                    speaker_id=s.speaker_id or "unknown",
                    start=s.start,
                    end=s.end,
                )
                for s in segment_objects
            ]
            para = get_paralinguistic_extractor().extract(
                para_segments, audio_path=audio_path
            )
            if para.available:
                para_dict = para.as_dict()
                # Deterministic arousal annotation (same as legacy step
                # 17a) — cheap, model-free, applies on every available
                # extraction.
                try:
                    from backend.app.services.paralinguistics_emotion import (
                        annotate_arousal,
                    )
                    para_dict = annotate_arousal(para_dict)
                except Exception:
                    logger.debug("arousal annotation failed", exc_info=True)
                # Heavy SpeechBrain emotion pass stays opt-in.
                if tenant_features.get("emotion_classification"):
                    try:
                        from backend.app.services.paralinguistics_emotion import (
                            annotate_emotion,
                        )
                        speakers = list(
                            (para_dict.get("per_speaker") or {}).keys()
                        )
                        segment_paths = [(sid, audio_path) for sid in speakers]
                        para_dict = annotate_emotion(para_dict, segment_paths)
                    except Exception:
                        logger.debug("emotion annotation failed", exc_info=True)

                paralinguistic_raw = para_dict
                # Per-utterance baselines + outlier detection. Short calls
                # with too few utterances per speaker yield an empty
                # notable list (MIN_SPEAKER_UTTERANCES gate inside).
                _, _, paralinguistic_notable = _para_baseline_analyze(
                    audio_path, segment_objects
                )
                paralinguistic_block = _build_para_prompt_block(
                    para_dict, paralinguistic_notable
                )
                if paralinguistic_block.is_empty():
                    paralinguistic_block = None
        except Exception:
            logger.exception(
                "Paralinguistic extraction failed for %s (non-fatal)",
                interaction_id,
            )
            paralinguistic_block = None

    # ── Step 9: AI analysis ──────────────────────────────────────────
    # Prompt-variant routing + personalization blocks.
    from backend.app.services.ai_analysis import ANALYSIS_SYSTEM_PROMPT_TERSE
    from backend.app.services.personalization_service import (
        build_analysis_context_block,
        build_rag_context_block,
        get_parameter_overrides,
    )
    from backend.app.services.prompt_variant_service import (
        select_variant_sync,
        to_uuid as _variant_to_uuid,
    )

    # Terse clipboard-voice prompt is the default and only path. The
    # verbose ANALYSIS_SYSTEM_PROMPT still lives in ai_analysis.py and
    # can be opted into via TenantPromptConfig.variant_template for
    # tenants that explicitly request the long-form analysis at a
    # higher price tier; it is not exposed via features_enabled.
    variant = select_variant_sync(
        session,
        tenant,
        surface="analysis",
        tier=recommended_tier,
        channel=interaction.channel,
        fallback_template=ANALYSIS_SYSTEM_PROMPT_TERSE,
    )
    tenant_block = build_analysis_context_block(session, tenant)
    rag_block = build_rag_context_block(
        session, tenant, triage_result, channel=interaction.channel
    )
    overrides = get_parameter_overrides(session, tenant, surface="analysis")

    # Tenant + per-customer brief assembled by LINDA agents (complements the
    # prompt-variant tenant_block above — kept as structured dicts so the
    # analyzer can render them in its own cacheable system slots).
    tenant_context = dict(getattr(tenant, "tenant_context", None) or {})
    customer_brief: Dict[str, Any] = {}
    if interaction.contact_id:
        from backend.app.models import Customer as _Customer

        _contact = (
            session.query(Contact)
            .filter(Contact.id == interaction.contact_id)
            .first()
        )
        if _contact and _contact.customer_id:
            _customer = session.query(_Customer).filter(_Customer.id == _contact.customer_id).first()
            if _customer:
                customer_brief = dict(_customer.customer_brief or {})

    # Release the connection before the main analysis — this is the
    # longest LLM call in the pipeline (30-90s on Sonnet). Without
    # this, the connection sits idle during analyze() and Neon kills
    # it before the post-analysis save can run.
    session.commit()
    # Resolve the call date so the prompt can anchor 'Thursday' /
    # 'tomorrow' references to real YYYY-MM-DD due_dates. We prefer
    # ``interaction.started_at`` (set by telephony / upload paths) and
    # fall back to ``created_at`` which always exists.
    _call_dt = getattr(interaction, "started_at", None) or interaction.created_at
    _call_date_str = _call_dt.date().isoformat() if _call_dt else None

    # Exactly-once claim for the paid Sonnet call (ledger — see
    # docs/complexity/01 §7). Replaces the old content-sniffing reuse
    # guard (``len(summary) >= 40``): the ledger row is authoritative.
    # REUSED → a prior attempt persisted this exact analysis (same
    # transcript + variant + tier) — never re-pay, even when a
    # later-step failure stamped ``insights['error']``. HELD → another
    # worker is mid-analysis on this interaction (duplicate delivery /
    # double enqueue) — StepHeldError defers the whole task instead of
    # double-paying. ACQUIRED → analyze, then persist-after-pay: the
    # output and the succeeded ledger row land in one commit, so a
    # failure in ANY later step can no longer lose the paid result.
    from backend.app.services.pipeline_ledger import (
        compute_input_hash,
        run_analysis_with_ledger,
    )

    _effective_tier = overrides.get("force_tier") or recommended_tier
    _analysis_input_hash = compute_input_hash(
        compressed_for_llm,
        getattr(variant, "variant_id", None),
        _effective_tier,
        _call_date_str,
    )

    def _paid_analysis() -> Dict[str, Any]:
        return _loop.run(
            _get_analysis_service().analyze(
                compressed_for_llm,
                tier=_effective_tier,
                triage_result=triage_result,
                system_prompt_override=variant.prompt_template,
                tenant_context_block=tenant_block,
                rag_context_block=rag_block,
                max_tokens_override=overrides.get("max_tokens"),
                tenant_context=tenant_context,
                customer_brief=customer_brief,
                paralinguistic_block=paralinguistic_block,
                complexity_score=complexity_score,
                call_date=_call_date_str,
            )
        )

    insights = run_analysis_with_ledger(
        session,
        tenant_id=tenant.id,
        interaction=interaction,
        input_hash=_analysis_input_hash,
        worker_id=_worker_id(),
        analyze_fn=_paid_analysis,
    )
    interaction.prompt_variant_id = _variant_to_uuid(variant.variant_id)
    logger.info(
        "AI analysis complete for interaction %s (variant=%s status=%s)",
        interaction_id, variant.name, variant.status,
    )

    # ── Step 9b: Outcome inference ──────────────────────────────────
    # Squeeze the analysis JSON into a normalised outcome label and,
    # where warranted, emit CustomerOutcomeEvent rows that downstream
    # agents (TenantBriefRefiner, CustomerBriefBuilder) will read.
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from backend.app.models import CustomerOutcomeEvent
    from backend.app.services.kb.outcome_inference import infer_outcome

    inferred = infer_outcome(insights)
    interaction.outcome_type = inferred.outcome_type
    interaction.outcome_value = inferred.outcome_value
    interaction.outcome_confidence = inferred.outcome_confidence
    interaction.outcome_source = "ai_inferred"
    interaction.outcome_notes = inferred.outcome_notes
    interaction.outcome_captured_at = _dt.now(_tz.utc)

    cust_id_for_rebuild: Optional[uuid.UUID] = None
    if interaction.contact_id:
        contact_row = (
            session.query(Contact)
            .filter(Contact.id == interaction.contact_id)
            .first()
        )
        cust_id_for_rebuild = (
            contact_row.customer_id if contact_row is not None else None
        )
        if cust_id_for_rebuild:
            from backend.app.models import Customer as _CustForStage
            from backend.app.services.lifecycle import is_client
            from backend.app.services.webhook_events import (
                CUSTOMER_OUTCOME_EVENT_MAP,
            )

            # Churn-flavored lifecycle events stay internal for prospects
            # (a lead can't churn); the OutcomeEvent row is still written
            # as evidence — only the outbound webhook is suppressed.
            _cust_for_stage = (
                session.query(_CustForStage)
                .filter(_CustForStage.id == cust_id_for_rebuild)
                .first()
            )
            _churn_events_gated = not is_client(_cust_for_stage)

            customer_events_for_webhooks: List[Dict[str, Any]] = []
            for ev in inferred.customer_events:
                session.add(
                    CustomerOutcomeEvent(
                        tenant_id=tenant.id,
                        customer_id=cust_id_for_rebuild,
                        interaction_id=interaction.id,
                        event_type=ev["event_type"],
                        magnitude=ev.get("magnitude"),
                        signal_strength=ev.get("signal_strength"),
                        reason=ev.get("reason"),
                        source=ev.get("source", "ai_inferred"),
                    )
                )
                wh_event = CUSTOMER_OUTCOME_EVENT_MAP.get(ev["event_type"])
                if _churn_events_gated and ev["event_type"] in (
                    "at_risk_flagged",
                    "churned",
                ):
                    wh_event = None
                if wh_event:
                    customer_events_for_webhooks.append(
                        {
                            "webhook_event": wh_event,
                            "customer_id": str(cust_id_for_rebuild),
                            "interaction_id": str(interaction.id),
                            "event_type": ev["event_type"],
                            "reason": ev.get("reason"),
                            "signal_strength": ev.get("signal_strength"),
                            "source": ev.get("source", "ai_inferred"),
                        }
                    )

            # Fan out lifecycle events to subscribed webhooks.
            if customer_events_for_webhooks:
                from backend.app.db import async_session
                from backend.app.services.webhook_dispatcher import emit_event
                from backend.app.tenant_ctx import tenant_context_async

                async def _emit_lifecycle() -> None:
                    # Fresh session — bind the tenant GUC (RLS blocks the
                    # delivery-row INSERT without it) and commit; nothing
                    # else owns this session's transaction.
                    async with async_session() as db:
                        async with tenant_context_async(tenant.id, db):
                            for ev in customer_events_for_webhooks:
                                await emit_event(
                                    db,
                                    tenant.id,
                                    ev["webhook_event"],
                                    {k: v for k, v in ev.items() if k != "webhook_event"},
                                )
                            await db.commit()

                try:
                    _loop.run(_emit_lifecycle())
                except Exception:
                    logger.exception("Customer lifecycle webhook emission failed")

    # Kick a debounced customer-brief rebuild so LINDA has a fresh dossier
    # for the next call with this customer. Best-effort; if Redis/Celery are
    # unavailable in this env we'll catch up on the next interaction.
    if cust_id_for_rebuild is not None:
        try:
            from backend.app.services.kb.context_dispatch import (
                schedule_customer_brief_rebuild,
            )

            _loop.run(schedule_customer_brief_rebuild(tenant.id, cust_id_for_rebuild))
        except Exception:
            logger.debug("schedule_customer_brief_rebuild failed", exc_info=True)

    # ── Step 10: Scorecard scoring ───────────────────────────────────
    # All active templates are scored in a single batched Haiku call — one
    # call per interaction instead of one per template. Transcript + insights
    # are shipped once; the model returns per-template results. If the
    # batched response parses poorly, score_many falls back to per-template
    # calls transparently so a flaky template doesn't take out siblings.
    templates = (
        session.query(ScorecardTemplate)
        .filter(ScorecardTemplate.tenant_id == tenant.id)
        .all()
    )
    applicable_templates: List[Dict[str, Any]] = []
    for template in templates:
        channel_filter = template.channel_filter
        if channel_filter and interaction.channel not in channel_filter:
            continue
        applicable_templates.append(
            {
                "id": str(template.id),
                "name": template.name,
                "criteria": template.criteria,
            }
        )

    transcript_for_scoring = _segments_for_llm(segments_dicts)
    scorecard_results: List[Dict[str, Any]] = []
    if applicable_templates:
        # Exactly-once claim for the batched Haiku scoring call. Score
        # rows are inserted HERE (not step 15) so persist-after-pay
        # holds: rows + succeeded ledger flip land in one commit.
        # REUSED → the rows are already in the DB from the winning run;
        # HELD → another worker is mid-scoring — scores are non-blocking,
        # so skip rather than defer the whole task (the holder's rows
        # will land). Idempotent re-derivation: replace this
        # interaction's machine-written scores instead of duplicating.
        from backend.app.services.pipeline_ledger import (
            STEP_SCORECARDS,
            StepClaim,
            claim_step,
            complete_step,
            fail_step,
        )

        _sc_claim = claim_step(
            session,
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            step_key=STEP_SCORECARDS,
            input_hash=compute_input_hash(
                _analysis_input_hash,
                ",".join(sorted(t["id"] for t in applicable_templates)),
            ),
            worker_id=_worker_id(),
        )
        if _sc_claim.outcome == StepClaim.ACQUIRED:
            try:
                scorecard_results = _loop.run(
                    _get_scorecard_service().score_many(
                        transcript_for_scoring, applicable_templates, insights
                    )
                )
            except Exception as _sc_exc:
                fail_step(
                    session, _sc_claim.run_id,
                    error="%s: %s" % (type(_sc_exc).__name__, _sc_exc),
                )
                raise
            session.query(InteractionScore).filter(
                InteractionScore.interaction_id == interaction.id
            ).delete(synchronize_session=False)
            for sc in scorecard_results:
                session.add(
                    InteractionScore(
                        interaction_id=interaction.id,
                        template_id=uuid.UUID(sc["template_id"]),
                        tenant_id=tenant.id,
                        total_score=sc.get("total_score"),
                        criterion_scores=sc.get("criterion_scores", []),
                    )
                )
            complete_step(
                session, _sc_claim.run_id, output_digest="interaction_scores"
            )
            logger.info(
                "Scored %d scorecard templates for interaction %s",
                len(scorecard_results), interaction_id,
            )
        elif _sc_claim.outcome == StepClaim.REUSED:
            logger.info(
                "Reusing persisted scorecard scores for interaction %s (ledger)",
                interaction_id,
            )
        else:  # HELD
            logger.info(
                "Scorecard scoring for interaction %s held by another worker — "
                "skipping (non-blocking)",
                interaction_id,
            )

    # ── Step 11: Snippet identification ──────────────────────────────
    snippet_dicts = _get_snippet_service().identify_notable_segments(
        insights, agent_id, tenant_id
    )
    logger.info(
        "Identified %d snippets for interaction %s",
        len(snippet_dicts), interaction_id,
    )

    # ── Step 12: Search indexing ─────────────────────────────────────
    search_data = {
        "transcript_segments": segments_dicts,
        "summary": insights.get("summary", ""),
        "topics": [
            t.get("name", t) if isinstance(t, dict) else t
            for t in insights.get("topics", [])
        ],
        "agent_id": agent_id,
        "channel": interaction.channel,
        "sentiment_score": insights.get("sentiment_score"),
        "created_at": (
            interaction.created_at.isoformat()
            if interaction.created_at else None
        ),
    }
    try:
        _loop.run(
            _get_search_service().index_interaction(
                interaction_id, tenant_id, search_data
            )
        )
        logger.info("Search indexed interaction %s", interaction_id)
    except Exception:
        logger.exception(
            "Search indexing failed for interaction %s (non-fatal)",
            interaction_id,
        )

    # ── Step 12b: Entity resolution ──────────────────────────────────
    # Plug Customer + Contact + role inference into the row before we
    # flip status to "analyzed". The resolver is best-effort: any
    # exception becomes a logged warning, the interaction still lands
    # as analyzed, and the orphan can be reprocessed later. Runs the
    # contact-rollup step (13b) against the freshly-resolved contact.
    try:
        from backend.app.services.entity_resolution import resolve_interaction_entities
        from backend.app.services.pipeline_ledger import (
            STEP_ENTITY_RESOLUTION,
            StepClaim as _ErStepClaim,
            claim_step as _er_claim_step,
            complete_step as _er_complete_step,
            fail_step as _er_fail_step,
        )

        # Ledger-wrapped so a swallowed failure is *discoverable*: a
        # 'failed' row here is exactly what the reconcile_orphan_
        # interactions sweeper scans for, and a 'succeeded' row lets it
        # distinguish "resolution crashed" from "there was genuinely
        # nobody to resolve" — the ambiguity called out in
        # docs/complexity/01 §3c.
        _er_claim = _er_claim_step(
            session,
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            step_key=STEP_ENTITY_RESOLUTION,
            input_hash=_analysis_input_hash,
            worker_id=_worker_id(),
        )
        if _er_claim.outcome == _ErStepClaim.ACQUIRED:
            try:
                resolution = _loop.run(
                    resolve_interaction_entities(
                        session=session,
                        interaction=interaction,
                        tenant=tenant,
                        insights=insights,
                        compressed_transcript=compressed_text,
                    )
                )
            except Exception as _er_exc:
                _er_fail_step(
                    session, _er_claim.run_id,
                    error="%s: %s" % (type(_er_exc).__name__, _er_exc),
                )
                raise
            if resolution.customer_action != "none":
                logger.info(
                    "Entity resolution for interaction %s: %s (score=%.2f, customer_id=%s)",
                    interaction_id,
                    resolution.customer_action,
                    resolution.customer_score,
                    resolution.customer_id,
                )
            if resolution.suggestions:
                # Stash suggestions on the interaction so the SPA can
                # render the inline match-candidate card. The
                # notification-tray surface plugs into the same data via
                # a dedicated endpoint in the Suggestions phase.
                existing_meta = (
                    getattr(interaction, "insights", None) or {}
                )
                existing_meta = dict(existing_meta)
                existing_meta["entity_resolution_suggestions"] = resolution.suggestions
                interaction.insights = existing_meta
            _er_complete_step(
                session, _er_claim.run_id,
                output_digest="customer_id=%s" % (interaction.customer_id,),
            )
        else:
            logger.info(
                "Entity resolution for interaction %s: ledger says %s — skipping",
                interaction_id, _er_claim.outcome,
            )
    except Exception:
        logger.exception(
            "Entity resolution failed for interaction %s — continuing as orphan",
            interaction_id,
        )

    # ── Step 12b.5: Support-case attach (PR B / dom_002) ─────────────
    # After entity resolution but before warnings + commitments so the
    # case FK is on the row when the warnings engine reads it (a future
    # warning kind may scope to "open IT-support case for this customer").
    # Best-effort: any failure becomes a logged warning and the
    # interaction continues through the pipeline as a normal IT-support
    # interaction without a case attachment.
    try:
        if interaction.domain == "it_support":
            from backend.app.services.support_case_service import (
                attach_or_create_case,
            )

            attach_or_create_case(session, interaction)
    except Exception:
        logger.exception(
            "Support-case attach failed for interaction %s (non-fatal)",
            interaction_id,
        )

    # ── Step 12c: Warnings + commitments ─────────────────────────────
    # Run after entity_resolution so we have ``interaction.customer_id``
    # populated (the warnings engine needs it to attach rows). Same
    # best-effort posture as 12b: any flake becomes a logged warning,
    # the interaction still lands as analyzed.
    try:
        from backend.app.services.warnings_commitments import detect_and_persist

        wc_outcome = _loop.run(
            detect_and_persist(
                session=session,
                interaction=interaction,
                tenant=tenant,
                insights=insights,
                compressed_transcript=compressed_text,
            )
        )
        if (
            wc_outcome.warnings_upserted
            or wc_outcome.commitments_created
            or wc_outcome.commitments_marked_done
        ):
            logger.info(
                "Phase 4 detect for interaction %s: warnings=%d (re-raised %d) "
                "commitments=%d done=%d",
                interaction_id,
                wc_outcome.warnings_upserted,
                wc_outcome.warnings_re_raised,
                wc_outcome.commitments_created,
                wc_outcome.commitments_marked_done,
            )
    except Exception:
        logger.exception(
            "Warnings/commitments detection failed for interaction %s — continuing",
            interaction_id,
        )

    # ── Step 12d: Customer relationship memory (PR dom_006) ──────────
    # Upsert customer concerns + their-side commitments from the AI
    # analyzer's ``concerns_raised`` and ``customer_commitments`` keys.
    # Best-effort: failures log and the interaction still lands. Only
    # runs when the interaction has a customer linkage; the extractor
    # short-circuits otherwise.
    try:
        if interaction.customer_id is not None:
            from backend.app.services.customer_memory import (
                update_from_interaction,
            )

            mem_counts = update_from_interaction(session, interaction, insights)
            if mem_counts.get("concerns") or mem_counts.get("commitments"):
                logger.info(
                    "Customer memory updated for interaction %s: "
                    "concerns=%d commitments=%d",
                    interaction_id,
                    mem_counts.get("concerns", 0),
                    mem_counts.get("commitments", 0),
                )
    except Exception:
        logger.exception(
            "Customer relationship memory update failed for interaction %s",
            interaction_id,
        )

    # ── Step 13: Update interaction row ──────────────────────────────
    interaction.status = "analyzed"
    interaction.transcript = segments_dicts
    # Phase 1: the LLM emits coarse buckets only. Code maps them to the
    # numeric fields the rest of the platform consumes (analytics,
    # contact health rollups, dashboards, training labels). Done BEFORE
    # the dict copy so downstream reads off the same ``insights`` ref
    # below see the derived numerics.
    from backend.app.services.score_mapping import derive_numeric_scores
    derive_numeric_scores(insights)
    # Phase 3 foundation: derive deterministic rubric scores from the
    # LLM's evidence counts (objections / commitments / discovery
    # questions, etc.) and dual-log them alongside the bucket-mapped
    # values. Lets us validate calibration before flipping to
    # rubric-as-source-of-truth in Phase 4.
    from backend.app.services.evidence_scoring import attach_rubric
    attach_rubric(insights)
    # Phase 5 rapport gauge — Linguistic Style Matching (LSM) is a
    # transcript-only signal of how closely the rep and customer mirror
    # each other's function-word usage. Phase 2 adds the
    # vocal-accommodation half from per-speaker prosody when audio
    # was available. The composite ``rapport.overall`` blends both
    # halves automatically.
    from backend.app.services.rapport_lsm import (
        attach_rapport,
        attach_vocal_accommodation,
    )
    attach_rapport(insights, segments_dicts)
    attach_vocal_accommodation(insights, paralinguistic_raw)

    # Phase 4 binary classifiers. When an active model exists for the
    # tenant + target, we write a calibrated probability alongside the
    # bucket-mapped numeric (dual-logging). Cold-start tenants get
    # ``status="insufficient_data"`` and the rubric / bucket numerics
    # remain the source of truth — no override. Best-effort: a missing
    # ScorerVersion table or a malformed model row never blocks the
    # pipeline.
    try:
        from backend.app.services.phase4_classifier import predict as _phase4_predict

        classifier_block: Dict[str, Dict[str, Any]] = {}
        for _target in ("churn", "upsell"):
            pred = _phase4_predict(session, tenant.id, _target, insights)
            classifier_block[_target] = {
                "status": pred.status,
                "probability": pred.probability,
                "model_version": pred.model_version,
                "label_horizon_days": pred.label_horizon_days,
                "caveat": pred.caveat,
            }
        insights["classifier_predictions"] = classifier_block
    except Exception:
        logger.exception(
            "Phase 4 classifier inference failed for %s (non-fatal)",
            interaction_id,
        )

    # ``interaction.insights`` may already have been mutated above
    # (entity_resolution stashes suggestions there); merge rather than
    # overwrite when we replace the dict here.
    merged_insights = dict(insights)
    existing_extras = getattr(interaction, "insights", None) or {}
    if isinstance(existing_extras, dict):
        for k in (
            "entity_resolution_suggestions",
            "entity_resolution_debug",
            "warnings_commitments_debug",
        ):
            if k in existing_extras:
                merged_insights[k] = existing_extras[k]
    interaction.insights = merged_insights
    interaction.call_metrics = call_metrics
    interaction.complexity_score = complexity_score
    interaction.analysis_tier = recommended_tier
    interaction.pii_redacted = pii_redacted

    # ── Step 13b: Update contact trend rollup ────────────────────────
    if interaction.contact_id is not None:
        contact = session.query(Contact).filter(Contact.id == interaction.contact_id).first()
        if contact is not None:
            update_contact_rollup(contact, insights, interaction.created_at)

    # ── Step 13c: Update conversation rollup (email threading) ───────
    if interaction.conversation_id is not None:
        conv = (
            session.query(Conversation)
            .filter(Conversation.id == interaction.conversation_id)
            .first()
        )
        if conv is not None:
            # Keep a small rolling summary + sentiment series at the conv level
            # so the reply generator and UI don't have to aggregate on read.
            conv_insights = dict(conv.insights or {})
            series = list(conv_insights.get("sentiment_series") or [])
            sscore = insights.get("sentiment_score")
            if sscore is not None:
                try:
                    series.append(float(sscore))
                    conv_insights["sentiment_series"] = series[-50:]
                except (TypeError, ValueError):
                    pass
            conv_insights["latest_summary"] = insights.get("summary", "")
            conv_insights["latest_churn_risk"] = insights.get("churn_risk")
            conv_insights["latest_upsell_score"] = insights.get("upsell_score")
            conv.insights = conv_insights
            # Direction drives status: inbound customer → waiting on us;
            # outbound agent → waiting on customer.
            if interaction.direction == "inbound":
                conv.status = "waiting_agent"
            elif interaction.direction == "outbound":
                conv.status = "waiting_customer"

    # ── Step 14: Action items — retired 2026-07 (4b cutover) ─────────
    # The pipeline no longer writes legacy ActionItem rows; the Action
    # Plan DAG below is the canonical output of analysis. The raw LLM
    # suggestions stay available in insights['action_items'] (plan
    # synthesis seeds, email drafts, outcome inference all read the
    # JSON, not the table). ActionItem persists only for manually
    # created tasks (POST /action-items, Linda chat proposals, manager
    # triage). (This also retires the delete-and-reinsert idempotency
    # dance the exactly-once work added for machine-written rows —
    # there are none to re-derive.)
    #
    # Category-taxonomy discovery used to piggyback on the row inserts;
    # keep feeding LLM-emitted categories so alias mapping and promotion
    # candidates continue to accumulate.
    for ai_item in insights.get("action_items", []):
        try:
            from backend.app.services.category_taxonomy import record_occurrence

            record_occurrence(session, tenant.id, ai_item.get("category") or "")
        except Exception:
            logger.debug(
                "category taxonomy record failed (non-fatal)", exc_info=True
            )

    # ── Step 14a: Synthesize Action Plan (the canonical action model) ─
    # Per the locked failure-mode decision, plan synthesis never blocks
    # the pipeline — on any error we log and continue; the interaction
    # still carries insights['action_items'] for the analysis surfaces,
    # and the next clean run recreates the plan.
    #
    # The synthesizer wants an AsyncSession; the pipeline holds a sync
    # one. We open a short-lived async session over the same database
    # and commit its changes independently — they're orthogonal to the
    # sync session's writes (different rows).
    # Diagnostic stamp on interaction.insights so we can read the
    # synthesis trace via the standard /interactions/{id} endpoint
    # without needing Fly log access. Each phase writes a key; the
    # final state of these keys after a redrive tells us exactly
    # where synthesis dropped (or whether it never started).
    _plan_diag: Dict[str, Any] = {"entered_block": True}

    # synth-redeploy-marker-2026-06-01a-diag-cap-bump-investigating-runtime-error
    try:
        from backend.app.db import async_session as _async_session_factory
        from backend.app.services.action_plan.synthesizer import (
            ActionPlanSynthesizer,
            SynthesisFailedError,
            SynthesisInputs,
        )

        # COMMIT (not flush) sync-session writes before opening a
        # separate async connection. The async session checks out a
        # different asyncpg connection from the pool, and under
        # PostgreSQL's default READ COMMITTED isolation it cannot see
        # uncommitted writes from the sync connection's transaction.
        # If we only flush, ``async_db.get(Interaction, id)`` below
        # returns None (the row was created/updated in the still-open
        # sync transaction) and synthesis silently exits via the
        # early-return guard, leaving no ActionPlan and no log line.
        session.commit()
        _plan_diag["sync_commit_ok"] = True

        # Exactly-once claim for the synthesis LLM call. Keyed on the
        # analysis input-hash: same analysis → same plan; a re-analysis
        # (changed transcript/variant/tier) legitimately re-synthesizes.
        # REUSED/HELD both skip — synthesis is non-blocking by the
        # locked failure-mode decision, so we never defer the task on it.
        from backend.app.services.pipeline_ledger import (
            STEP_PLAN_SYNTHESIS,
            StepClaim as _StepClaim,
            claim_step as _pl_claim_step,
            complete_step as _pl_complete_step,
            fail_step as _pl_fail_step,
        )

        _plan_claim = _pl_claim_step(
            session,
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            step_key=STEP_PLAN_SYNTHESIS,
            input_hash=_analysis_input_hash,
            worker_id=_worker_id(),
        )
        _plan_diag["ledger_outcome"] = _plan_claim.outcome

        async def _run_plan_synthesis() -> None:
            # No dispose-first needed here anymore: ``_TaskEventLoop``
            # disposes the shared async engine at loop entry, so any
            # connection checked out inside this loop is already bound
            # to it (see the __enter__ docblock).
            async with _async_session_factory() as async_db:
                synthesizer = ActionPlanSynthesizer()
                # Re-fetch the tenant + interaction from the async
                # session so the ORM identity map is clean.
                from backend.app.models import Interaction as _IxModel
                from backend.app.models import Tenant as _TenantModel

                async_tenant = await async_db.get(_TenantModel, tenant.id)
                async_ix = await async_db.get(_IxModel, interaction.id)
                _plan_diag["async_session_opened"] = True
                _plan_diag["async_tenant_loaded"] = async_tenant is not None
                _plan_diag["async_ix_loaded"] = async_ix is not None
                if async_tenant is None or async_ix is None:
                    # Loud log so this can't go unnoticed again. If we
                    # ever see "tenant_missing=True" or "ix_missing=True"
                    # in Fly logs, the commit-vs-flush guard above
                    # regressed.
                    logger.warning(
                        "Action plan synthesis early-return for interaction %s: "
                        "tenant_missing=%s ix_missing=%s (sync session must commit "
                        "before opening async session)",
                        interaction.id,
                        async_tenant is None,
                        async_ix is None,
                    )
                    _plan_diag["early_return_reason"] = "rows_not_visible"
                    return
                # compressed_for_llm is a LIST of segment dicts produced
                # by _segments_for_llm(), not a string. Flatten to plain
                # text so the synthesizer's retrieval query (and Call A's
                # transcript_text input) gets the right shape. Fall back
                # to interaction.raw_text when compressed_for_llm isn't
                # bound (paths that bypass segmentation).
                try:
                    _segs = compressed_for_llm
                except NameError:
                    _segs = None
                if isinstance(_segs, list):
                    _lines = []
                    for _s in _segs:
                        if isinstance(_s, dict):
                            t = _s.get("text") or ""
                            spk = _s.get("speaker") or _s.get("speaker_id")
                            if spk:
                                _lines.append(f"{spk}: {t}")
                            else:
                                _lines.append(str(t))
                    txt = "\n".join(_lines)
                elif isinstance(_segs, str):
                    txt = _segs
                else:
                    txt = (async_ix.raw_text or "")
                logger.info(
                    "Action plan synthesis entering synthesize() for interaction %s",
                    interaction.id,
                )
                _plan_diag["synthesize_started"] = True
                _result = await synthesizer.synthesize(
                    async_db,
                    SynthesisInputs(
                        tenant=async_tenant,
                        interaction=async_ix,
                        transcript_text=txt,
                        triage=(triage_result if isinstance(triage_result, dict) else {}),
                        customer_id=async_ix.customer_id,
                        acting_user_id=async_ix.agent_id,
                    ),
                )
                _plan_diag["synthesize_returned"] = True
                _plan_diag["new_plan_id"] = str(_result.plan_id)
                _plan_diag["new_step_count"] = len(_result.steps or [])
                await async_db.commit()
                _plan_diag["async_commit_ok"] = True
                logger.info(
                    "Action plan synthesis SUCCESS for interaction %s: plan_id=%s steps=%d domain=%s",
                    interaction.id,
                    _result.plan_id,
                    len(_result.steps or []),
                    _result.chosen_domain,
                )

        if _plan_claim.outcome == _StepClaim.ACQUIRED:
            try:
                _loop.run(_run_plan_synthesis())
            except Exception as _plan_run_exc:
                _pl_fail_step(
                    session, _plan_claim.run_id,
                    error="%s: %s" % (type(_plan_run_exc).__name__, _plan_run_exc),
                )
                raise
            _pl_complete_step(
                session, _plan_claim.run_id, output_digest="action_plans"
            )
        else:
            logger.info(
                "Action plan synthesis for interaction %s: ledger says %s — skipping",
                interaction.id, _plan_claim.outcome,
            )
    except SynthesisFailedError as _plan_exc:  # type: ignore[name-defined]
        logger.warning(
            "Action plan synthesis failed for interaction %s (non-fatal): %s",
            interaction.id, _plan_exc,
        )
        _plan_diag["caught_kind"] = "SynthesisFailedError"
        _plan_diag["caught_error"] = str(_plan_exc)[:200]
    except Exception as _plan_exc_other:  # noqa: BLE001
        logger.exception(
            "Action plan synthesis raised unexpectedly for interaction %s "
            "(non-fatal); pipeline continues",
            interaction.id,
        )
        _plan_diag["caught_kind"] = type(_plan_exc_other).__name__
        _plan_diag["caught_error"] = str(_plan_exc_other)[:1000]
        import traceback as _tb
        # 8000 chars instead of 2000 — the SQLAlchemy stack alone hits
        # 1800 chars before the actual root error line, leaving the
        # meaningful exception text truncated. 8000 captures the full
        # chain including any chained "caused by" exceptions.
        _plan_diag["caught_traceback"] = _tb.format_exc()[:8000]
    # The synthesis block above populates ``_plan_diag`` but does NOT
    # persist it here. Previous approaches (a separate session.commit
    # mid-pipeline, then a fresh isolated session with JSONB merge)
    # both failed silently in production for reasons we couldn't see
    # without Fly log access. The current strategy is the simplest
    # one: just assign the diag to ``interaction.insights`` via the
    # main session's ORM and let the pipeline's final ``session.commit``
    # at the bottom of this function persist it atomically with every
    # other write. Since the main commit demonstrably succeeds (status
    # flips to ``analyzed``, all other insights fields land), the diag
    # rides along with that same successful commit.

    # ── Step 15: Insert interaction scores ───────────────────────────
    # Moved into step 10: score rows insert in the same commit that
    # flips the scorecards ledger row to succeeded (persist-after-pay).

    # ── Step 16: Insert interaction snippets ─────────────────────────
    # Idempotent re-derivation: snippets are recomputed from insights on
    # every run, so a retry replaces the machine-written rows instead of
    # duplicating them. Library-curated rows (in_library=True) survive;
    # a recomputed snippet that collides with a surviving library row
    # (same span + type) is skipped rather than duplicated.
    _library_snippets = (
        session.query(InteractionSnippet)
        .filter(
            InteractionSnippet.interaction_id == interaction.id,
            InteractionSnippet.in_library == True,  # noqa: E712
        )
        .all()
    )
    _library_keys = {
        (s.start_time, s.end_time, s.snippet_type) for s in _library_snippets
    }
    session.query(InteractionSnippet).filter(
        InteractionSnippet.interaction_id == interaction.id,
        InteractionSnippet.in_library == False,  # noqa: E712
    ).delete(synchronize_session=False)
    for sn in snippet_dicts:
        _sn_key = (
            _coerce_seconds(sn.get("start_time")),
            _coerce_seconds(sn.get("end_time")),
            sn.get("snippet_type"),
        )
        if _sn_key in _library_keys:
            continue
        snippet_row = InteractionSnippet(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
            start_time=_coerce_seconds(sn.get("start_time")),
            end_time=_coerce_seconds(sn.get("end_time")),
            snippet_type=sn.get("snippet_type"),
            quality=sn.get("quality"),
            title=sn.get("title"),
            description=sn.get("description"),
            transcript_excerpt=sn.get("transcript_excerpt", []),
            tags=sn.get("tags", []),
            in_library=sn.get("in_library", False),
            library_category=sn.get("library_category"),
        )
        session.add(snippet_row)

    # ── Step 17: Write InteractionFeatures (canonical feature store) ─
    from backend.app.models import InteractionFeatures
    from backend.app.services.feature_extractors import FeatureExtractor

    deterministic_features = FeatureExtractor().extract(segment_objects)

    # ── Step 17a (legacy): persist paralinguistic block ──────────────
    # Phase 2 moved the extraction itself to step 7.5 so the AI prompt
    # could consume it. This step now only persists the already-
    # computed ``paralinguistic_raw`` (plus the notable utterance list)
    # onto ``deterministic_features`` so the existing JSONB storage and
    # downstream consumers (rapport gauge, dashboards) keep working.
    if paralinguistic_raw:
        block_for_storage = dict(paralinguistic_raw)
        if paralinguistic_notable:
            block_for_storage["notable_utterances"] = [
                {
                    "segment_idx": tag.segment_idx,
                    "speaker_id": tag.speaker_id,
                    "start": tag.start,
                    "features": [
                        {"name": name, "z": z} for name, z in tag.features
                    ],
                }
                for tag in paralinguistic_notable
            ]
        deterministic_features["paralinguistic"] = block_for_storage

    features_row = (
        session.query(InteractionFeatures)
        .filter(InteractionFeatures.interaction_id == interaction.id)
        .first()
    )
    if features_row is None:
        features_row = InteractionFeatures(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
        )
        session.add(features_row)

    # ── Step 17c: Fire outbound webhooks ───────────────────────────────
    # Churn fields are gated on account lifecycle: a prospect can't
    # churn, so webhook consumers (e.g. the Flex console) must never
    # receive churn for one — same rule the REST serializers apply.
    from backend.app.models import Customer as _CustomerForWebhooks
    from backend.app.services.lifecycle import lifecycle_stage as _acct_lifecycle

    _webhook_customer = None
    if interaction.customer_id is not None:
        _webhook_customer = (
            session.query(_CustomerForWebhooks)
            .filter(_CustomerForWebhooks.id == interaction.customer_id)
            .first()
        )
    _webhook_stage = _acct_lifecycle(_webhook_customer)

    _emit_webhooks_for_interaction(
        tenant_id=tenant.id,
        interaction_id=uuid.UUID(interaction_id),
        insights=insights,
        outcome_type=interaction.outcome_type,
        outcome_confidence=interaction.outcome_confidence,
        account_lifecycle_stage=_webhook_stage,
    )

    # ── Step 17d: CRM write-back (opt-in) ──────────────────────────────
    # Dispatch as a separate Celery task so a slow CRM doesn't stretch
    # the critical path. Idempotent against the same interaction, so a
    # Celery retry can replay safely.
    tf = getattr(tenant, "features_enabled", None) or {}
    if tf.get("crm_writeback_notes") or tf.get("crm_writeback_activities"):
        try:
            crm_writeback.delay(interaction_id)
        except Exception:
            logger.debug(
                "CRM write-back dispatch failed for %s", interaction_id,
                exc_info=True,
            )

    # ── Step 17b: Weak-supervision labels (orthogonal to the LLM guess) ─
    # Cheap regex LFs produce {cancel_intent, commitment, objection_resolved}
    # labels stored alongside the LLM structured blob.  The orchestrator
    # and calibrator treat these as an independent signal, improving
    # calibration quality without replacing any existing field.
    from backend.app.services.weak_supervision import label_interaction

    enriched_insights: Dict[str, Any] = dict(insights or {})
    try:
        ws_labels = label_interaction(
            transcript=compressed_text,
            turns=segments_dicts,
            llm_churn_signal=enriched_insights.get("churn_risk_signal"),
        )
        enriched_insights["weak_supervision"] = {
            key: {
                "label": agg.label,
                "probability": agg.probability,
                "support": agg.support,
                "lf_votes": agg.lf_votes,
            }
            for key, agg in ws_labels.items()
        }
    except Exception:  # noqa: BLE001 — WS must never fail the pipeline
        logger.exception(
            "Weak-supervision labeling failed for interaction %s (non-fatal)",
            interaction_id,
        )

    features_row.deterministic = deterministic_features
    features_row.llm_structured = enriched_insights
    features_row.scorer_versions = {
        "analysis_tier": recommended_tier,
        "complexity_score": complexity_score,
    }
    # Phase 0 telemetry: record which prompt + model produced this analysis,
    # so outcome data can be cohorted by version when training the Phase 4
    # classifier. Imported lazily to avoid pulling LLM-client deps in the
    # test environment.
    try:
        from backend.app.services.ai_analysis import (
            ANALYSIS_PROMPT_VERSION,
            MODELS as _ANALYSIS_MODELS,
        )
        from backend.app.services.triage_service import TRIAGE_PROMPT_VERSION

        features_row.analysis_prompt_version = ANALYSIS_PROMPT_VERSION
        features_row.triage_prompt_version = TRIAGE_PROMPT_VERSION
        features_row.model_used = _ANALYSIS_MODELS.get(recommended_tier)
    except Exception:
        logger.debug(
            "telemetry version capture failed for %s (non-fatal)",
            interaction_id,
            exc_info=True,
        )

    # ── Step 18: Enqueue delta report → orchestrator ─────────────────
    try:
        _enqueue_delta_report(
            session=session,
            tenant=tenant,
            interaction=interaction,
            features={
                "deterministic": deterministic_features,
                "llm_structured": enriched_insights,
            },
            _loop=_loop,
        )
    except Exception:
        logger.exception(
            "Delta report generation failed for %s (non-fatal)",
            interaction_id,
        )

    # ── Step 19: Fire outbound webhooks ──────────────────────────────
    from backend.app.services.webhook_dispatcher import dispatch_sync

    analyzed_payload = {
        "event": "interaction.analyzed",
        "tenant_id": tenant_id,
        "interaction_id": interaction_id,
        "channel": interaction.channel,
        "direction": interaction.direction,
        "classification": getattr(interaction, "classification", None),
        "contact_id": str(interaction.contact_id) if interaction.contact_id else None,
        "conversation_id": (
            str(interaction.conversation_id) if interaction.conversation_id else None
        ),
        "summary": insights.get("summary"),
        "sentiment_score": insights.get("sentiment_score"),
        # Clients only — a prospect has no relationship to churn out of.
        "churn_risk": (
            insights.get("churn_risk") if _webhook_stage == "client" else None
        ),
        "upsell_score": insights.get("upsell_score"),
        "lifecycle_stage": _webhook_stage,
        "action_item_count": len(insights.get("action_items", [])),
    }
    try:
        dispatch_sync(session, tenant.id, "interaction.analyzed", analyzed_payload)
    except Exception:
        logger.exception("Webhook dispatch raised (non-fatal)")

    # Conversation-level fan-out — only when this message actually has a thread.
    if interaction.conversation_id is not None:
        conv_row = (
            session.query(Conversation)
            .filter(Conversation.id == interaction.conversation_id)
            .first()
        )
        if conv_row is not None:
            try:
                dispatch_sync(
                    session,
                    tenant.id,
                    "conversation.updated",
                    {
                        "event": "conversation.updated",
                        "tenant_id": tenant_id,
                        "conversation_id": str(conv_row.id),
                        "channel": conv_row.channel,
                        "classification": conv_row.classification,
                        "status": conv_row.status,
                        "message_count": conv_row.message_count,
                        "latest_summary": (conv_row.insights or {}).get("latest_summary"),
                    },
                )
            except Exception:
                logger.exception("Conversation webhook dispatch raised (non-fatal)")

    # ── Stamp _plan_synthesis_diag ───────────────────────────────────
    # Persist the synthesis trace right before the final commit so it
    # rides along with the same successful transaction that lands
    # status='analyzed' and every other insights field. Always-runs;
    # no try/except so a bug here is loud, not silent.
    _diag_insights = dict(interaction.insights or {})
    _diag_insights["_plan_synthesis_diag"] = _plan_diag
    interaction.insights = _diag_insights

    session.commit()
    logger.info("Pipeline complete for interaction %s", interaction_id)

    # ── Step 18: Schedule LLM-judge evaluation (Layer 2) ─────────────
    # 15-min delay so the interaction settles in DB and (for replies) any
    # follow-on edit-distance event has been written.
    try:
        evaluate_analysis.apply_async(args=[interaction_id], countdown=900)
        if interaction.channel == "email":
            evaluate_classification.apply_async(args=[interaction_id], countdown=900)
            if interaction.direction == "outbound":
                evaluate_reply.apply_async(args=[interaction_id], countdown=900)
    except Exception:
        logger.exception("Failed to enqueue evaluator tasks (non-fatal)")


def _enqueue_delta_report(
    *,
    session: Session,
    tenant: Any,
    interaction: Any,
    features: Dict[str, Any],
    _loop: "Optional[_TaskEventLoop]" = None,
) -> None:
    """Build and persist a ``DeltaReport`` scoped to every touched entity.

    The LLM call here is a small Sonnet invocation producing ≤1k tokens of
    structured JSON.  Failure is logged but never fatal — the orchestrator
    can still run without a delta, it just has less evidence.
    """
    from backend.app.services.orchestrator import (
        DeltaReportWriter,
        EntityScope,
        ENTITY_AGENT,
        ENTITY_BUSINESS,
        ENTITY_CLIENT,
        ENTITY_MANAGER,
        get_orchestrator,
    )
    from backend.app.models import User

    scopes: List[EntityScope] = [
        EntityScope(entity_type=ENTITY_BUSINESS, entity_id=str(tenant.id)),
    ]
    if interaction.contact_id:
        scopes.append(EntityScope(entity_type=ENTITY_CLIENT, entity_id=str(interaction.contact_id)))
    if interaction.agent_id:
        scopes.append(EntityScope(entity_type=ENTITY_AGENT, entity_id=str(interaction.agent_id)))
        agent = session.query(User).filter(User.id == interaction.agent_id).first()
        manager_id = getattr(agent, "manager_id", None) if agent else None
        if manager_id:
            scopes.append(EntityScope(entity_type=ENTITY_MANAGER, entity_id=str(manager_id)))

    writer = DeltaReportWriter()
    _coro = writer.write(
        tenant=tenant, interaction=interaction, features=features, scopes=scopes
    )
    delta = _loop.run(_coro) if _loop is not None else asyncio.run(_coro)
    if delta:
        get_orchestrator().record_delta(
            session,
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            scopes=scopes,
            delta=delta,
        )


def _emit_webhooks_for_interaction(
    tenant_id,
    interaction_id,
    insights: Dict[str, Any],
    outcome_type: Optional[str],
    outcome_confidence: Optional[float],
    account_lifecycle_stage: str = "prospect",
) -> None:
    """Emit ``interaction.outcome_inferred`` for a freshly analyzed
    interaction (no-op when no outcome was inferred).

    ``interaction.analyzed`` is NOT emitted here: this function used to
    send a thin duplicate of it, so subscribers received two
    differently-shaped analyzed events per analysis. The single source
    for ``interaction.analyzed`` is the rich ``dispatch_sync`` payload
    at pipeline Step 19.

    Called from inside the sync Celery task, so we hop into an async
    session via ``asyncio.run``. Never blocks on HTTP — ``emit_event``
    writes delivery rows and enqueues the delivery task.
    """
    if not outcome_type:
        return

    from backend.app.db import async_session
    from backend.app.services.webhook_dispatcher import emit_event

    summary = {
        "interaction_id": str(interaction_id),
        "summary": (insights or {}).get("summary", "")[:600],
        "sentiment_overall": (insights or {}).get("sentiment_overall"),
        "sentiment_score": (insights or {}).get("sentiment_score"),
        # Clients only — churn is meaningless for a prospect/lead.
        "churn_risk_signal": (
            (insights or {}).get("churn_risk_signal")
            if account_lifecycle_stage == "client"
            else None
        ),
        "upsell_signal": (insights or {}).get("upsell_signal"),
        "lifecycle_stage": account_lifecycle_stage,
    }

    async def _runner() -> None:
        from backend.app.tenant_ctx import tenant_context_async

        # Fresh session — bind the tenant GUC (RLS blocks the delivery-row
        # INSERT without it) and commit; nothing else owns this session's
        # transaction.
        async with async_session() as db:
            async with tenant_context_async(tenant_id, db):
                await emit_event(
                    db,
                    tenant_id,
                    "interaction.outcome_inferred",
                    {
                        **summary,
                        "outcome_type": outcome_type,
                        "outcome_confidence": outcome_confidence,
                    },
                )
                await db.commit()

    try:
        _run_async(_runner)
    except Exception:
        logger.exception(
            "Webhook emission failed for interaction %s", interaction_id
        )


# ── Celery Tasks ─────────────────────────────────────────────────────────


@celery_app.task(bind=True, name="process_voice_interaction", max_retries=3)
def process_voice_interaction(self, interaction_id: str) -> Dict[str, Any]:
    """Full batch pipeline for a voice upload.

    Steps:
    1.  Load interaction from DB
    2.  Load tenant config
    3.  Load audio file
    4.  Transcribe audio → segments
    5.  PII redaction (if enabled)
    6.  Call metrics
    7.  Compress transcript for LLM
    8.  Triage — complexity scoring
    9.  AI analysis (Haiku or Sonnet based on triage)
    10. Scorecard scoring
    11. Snippet identification
    12. Search indexing
    13. Update interaction row
    14. Insert action items
    15. Insert interaction scores
    16. Insert interaction snippets
    17. Fire outbound webhooks
    """
    from backend.app.models import Interaction, Tenant

    logger.info("Starting voice pipeline for interaction %s", interaction_id)
    # RLS: the task_prerun hook already bound this interaction's tenant
    # (resolved via the SECURITY DEFINER bootstrap) — every query below is
    # tenant-scoped.
    session = _get_sync_session()

    try:
        # ── Step 1: Load interaction ─────────────────────────────────
        interaction = (
            session.query(Interaction)
            .filter(Interaction.id == uuid.UUID(interaction_id))
            .first()
        )
        if interaction is None:
            logger.error("Interaction %s not found", interaction_id)
            return {"status": "error", "detail": "Interaction not found"}

        # ── Step 2: Load tenant config ───────────────────────────────
        tenant = (
            session.query(Tenant)
            .filter(Tenant.id == interaction.tenant_id)
            .first()
        )
        if tenant is None:
            logger.error("Tenant not found for interaction %s", interaction_id)
            return {"status": "error", "detail": "Tenant not found"}

        # ── Steps 3–4: Transcription ─────────────────────────────────
        # Three paths:
        #   (a) live-call interactions already have ``transcript`` filled
        #       by the Media Streams WebSocket handler → use as-is.
        #   (b) direct uploads landed their bytes in S3 staging under
        #       ``audio_s3_key`` → download, transcribe, delete.
        #   (c) external recording systems pushed us a pointer →
        #       ``audio_url`` is set → stream that URL directly to
        #       Deepgram (we never touch the bytes).

        segments_dicts: Optional[List[Dict[str, Any]]] = None
        staged_key: Optional[str] = None
        staged_path: Optional[str] = None

        if interaction.transcript and len(interaction.transcript) > 0:
            segments_dicts = interaction.transcript
            logger.info(
                "Using pre-populated transcript (%d segments) for interaction %s",
                len(segments_dicts), interaction_id,
            )
        elif interaction.audio_s3_key or interaction.audio_url:
            from backend.app.services import s3_audio
            from backend.app.services.transcription import TranscriptionService

            engine = interaction.engine or tenant.transcription_engine or "deepgram"
            keyterms = getattr(tenant, "keyterm_boost_list", None) or None
            language = getattr(tenant, "transcription_language", None) or "en"
            # Tenants can opt to a cheaper Deepgram model (nova-2) for
            # routine support traffic via features_enabled["deepgram_model"].
            # Defaults to nova-3 when unset.
            tenant_features_for_engine = getattr(tenant, "features_enabled", None) or {}
            deepgram_model = tenant_features_for_engine.get("deepgram_model")
            svc = TranscriptionService()

            # Paralinguistic extraction needs a local file. In URL mode
            # we'd normally hand the URL to Deepgram without downloading,
            # but if the tenant opted into paralinguistics we need the
            # bytes on disk — so fetch once and reuse.
            tenant_features = getattr(tenant, "features_enabled", None) or {}
            want_paralinguistic = bool(
                tenant_features.get("paralinguistic_analysis", True)
            )

            # Exactly-once claim for the paid Deepgram/Whisper call. The
            # transcript is already persisted+committed the moment
            # transcription returns (below), so retries short-circuit via
            # the pre-populated-transcript branch above; the claim closes
            # the remaining hole — two *concurrent* deliveries both
            # observing an empty transcript and both paying to transcribe.
            from backend.app.services.pipeline_ledger import (
                STEP_TRANSCRIPTION,
                StepClaim,
                claim_step,
                complete_step,
                compute_input_hash,
                fail_step,
            )

            _tr_run_id: Optional[uuid.UUID] = None
            _tr_claim = claim_step(
                session,
                tenant_id=tenant.id,
                interaction_id=interaction.id,
                step_key=STEP_TRANSCRIPTION,
                input_hash=compute_input_hash(
                    interaction.audio_s3_key,
                    interaction.audio_url,
                    engine,
                    deepgram_model,
                    language,
                ),
                worker_id=_worker_id(),
            )
            if _tr_claim.outcome == StepClaim.HELD:
                raise StepHeldError(
                    "transcription for interaction %s held by another worker"
                    % interaction_id
                )
            if _tr_claim.outcome == StepClaim.REUSED:
                session.refresh(interaction)
                if interaction.transcript and len(interaction.transcript) > 0:
                    segments_dicts = interaction.transcript
                    logger.info(
                        "Reusing persisted transcript (%d segments) for "
                        "interaction %s (ledger)",
                        len(segments_dicts), interaction_id,
                    )
                else:
                    logger.warning(
                        "Transcription ledger run %s for interaction %s is "
                        "'succeeded' but no transcript persisted — "
                        "re-transcribing without a claim",
                        _tr_claim.run_id, interaction_id,
                    )
            else:
                _tr_run_id = _tr_claim.run_id

            if segments_dicts is None:
                try:
                    if (
                        interaction.audio_url
                        and engine == "deepgram"
                        and not want_paralinguistic
                    ):
                        # URL mode: Deepgram fetches directly, we never stage.
                        segments = asyncio.run(
                            svc.transcribe(
                                audio_url=interaction.audio_url,
                                engine="deepgram",
                                language=language,
                                keyterms=keyterms,
                                model=deepgram_model,
                                tenant_features=tenant_features_for_engine,
                            )
                        )
                    else:
                        # Need a local path — from S3 staging, or download the
                        # URL first (Whisper + paralinguistic both want bytes).
                        if interaction.audio_s3_key:
                            staged_key = interaction.audio_s3_key
                            staged_path = s3_audio.download_to_tempfile(staged_key)
                        else:
                            import httpx
                            import tempfile

                            tmp = tempfile.NamedTemporaryFile(
                                prefix="linda-audio-", suffix=".bin", delete=False
                            )
                            try:
                                with httpx.Client(timeout=60.0) as client:
                                    resp = client.get(interaction.audio_url)
                                    resp.raise_for_status()
                                    tmp.write(resp.content)
                            finally:
                                tmp.close()
                            staged_path = tmp.name

                        segments = asyncio.run(
                            svc.transcribe(
                                audio_path=staged_path,
                                engine=engine,
                                language=language,
                                keyterms=keyterms,
                                model=deepgram_model,
                                tenant_features=tenant_features_for_engine,
                            )
                        )
                    segments_dicts = _segments_to_dicts(segments)
                    interaction.transcript = segments_dicts
                    # Persist duration_seconds from the last segment if not set.
                    if not interaction.duration_seconds and segments:
                        interaction.duration_seconds = int(segments[-1].end)
                    # Persist-after-pay: transcript + succeeded ledger row
                    # land in the same commit.
                    if _tr_run_id is not None:
                        complete_step(
                            session, _tr_run_id,
                            output_digest="interaction.transcript",
                            commit=False,
                        )
                    session.commit()
                except Exception as _tr_exc:
                    logger.exception(
                        "Transcription failed for interaction %s", interaction_id
                    )
                    interaction.status = "transcription_failed"
                    if _tr_run_id is not None:
                        fail_step(
                            session, _tr_run_id,
                            error="%s: %s" % (type(_tr_exc).__name__, _tr_exc),
                            commit=False,
                        )
                    session.commit()
                    # Clean up staged bytes on transcription failure too —
                    # the rest of the pipeline won't run.
                    _cleanup_staged_audio(session, interaction, staged_path, staged_key)
                    raise
        else:
            logger.warning(
                "Interaction %s has no transcript, audio_s3_key, or audio_url",
                interaction_id,
            )
            interaction.status = "transcription_pending"
            session.commit()
            return {
                "status": "transcription_pending",
                "detail": "No audio source available",
            }

        # ── Steps 5–17: Run shared pipeline ──────────────────────────
        try:
            _run_pipeline(
                session,
                interaction_id,
                segments_dicts,
                tenant,
                interaction,
                audio_path=staged_path,
            )
        finally:
            # Paralinguistic extraction has had its shot — evict the
            # audio bytes. Keeps us aligned with the no-retention
            # product policy.
            _cleanup_staged_audio(session, interaction, staged_path, staged_key)
            staged_path = None
            staged_key = None

        try:
            from backend.app.services.metrics import PIPELINE_RUNS

            PIPELINE_RUNS.labels(
                channel=interaction.channel or "unknown",
                status="success",
            ).inc()
        except Exception:
            pass
        return {"status": "analyzed", "interaction_id": interaction_id}

    except StepHeldError as exc:
        # Another worker holds the analysis lease (duplicate delivery /
        # double enqueue). Not a failure: leave status + insights alone
        # and check back after the lease holder has had time to finish.
        session.rollback()
        logger.info(
            "Voice pipeline deferring interaction %s: %s", interaction_id, exc
        )
        raise self.retry(exc=exc, countdown=120)
    except Exception as exc:
        session.rollback()
        logger.exception(
            "Voice pipeline failed for interaction %s", interaction_id
        )
        # Update status to failed AND surface the exception in
        # ``insights['error']`` so the API + dashboard can show what
        # actually broke. Without this the row just goes opaque.
        try:
            interaction = (
                session.query(Interaction)
                .filter(Interaction.id == uuid.UUID(interaction_id))
                .first()
            )
            if interaction:
                interaction.status = "failed"
                err_payload = {
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "step": "voice_pipeline",
                    "retry_count": self.request.retries,
                }
                # Preserve any prior insights instead of clobbering.
                merged = dict(interaction.insights or {})
                merged.update(err_payload)
                interaction.insights = merged
                session.commit()
        except Exception:
            logger.exception("Failed to update interaction status to 'failed'")
        raise self.retry(exc=exc, countdown=60)
    finally:
        session.close()


@celery_app.task(bind=True, name="process_text_interaction", max_retries=3)
def process_text_interaction(self, interaction_id: str) -> Dict[str, Any]:
    """Batch pipeline for a text-based interaction (email, transcript).

    Similar to :func:`process_voice_interaction` but skips audio download
    and transcription (steps 3–4).  Uses ``raw_text`` from the interaction
    directly, converting it into a single-segment transcript.
    """
    from backend.app.models import Interaction, Tenant

    logger.info("Starting text pipeline for interaction %s", interaction_id)
    # RLS: tenant bound by the task_prerun hook, same as the voice pipeline.
    session = _get_sync_session()

    try:
        # ── Step 1: Load interaction ─────────────────────────────────
        interaction = (
            session.query(Interaction)
            .filter(Interaction.id == uuid.UUID(interaction_id))
            .first()
        )
        if interaction is None:
            logger.error("Interaction %s not found", interaction_id)
            return {"status": "error", "detail": "Interaction not found"}

        # ── Step 2: Load tenant config ───────────────────────────────
        tenant = (
            session.query(Tenant)
            .filter(Tenant.id == interaction.tenant_id)
            .first()
        )
        if tenant is None:
            logger.error("Tenant not found for interaction %s", interaction_id)
            return {"status": "error", "detail": "Tenant not found"}

        # ── Build segments from raw_text ─────────────────────────────
        # For text channels there is no audio — parse speaker tags
        # ("REP:", "PROSPECT:", "Maria Chen:") into proper turns. Falls
        # back to a single segment when the source has no recognizable
        # tags so anything unstructured still flows through the pipeline.
        if interaction.transcript and len(interaction.transcript) > 0:
            segments_dicts = interaction.transcript
        elif interaction.raw_text:
            from backend.app.services.pipeline_ledger import (
                STEP_SEGMENTATION,
                StepClaim,
                claim_step,
                complete_step,
                compute_input_hash,
                fail_step,
            )
            from backend.app.services.text_segmenter import segments_from_text

            # Exactly-once claim for the segmenter's possible 30-60s Haiku
            # call. The claim's commit also releases the DB connection
            # before the LLM round-trip (Neon kills idle connections —
            # this replaces the bare session.commit() that lived here).
            # Committing here is safe — only READ queries so far.
            _seg_run_id: Optional[uuid.UUID] = None
            _seg_claim = claim_step(
                session,
                tenant_id=tenant.id,
                interaction_id=interaction.id,
                step_key=STEP_SEGMENTATION,
                input_hash=compute_input_hash(
                    interaction.raw_text, interaction.duration_seconds
                ),
                worker_id=_worker_id(),
            )
            if _seg_claim.outcome == StepClaim.HELD:
                raise StepHeldError(
                    "segmentation for interaction %s held by another worker"
                    % interaction_id
                )
            segments_dicts = None
            if _seg_claim.outcome == StepClaim.REUSED:
                session.refresh(interaction)
                if interaction.transcript and len(interaction.transcript) > 0:
                    segments_dicts = interaction.transcript
                    logger.info(
                        "Reusing persisted segmentation for interaction %s (ledger)",
                        interaction_id,
                    )
                else:
                    logger.warning(
                        "Segmentation ledger run %s for interaction %s is "
                        "'succeeded' but no transcript persisted — re-running "
                        "without a claim",
                        _seg_claim.run_id, interaction_id,
                    )
            else:
                _seg_run_id = _seg_claim.run_id

            if segments_dicts is None:
                try:
                    segments_dicts = segments_from_text(
                        interaction.raw_text,
                        duration_seconds=interaction.duration_seconds,
                    )
                except Exception as _seg_exc:
                    if _seg_run_id is not None:
                        fail_step(
                            session, _seg_run_id,
                            error="%s: %s" % (type(_seg_exc).__name__, _seg_exc),
                        )
                    raise
                if segments_dicts:
                    # Persist-after-pay: transcript + succeeded ledger row
                    # in one commit, so a later-step failure never re-pays
                    # the segmenter.
                    interaction.transcript = segments_dicts
                    if _seg_run_id is not None:
                        complete_step(
                            session, _seg_run_id,
                            output_digest="interaction.transcript",
                            commit=False,
                        )
                    session.commit()
            if not segments_dicts:
                logger.error(
                    "Text segmenter returned empty for interaction %s",
                    interaction_id,
                )
                interaction.status = "failed"
                if _seg_run_id is not None:
                    fail_step(
                        session, _seg_run_id, error="empty segmentation",
                        commit=False,
                    )
                session.commit()
                return {"status": "error", "detail": "Empty raw_text"}
        else:
            logger.error(
                "No text content for interaction %s", interaction_id
            )
            interaction.status = "failed"
            session.commit()
            return {"status": "error", "detail": "No text content"}

        # ── Steps 5–17: Run shared pipeline ──────────────────────────
        _run_pipeline(session, interaction_id, segments_dicts, tenant, interaction)

        try:
            from backend.app.services.metrics import PIPELINE_RUNS

            PIPELINE_RUNS.labels(
                channel=interaction.channel or "unknown",
                status="success",
            ).inc()
        except Exception:
            pass
        return {"status": "analyzed", "interaction_id": interaction_id}

    except StepHeldError as exc:
        # See the voice-task handler: defer, don't fail.
        session.rollback()
        logger.info(
            "Text pipeline deferring interaction %s: %s", interaction_id, exc
        )
        raise self.retry(exc=exc, countdown=120)
    except Exception as exc:
        session.rollback()
        logger.exception(
            "Text pipeline failed for interaction %s", interaction_id
        )
        try:
            interaction = (
                session.query(Interaction)
                .filter(Interaction.id == uuid.UUID(interaction_id))
                .first()
            )
            if interaction:
                interaction.status = "failed"
                err_payload = {
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "step": "text_pipeline",
                    "retry_count": self.request.retries,
                }
                merged = dict(interaction.insights or {})
                merged.update(err_payload)
                interaction.insights = merged
                session.commit()
        except Exception:
            logger.exception("Failed to update interaction status to 'failed'")
        raise self.retry(exc=exc, countdown=60)
    finally:
        session.close()


# ── Scheduled periodic tasks ─────────────────────────────────────────────


@celery_app.task(name="email_push_process_gmail", bind=True, max_retries=3)
def email_push_process_gmail(self, integration_id: str, new_history_id: str) -> Dict[str, Any]:
    """Diff Gmail history from the cursor forward and ingest new messages.

    Called by the Pub/Sub push endpoint.  Keeps the HTTP handler fast:
    all API calls + DB writes happen here.
    """
    import asyncio as _asyncio

    from backend.app.models import EmailSyncCursor, Integration, Tenant, User
    from backend.app.services.email_classifier import EmailClassifier
    from backend.app.services.email_ingest.ingest import ingest_email
    from backend.app.services.email_ingest.poller import refresh_if_expired_sync
    from backend.app.services.email_ingest.push import fetch_gmail_since_history

    session = _get_sync_session()
    try:
        integration = (
            session.query(Integration)
            .filter(Integration.id == uuid.UUID(integration_id))
            .first()
        )
        if integration is None:
            return {"status": "integration_missing"}

        tenant = session.query(Tenant).filter(Tenant.id == integration.tenant_id).first()
        if tenant is None:
            return {"status": "tenant_missing"}

        user = (
            session.query(User).filter(User.id == integration.user_id).first()
            if integration.user_id else None
        )
        agent_email = user.email if user else None

        cursor = (
            session.query(EmailSyncCursor)
            .filter(EmailSyncCursor.integration_id == integration.id)
            .first()
        )
        if cursor is None:
            cursor = EmailSyncCursor(
                integration_id=integration.id,
                tenant_id=integration.tenant_id,
                provider="google",
            )
            session.add(cursor)
            session.flush()
        start_history = cursor.history_id or new_history_id

        access_token = refresh_if_expired_sync(session, integration)
        classifier = EmailClassifier()
        ingested = 0

        async def _run():
            nonlocal ingested
            for msg in fetch_gmail_since_history(access_token, start_history, agent_email):
                if await ingest_email(session, tenant, msg, classifier) is not None:
                    ingested += 1

        _asyncio.run(_run())
        # Always move the cursor forward even when nothing ingested, so
        # an internal-only burst doesn't make us keep re-diffing it.
        cursor.history_id = new_history_id
        session.commit()
        return {"status": "ok", "ingested": ingested}

    except Exception as exc:
        session.rollback()
        logger.exception("Gmail push task failed")
        raise self.retry(exc=exc, countdown=30)
    finally:
        session.close()


@celery_app.task(name="email_push_process_graph", bind=True, max_retries=3)
def email_push_process_graph(
    self,
    integration_id: str,
    message_id: str,
    parent_folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a single Graph message and route it through the ingest path."""
    import asyncio as _asyncio

    from backend.app.models import Integration, Tenant, User
    from backend.app.services.email_classifier import EmailClassifier
    from backend.app.services.email_ingest.ingest import ingest_email
    from backend.app.services.email_ingest.poller import refresh_if_expired_sync
    from backend.app.services.email_ingest.push import fetch_graph_message

    session = _get_sync_session()
    try:
        integration = (
            session.query(Integration)
            .filter(Integration.id == uuid.UUID(integration_id))
            .first()
        )
        if integration is None:
            return {"status": "integration_missing"}

        tenant = session.query(Tenant).filter(Tenant.id == integration.tenant_id).first()
        if tenant is None:
            return {"status": "tenant_missing"}

        user = (
            session.query(User).filter(User.id == integration.user_id).first()
            if integration.user_id else None
        )
        agent_email = user.email if user else None

        access_token = refresh_if_expired_sync(session, integration)

        # Folder id hint from the notification is the fastest direction
        # signal; otherwise we infer from sender vs. agent email.
        direction_hint = None
        if parent_folder_id:
            lowered = parent_folder_id.lower()
            if "sent" in lowered:
                direction_hint = "outbound"
            elif "inbox" in lowered:
                direction_hint = "inbound"

        msg = fetch_graph_message(access_token, message_id, agent_email, direction_hint)
        if msg is None:
            session.commit()
            return {"status": "fetch_failed"}

        classifier = EmailClassifier()
        async def _run():
            return await ingest_email(session, tenant, msg, classifier)

        ingested_id = _asyncio.run(_run())
        session.commit()
        return {"status": "ok", "ingested": bool(ingested_id)}

    except Exception as exc:
        session.rollback()
        logger.exception("Graph push task failed")
        raise self.retry(exc=exc, countdown=30)
    finally:
        session.close()


@celery_app.task(name="email_backfill_run", bind=True, max_retries=0)
def email_backfill_run(self, job_id: str) -> Dict[str, Any]:
    """Run one historical mailbox import (``EmailBackfillJob``).

    Enqueued by ``POST /api/v1/email/backfill``. No Celery retries —
    the service marks the job row ``error`` with a reason, and the
    caller re-triggers explicitly (dedupe makes a re-run a cheap
    catch-up over the already-imported prefix).

    Backstop: if an exception ever escapes ``run_backfill`` anyway, the
    job row is flipped to ``error`` here before re-raising — a job left
    at ``running`` with no live worker would otherwise wedge the start
    endpoint's in-flight guard for the tenant.
    """
    from backend.app.services.email_ingest.backfill import run_backfill

    session = _get_sync_session()
    try:
        return run_backfill(session, job_id)
    except Exception as exc:
        try:
            from datetime import datetime, timezone

            from backend.app.models import EmailBackfillJob

            session.rollback()
            job = (
                session.query(EmailBackfillJob)
                .filter(EmailBackfillJob.id == uuid.UUID(str(job_id)))
                .first()
            )
            if job is not None and job.status in ("queued", "running"):
                job.status = "error"
                job.error = f"Sync failed unexpectedly: {exc}"
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:  # noqa: BLE001 — backstop is best-effort
            logger.exception(
                "Could not mark backfill job %s as error after task failure", job_id
            )
        raise
    finally:
        session.close()


@celery_app.task(name="backfill_sentiment_scores", bind=True, max_retries=0)
def backfill_sentiment_scores(self, tenant_id: str) -> Dict[str, Any]:
    """Re-derive ``insights['sentiment_score']`` on a tenant's analyzed
    interactions and rebuild each ``Contact.sentiment_trend``.

    Repairs rows scored before the ``resolve_sentiment_score`` scale
    guard landed: the analyzer occasionally emitted ``sentiment_score_direct``
    on a 0-1 scale (e.g. ``0.7`` for an enthusiastic call), which passed
    the old ``0 <= x <= 10`` check and leaked into the 0-10 field — so an
    engaged prospect rendered as ~0.7/10 and the UI labeled them
    "Negative". Recomputes from the stored bucket / direct read via the
    same resolver the live pipeline now uses, so it's idempotent: once a
    row is corrected, a re-run is a no-op.

    Scoped to one tenant (enqueued by ``POST /admin/backfill-sentiment-scores``).
    """
    from sqlalchemy.orm.attributes import flag_modified

    from backend.app.models import Contact, Interaction
    from backend.app.services.score_mapping import resolve_sentiment_score
    from backend.app.tenant_ctx import tenant_context

    tid = uuid.UUID(str(tenant_id))
    session = _get_sync_session()
    scanned = 0
    updated = 0
    contacts_rebuilt = 0
    try:
        with tenant_context(tid, session):
            rows = (
                session.query(Interaction)
                .filter(Interaction.tenant_id == tid)
                .order_by(Interaction.created_at.asc())
                .all()
            )
            # Replay corrected per-interaction sentiment into per-contact
            # trends, in chronological order (matches the live rollup).
            trends: Dict[uuid.UUID, List[float]] = {}
            for ix in rows:
                scanned += 1
                insights = ix.insights or {}
                if not insights:
                    continue
                corrected = resolve_sentiment_score(insights)
                if corrected is None:
                    continue
                if insights.get("sentiment_score") != corrected:
                    insights["sentiment_score"] = corrected
                    ix.insights = insights
                    flag_modified(ix, "insights")
                    updated += 1
                if ix.contact_id is not None:
                    trends.setdefault(ix.contact_id, []).append(float(corrected))

            if trends:
                contact_rows = (
                    session.query(Contact)
                    .filter(
                        Contact.tenant_id == tid,
                        Contact.id.in_(list(trends.keys())),
                    )
                    .all()
                )
                for c in contact_rows:
                    series = trends.get(c.id) or []
                    c.sentiment_trend = series[-CONTACT_SENTIMENT_TREND_CAP:]
                    contacts_rebuilt += 1
            session.commit()
    finally:
        session.close()

    result = {
        "tenant_id": str(tid),
        "scanned": scanned,
        "updated": updated,
        "contacts_rebuilt": contacts_rebuilt,
    }
    logger.info("backfill_sentiment_scores: %s", result)
    return result


@celery_app.task(name="backfill_sentiment_scores_all_tenants")
def backfill_sentiment_scores_all_tenants() -> Dict[str, Any]:
    """Dispatcher: repair poisoned sentiment scores across EVERY tenant.

    Fans out one ``backfill_sentiment_scores`` subtask per tenant (each
    with its own session + loop), the same pattern the trend-scan
    dispatchers use. Per-tenant work is idempotent — once a row is
    corrected a re-run is a no-op — so this is safe to invoke more than
    once. Run it once after the sentiment scale-guard deploys to correct
    historical rows platform-wide.
    """
    from celery import group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    group(backfill_sentiment_scores.s(tid) for tid in tenant_ids).apply_async()
    return {"dispatched_tenants": len(tenant_ids)}


@celery_app.task(name="email_push_renew_subscriptions")
def email_push_renew_subscriptions() -> Dict[str, Any]:
    """(Re-)register Gmail watches and Graph subscriptions.

    Runs on a 12h schedule.  Expired watches/subscriptions are simply
    recreated; the provider returns the same stream so we pick up
    wherever we left off.  Requires PUBLIC_WEBHOOK_BASE_URL /
    GMAIL_PUBSUB_TOPIC / GRAPH_CLIENT_STATE to be configured —
    otherwise the task no-ops.
    """
    from backend.app.models import EmailSyncCursor, Integration
    from backend.app.services.email_ingest.poller import (
        IntegrationAuthError,
        mark_needs_reauth,
        refresh_if_expired_sync,
    )
    from backend.app.services.email_ingest.push import (
        subscribe_graph_mailbox,
        watch_gmail,
    )

    s = get_settings()
    base_url = s.PUBLIC_WEBHOOK_BASE_URL.rstrip("/")
    if not base_url:
        logger.info("PUBLIC_WEBHOOK_BASE_URL unset — skipping push renewal")
        return {"status": "skipped", "reason": "no_public_url"}

    from datetime import datetime, timedelta, timezone

    session = _get_sync_session()
    gmail_ok = graph_ok = failed = skipped = 0
    now = datetime.now(timezone.utc)
    # Renew when the existing subscription is inside the 24 h tail of its
    # lifetime — re-registering earlier just burns Google / Microsoft API
    # calls (verified against the audit's ~$5/month leak estimate).
    renew_horizon = now + timedelta(hours=24)
    from backend.app.tenant_ctx import tenant_context

    try:
        integrations = (
            session.query(Integration)
            .filter(Integration.provider.in_(["google", "microsoft"]))
            .all()
        )
        for integ in integrations:
            with tenant_context(integ.tenant_id, session):
                # Already flagged dead → don't re-hit the token endpoint.
                if (integ.provider_config or {}).get("needs_reauth"):
                    skipped += 1
                    continue
                try:
                    access_token = refresh_if_expired_sync(session, integ)
                except IntegrationAuthError as exc:
                    # Non-retryable auth failure: WARNING (not ERROR) so it
                    # doesn't flood Sentry, and flag for re-auth.
                    logger.warning("Integration %s needs re-auth: %s", integ.id, exc)
                    session.rollback()
                    mark_needs_reauth(session, integ)
                    skipped += 1
                    continue
                except Exception:
                    failed += 1
                    logger.exception("Refresh failed for integration %s", integ.id)
                    session.rollback()
                    continue

                cursor = (
                    session.query(EmailSyncCursor)
                    .filter(EmailSyncCursor.integration_id == integ.id)
                    .first()
                )
                if cursor is None:
                    cursor = EmailSyncCursor(
                        integration_id=integ.id,
                        tenant_id=integ.tenant_id,
                        provider=integ.provider,
                    )
                    session.add(cursor)
                    session.flush()

                # Skip if the existing subscription is still healthy. NULL
                # expires_at means "never registered" → always renew.
                existing_expiry = cursor.push_subscription_expires_at
                if existing_expiry is not None and existing_expiry > renew_horizon:
                    skipped += 1
                    continue

                try:
                    if integ.provider == "google" and s.GMAIL_PUBSUB_TOPIC:
                        resp = watch_gmail(access_token, s.GMAIL_PUBSUB_TOPIC)
                        # Persist the watch's historyId so the first push
                        # notification has something to diff against.
                        cursor.history_id = str(resp.get("historyId") or cursor.history_id or "")
                        # Gmail returns expiration as epoch milliseconds (string).
                        raw_exp = resp.get("expiration")
                        if raw_exp:
                            try:
                                cursor.push_subscription_expires_at = (
                                    datetime.fromtimestamp(int(raw_exp) / 1000, tz=timezone.utc)
                                )
                            except (TypeError, ValueError):
                                pass
                        gmail_ok += 1
                    elif integ.provider == "microsoft" and s.GRAPH_CLIENT_STATE:
                        notification_url = (
                            f"{base_url}{s.API_V1_PREFIX}/email-push/graph"
                        )
                        resp = subscribe_graph_mailbox(
                            access_token,
                            notification_url=notification_url,
                            client_state=s.GRAPH_CLIENT_STATE,
                        )
                        # Reuse delta_link as a handle to the subscription id —
                        # the notification endpoint looks it up there.
                        cursor.delta_link = resp.get("id") or cursor.delta_link
                        # Graph returns ISO-8601 expirationDateTime.
                        raw_exp = resp.get("expirationDateTime")
                        if raw_exp:
                            try:
                                cursor.push_subscription_expires_at = (
                                    datetime.fromisoformat(raw_exp.replace("Z", "+00:00"))
                                )
                            except ValueError:
                                pass
                        graph_ok += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "Push subscription failed for integration %s (%s)",
                        integ.id, integ.provider,
                    )
        session.commit()
    finally:
        session.close()

    return {
        "status": "ok",
        "gmail_subscribed": gmail_ok,
        "graph_subscribed": graph_ok,
        "skipped_healthy": skipped,
        "failed": failed,
    }


@celery_app.task(name="email_ingest_poll")
def email_ingest_poll() -> Dict[str, Any]:
    """Poll every connected Google/Microsoft integration for new mail.

    Scheduled every 2 minutes by Celery Beat.  Each integration advances
    its own ``EmailSyncCursor`` so we only fetch deltas.  External,
    customer-facing emails are created as ``Interaction(channel='email')``
    rows and enqueued for the standard text-analysis pipeline.  Internal
    emails are dropped with a log line and never touch the Interaction
    table.
    """
    from backend.app.services.email_ingest.poller import poll_all

    session = _get_sync_session()
    try:
        return poll_all(session)
    finally:
        session.close()


@celery_app.task(name="outreach_scheduler_tick")
def outreach_scheduler_tick() -> Dict[str, Any]:
    """Cold-outreach send engine — one pass over every tenant.

    Triggered by Celery Beat every 10 minutes. Walks the (global)
    tenants table, enters tenant_context per tenant, and for each
    active outreach campaign sends due approved drafts inside the
    campaign's send window and daily quota, surfaces due follow-up
    bumps back into the draft/approval flow, and completes campaigns
    with no actionable members left. See
    backend/app/services/outreach/scheduler.py.
    """
    from backend.app.services.outreach.scheduler import run_all_tenants

    session = _get_sync_session()
    try:
        return run_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="outreach_generate_drafts")
def outreach_generate_drafts(
    tenant_id: str,
    campaign_id: str,
    member_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Fan out per-prospect draft personalization for a campaign.

    Enqueued by POST /outreach/campaigns/{id}/generate-drafts (and by
    activate when drafts are missing). One Sonnet call per member;
    commits per member so progress survives a worker restart.
    """
    from backend.app.models import Campaign as _Campaign
    from backend.app.models import Tenant as _Tenant
    from backend.app.services.outreach.scheduler import generate_drafts_for_campaign
    from backend.app.tenant_ctx import tenant_context

    session = _get_sync_session()
    try:
        with tenant_context(tenant_id, session):
            campaign = session.get(_Campaign, uuid.UUID(campaign_id))
            tenant = session.get(_Tenant, uuid.UUID(tenant_id))
            if campaign is None or tenant is None:
                return {"error": "campaign_or_tenant_not_found"}
            ids = [uuid.UUID(m) for m in member_ids] if member_ids else None
            return generate_drafts_for_campaign(
                session, tenant, campaign, member_ids=ids
            )
    finally:
        session.close()


@celery_app.task(name="embed_support_case_subject", max_retries=2, bind=True)
def embed_support_case_subject(self, case_id: str) -> Dict[str, Any]:
    """Embed one SupportCase's subject off the case-creation hot path.

    The daily trend scan previously absorbed all the embedding work in a
    07:00 UTC spike; this lets cases get embedded as they're created so
    the daily scan only has to mop up stragglers. Failures don't
    propagate — the daily scan's TTL+missing-embedding query still
    catches anything this task missed.

    Idempotent: skips if the case already has a fresh embedding.
    """
    import asyncio as _asyncio
    from datetime import timezone as _tz

    from backend.app.models import SupportCase
    from backend.app.services.support_trend_detector import EMBED_TTL_DAYS

    session = _get_sync_session()
    try:
        case = (
            session.query(SupportCase)
            .filter(SupportCase.id == uuid.UUID(case_id))
            .first()
        )
        if case is None or not case.subject:
            return {"status": "skipped", "reason": "case_missing_or_no_subject"}
        if case.embedded_at is not None:
            age = datetime.now(_tz.utc) - case.embedded_at
            if age.days < EMBED_TTL_DAYS:
                return {"status": "skipped", "reason": "fresh_embedding"}
        try:
            from backend.app.services.embeddings import VoyageEmbedder
        except Exception:
            return {"status": "skipped", "reason": "voyage_unavailable"}
        embedder = VoyageEmbedder()

        async def _embed() -> List[List[float]]:
            return await embedder.embed([case.subject], input_type="document")

        vecs = _asyncio.run(_embed())
        if not vecs:
            return {"status": "skipped", "reason": "no_embedding_returned"}
        case.subject_embedding = list(vecs[0])
        case.embedded_at = datetime.now(_tz.utc)
        session.commit()
        return {"status": "embedded", "case_id": case_id}
    except Exception as exc:
        logger.exception("embed_support_case_subject failed for %s", case_id)
        session.rollback()
        raise self.retry(exc=exc, countdown=60)
    finally:
        session.close()


@celery_app.task(name="recompute_llm_ceilings")
def recompute_llm_ceilings() -> Dict[str, Any]:
    """Daily aggregation of ``llm_call_telemetry`` into
    ``llm_ceiling_recommendation``. Once a (call_site, tier) has enough
    history, ``compute_max_tokens`` will pick up the learned ceiling on
    the next request (in-process cache TTL = 1h)."""
    from backend.app.services.llm_telemetry import recompute_ceilings

    return recompute_ceilings()


@celery_app.task(name="tenant_insights_weekly")
def tenant_insights_weekly() -> Dict[str, Any]:
    """Weekly rollup of tenant-level insights.

    Writes/updates a ``TenantInsight`` row per tenant for the last 7 days.
    Triggered by Celery Beat (see ``beat_schedule`` above).
    """
    from backend.app.services.tenant_insights_service import rollup_all_tenants_weekly

    session = _get_sync_session()
    try:
        processed = rollup_all_tenants_weekly(session)
        return {"tenants_processed": processed}
    finally:
        session.close()


def _all_tenant_ids() -> List[str]:
    """Snapshot tenant ids on a short-lived session (dispatcher helper)."""
    from backend.app.models import Tenant

    session = _get_sync_session()
    try:
        return [str(t.id) for t in session.query(Tenant.id).all()]
    finally:
        session.close()


@celery_app.task(name="_log_scan_aggregate")
def _log_scan_aggregate(
    results: List[Optional[Dict[str, Any]]], scan_name: str
) -> Dict[str, Any]:
    """Chord callback: the single honest aggregate for a fan-out scan.

    Runs only after every per-tenant subtask finished, so it reports
    *real* outcomes — unlike the old sync-wait dispatchers, which
    reported "0 processed / all failed" whenever the wait timed out.
    """
    ok = 0
    failed: List[str] = []
    for r in results or []:
        if isinstance(r, dict) and not r.get("error"):
            ok += 1
        elif isinstance(r, dict):
            failed.append(str(r.get("tenant_id") or "unknown"))
        else:
            failed.append("unknown")
    summary = {
        "scan": scan_name,
        "tenants_processed": ok,
        "failed_tenants": failed,
    }
    log = logger.warning if failed else logger.info
    log(
        "%s aggregate: %d tenants processed, %d failed (%s)",
        scan_name, ok, len(failed), ",".join(failed) or "-",
    )
    return summary


@celery_app.task(name="support_trend_scan_tenant")
def support_trend_scan_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant slice of the support trend scan — own session, own
    event loop, so one slow tenant can't delay the others."""
    from backend.app.models import Tenant
    from backend.app.services.support_trend_detector import run_for_tenant

    session = _get_sync_session()
    try:
        tenant = (
            session.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        )
        if tenant is None:
            return {"tenant_id": tenant_id, "error": 1, "detail": "tenant_not_found"}
        result = _run_async(lambda: run_for_tenant(session, tenant))
        out: Dict[str, Any] = {"tenant_id": tenant_id}
        out.update(result or {})
        return out
    except Exception:
        logger.exception("Support trend scan failed for tenant %s", tenant_id)
        session.rollback()
        return {"tenant_id": tenant_id, "error": 1}
    finally:
        session.close()


@celery_app.task(name="support_trend_scan")
def support_trend_scan() -> Dict[str, Any]:
    """Daily AI-driven cross-customer trend scan — dispatcher.

    Fans out one subtask per tenant (each with its own session + loop;
    the old version ran all tenants sequentially under one shared sync
    session) and returns a dispatch receipt immediately. The chord
    callback logs the honest aggregate once every tenant finished.
    """
    from celery import chord, group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    header = group(support_trend_scan_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_log_scan_aggregate.s("support_trend_scan"))
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


@celery_app.task(name="cohort_recommendation_scan_tenant")
def cohort_recommendation_scan_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant slice of the cohort recommendation scan."""
    from backend.app.models import Tenant
    from backend.app.services.cohort_recommendations import run_for_tenant

    session = _get_sync_session()
    try:
        tenant = (
            session.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        )
        if tenant is None:
            return {"tenant_id": tenant_id, "error": 1, "detail": "tenant_not_found"}
        counts = run_for_tenant(session, tenant)
        out: Dict[str, Any] = {"tenant_id": tenant_id}
        out.update(counts or {})
        return out
    except Exception:
        logger.exception(
            "Cohort recommendation scan failed for tenant %s", tenant_id
        )
        session.rollback()
        return {"tenant_id": tenant_id, "error": 1}
    finally:
        session.close()


@celery_app.task(name="cohort_recommendation_scan")
def cohort_recommendation_scan() -> Dict[str, Any]:
    """Daily cohort detectors → ManagerRecommendation inserts — dispatcher.

    Same fan-out shape as ``support_trend_scan``: per-tenant subtasks,
    immediate dispatch receipt, honest aggregate in the chord callback.
    """
    from celery import chord, group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    header = group(cohort_recommendation_scan_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_log_scan_aggregate.s("cohort_recommendation_scan"))
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


@celery_app.task(name="sales_trend_scan_tenant")
def sales_trend_scan_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant slice of the sales trend scan — own session, own event
    loop, same shape as ``support_trend_scan_tenant``."""
    from backend.app.models import Tenant
    from backend.app.services.sales_trend_detector import run_for_tenant

    session = _get_sync_session()
    try:
        tenant = (
            session.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        )
        if tenant is None:
            return {"tenant_id": tenant_id, "error": 1, "detail": "tenant_not_found"}
        result = _run_async(lambda: run_for_tenant(session, tenant))
        out: Dict[str, Any] = {"tenant_id": tenant_id}
        out.update(result or {})
        return out
    except Exception:
        logger.exception("Sales trend scan failed for tenant %s", tenant_id)
        session.rollback()
        return {"tenant_id": tenant_id, "error": 1}
    finally:
        session.close()


@celery_app.task(name="sales_trend_scan")
def sales_trend_scan() -> Dict[str, Any]:
    """Daily AI-driven cross-customer sales trend scan — dispatcher.

    Same fan-out shape as ``support_trend_scan``: per-tenant subtasks,
    immediate dispatch receipt, honest aggregate in the chord callback.
    """
    from celery import chord, group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    header = group(sales_trend_scan_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_log_scan_aggregate.s("sales_trend_scan"))
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


@celery_app.task(name="cs_trend_scan_tenant")
def cs_trend_scan_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant slice of the CS trend scan."""
    from backend.app.models import Tenant
    from backend.app.services.cs_trend_detector import run_for_tenant

    session = _get_sync_session()
    try:
        tenant = (
            session.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        )
        if tenant is None:
            return {"tenant_id": tenant_id, "error": 1, "detail": "tenant_not_found"}
        result = _run_async(lambda: run_for_tenant(session, tenant))
        out: Dict[str, Any] = {"tenant_id": tenant_id}
        out.update(result or {})
        return out
    except Exception:
        logger.exception("CS trend scan failed for tenant %s", tenant_id)
        session.rollback()
        return {"tenant_id": tenant_id, "error": 1}
    finally:
        session.close()


@celery_app.task(name="cs_trend_scan")
def cs_trend_scan() -> Dict[str, Any]:
    """Daily AI-driven cross-customer CS trend scan — dispatcher."""
    from celery import chord, group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    header = group(cs_trend_scan_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_log_scan_aggregate.s("cs_trend_scan"))
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


@celery_app.task(name="concern_aggregation_scan_tenant")
def concern_aggregation_scan_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant slice of the cross-customer concern aggregation scan."""
    from backend.app.models import Tenant
    from backend.app.services.concern_aggregation import run_for_tenant

    session = _get_sync_session()
    try:
        tenant = (
            session.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        )
        if tenant is None:
            return {"tenant_id": tenant_id, "error": 1, "detail": "tenant_not_found"}
        result = _run_async(lambda: run_for_tenant(session, tenant))
        out: Dict[str, Any] = {"tenant_id": tenant_id}
        out.update(result or {})
        return out
    except Exception:
        logger.exception("Concern aggregation scan failed for tenant %s", tenant_id)
        session.rollback()
        return {"tenant_id": tenant_id, "error": 1}
    finally:
        session.close()


@celery_app.task(name="concern_aggregation_scan")
def concern_aggregation_scan() -> Dict[str, Any]:
    """Daily cross-customer concern aggregation scan — dispatcher."""
    from celery import chord, group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    header = group(concern_aggregation_scan_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_log_scan_aggregate.s("concern_aggregation_scan"))
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


@celery_app.task(name="broken_commitment_scan_tenant")
def broken_commitment_scan_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant slice of the broken-commitment scan. Deterministic, no
    LLM call, no event loop needed."""
    from backend.app.models import Tenant
    from backend.app.services.commitment_detector import detect_and_flag

    session = _get_sync_session()
    try:
        tenant = (
            session.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        )
        if tenant is None:
            return {"tenant_id": tenant_id, "error": 1, "detail": "tenant_not_found"}
        result = detect_and_flag(session, tenant)
        out: Dict[str, Any] = {"tenant_id": tenant_id}
        out.update(result or {})
        return out
    except Exception:
        logger.exception("Broken-commitment scan failed for tenant %s", tenant_id)
        session.rollback()
        return {"tenant_id": tenant_id, "error": 1}
    finally:
        session.close()


@celery_app.task(name="broken_commitment_scan")
def broken_commitment_scan() -> Dict[str, Any]:
    """Daily broken-commitment scan — dispatcher."""
    from celery import chord, group

    tenant_ids = _all_tenant_ids()
    if not tenant_ids:
        return {"dispatched_tenants": 0}
    header = group(broken_commitment_scan_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_log_scan_aggregate.s("broken_commitment_scan"))
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


@celery_app.task(name="reconcile_orphan_interactions")
def reconcile_orphan_interactions(
    batch_size: int = 50, lookback_days: int = 30
) -> Dict[str, Any]:
    """Detect-and-heal for orphaned interactions (docs/complexity/01 §3c).

    Entity resolution is best-effort in the pipeline: a failure lands
    the interaction as ``analyzed`` with no customer linkage. The step
    ledger makes that state discoverable — this sweeper scans for
    interactions whose entity_resolution run is ``failed``, re-claims
    each run through the ledger (atomic, so a racing sweeper or a
    concurrent pipeline retry loses cleanly), and re-runs resolution
    against the persisted insights + transcript.

    A ``succeeded`` run with no linkage means "genuinely nobody to
    resolve" and is intentionally left alone.
    """
    from datetime import timedelta

    from backend.app.models import Interaction, InteractionStepRun, Tenant
    from backend.app.services.pipeline_ledger import (
        STEP_ENTITY_RESOLUTION,
        StepClaim,
        claim_step,
        complete_step,
        fail_step,
    )

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    session = _get_sync_session()
    scanned = 0
    healed = 0
    failed = 0
    try:
        pairs = (
            session.query(Interaction, InteractionStepRun)
            .join(
                InteractionStepRun,
                (InteractionStepRun.interaction_id == Interaction.id)
                & (InteractionStepRun.step_key == STEP_ENTITY_RESOLUTION),
            )
            .filter(
                Interaction.status == "analyzed",
                Interaction.customer_id.is_(None),
                InteractionStepRun.status == "failed",
                Interaction.created_at >= cutoff,
            )
            .order_by(Interaction.created_at.asc())
            .limit(batch_size)
            .all()
        )
        if not pairs:
            return {"scanned": 0, "healed": 0, "failed": 0}

        tenants: Dict[Any, Any] = {}
        with _TaskEventLoop() as _loop:
            for interaction, run in pairs:
                scanned += 1
                tenant = tenants.get(interaction.tenant_id)
                if tenant is None:
                    tenant = (
                        session.query(Tenant)
                        .filter(Tenant.id == interaction.tenant_id)
                        .first()
                    )
                    tenants[interaction.tenant_id] = tenant
                if tenant is None:
                    continue

                claim = claim_step(
                    session,
                    tenant_id=tenant.id,
                    interaction_id=interaction.id,
                    step_key=STEP_ENTITY_RESOLUTION,
                    input_hash=run.input_hash,
                    worker_id=_worker_id(),
                )
                if claim.outcome != StepClaim.ACQUIRED:
                    # Healed by a pipeline retry, or another sweeper is
                    # on it right now — either way, not ours.
                    continue

                # Rebuild the compressed transcript from the persisted
                # segments — good enough for the resolver's name/context
                # matching (the original compression only drops filler).
                transcript = interaction.transcript or []
                compressed = "\n".join(
                    "%s: %s" % (s.get("speaker_id") or "?", s.get("text") or "")
                    for s in transcript
                    if isinstance(s, dict)
                )
                try:
                    from backend.app.services.entity_resolution import (
                        resolve_interaction_entities,
                    )

                    resolution = _loop.run(
                        resolve_interaction_entities(
                            session=session,
                            interaction=interaction,
                            tenant=tenant,
                            insights=dict(interaction.insights or {}),
                            compressed_transcript=compressed,
                        )
                    )
                except Exception as exc:
                    fail_step(
                        session, claim.run_id,
                        error="%s: %s" % (type(exc).__name__, exc),
                    )
                    failed += 1
                    logger.warning(
                        "Orphan reconciliation failed again for interaction %s "
                        "(will retry next sweep)",
                        interaction.id, exc_info=True,
                    )
                    continue

                if resolution.suggestions:
                    meta = dict(getattr(interaction, "insights", None) or {})
                    meta["entity_resolution_suggestions"] = resolution.suggestions
                    interaction.insights = meta
                complete_step(
                    session, claim.run_id,
                    output_digest="customer_id=%s" % (interaction.customer_id,),
                )
                healed += 1
                logger.info(
                    "Orphan reconciliation healed interaction %s (action=%s)",
                    interaction.id, resolution.customer_action,
                )
    finally:
        session.close()
    return {"scanned": scanned, "healed": healed, "failed": failed}


@celery_app.task(
    name="enrich_manager_recommendation",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def enrich_manager_recommendation(self, rec_id: str) -> Dict[str, Any]:
    """Compose the account brief for one freshly inserted recommendation.

    Enqueued by ``recommendation_enrichment.queue_enrichment_for`` right
    after the cohort detectors / daily builder commit. Retries cover
    worker-side blips; LLM-side failures are already absorbed inside
    ``compose_brief`` (the recommendation keeps its deterministic
    rationale, so there is nothing to retry).
    """
    from backend.app.services.recommendation_enrichment import enrich_by_id

    try:
        return _run_async(lambda: enrich_by_id(rec_id))
    except Exception as exc:
        logger.exception(
            "Recommendation enrichment task failed for %s", rec_id
        )
        raise self.retry(exc=exc)


@celery_app.task(name="qbr_overdue_scan")
def qbr_overdue_scan() -> Dict[str, Any]:
    """Daily scan for QBR-overdue customers; fires the ``qbr_overdue``
    notification per eligible account, deduped against unread pings
    inside the dedup window. Best-effort: per-tenant or per-customer
    failures land in the logger and the rest of the run continues.
    """
    from backend.app.models import Notification, Tenant
    from backend.app.services.cs_account_health import (
        find_qbr_overdue_customers,
        should_fire_qbr_overdue,
    )

    session = _get_sync_session()
    notifications_sent = 0
    tenants_processed = 0
    try:
        from backend.app.tenant_ctx import tenant_context

        tenants = session.execute(__import__("sqlalchemy").select(Tenant)).scalars().all()
        for tenant in tenants:
            try:
                with tenant_context(tenant.id, session):
                    candidates = find_qbr_overdue_customers(session, tenant.id)
                    for customer in candidates:
                        if not should_fire_qbr_overdue(session, customer):
                            continue
                        owner_id = customer.strongest_connection_user_id
                        if owner_id is None:
                            continue
                        n = Notification(
                            tenant_id=tenant.id,
                            user_id=owner_id,
                            kind="qbr_overdue",
                            title=f"QBR overdue: {customer.name}",
                            body=(
                                f"No CS interaction in 90+ days. Schedule a "
                                f"check-in with {customer.name}."
                            ),
                            link_url=f"/cs/accounts/{customer.id}",
                        )
                        session.add(n)
                        notifications_sent += 1
                    session.commit()
                tenants_processed += 1
            except Exception:
                logger.exception(
                    "QBR-overdue scan failed for tenant %s (non-fatal)",
                    tenant.id,
                )
                session.rollback()
    finally:
        session.close()
    return {
        "tenants_processed": tenants_processed,
        "notifications_sent": notifications_sent,
    }


# ── Orchestrator Celery tasks ────────────────────────────────────────────


@celery_app.task(name="_orchestrate_one_tenant")
def _orchestrate_one_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant body of the daily orchestrator. Runs in its own worker
    slot so the parent fan-out can parallelize across the fleet.

    Returns a dict the chord callback can reduce — one tenant's failure
    surfaces as ``success=False`` rather than aborting the whole run.
    """
    from backend.app.models import Tenant
    from backend.app.services.orchestrator import get_orchestrator

    session = _get_sync_session()
    try:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return {"tenant_id": tenant_id, "success": False, "error": "missing"}
        orch = get_orchestrator()
        counts: Dict[str, int] = {}
        baseline_refreshed = False
        try:
            counts = orch.run_daily(session, tenant.id) or {}
        except Exception as exc:  # noqa: BLE001 — per-tenant isolation
            logger.exception(
                "Daily orchestrator failed for tenant %s", tenant_id
            )
            return {
                "tenant_id": str(tenant_id),
                "success": False,
                "totals": {},
                "baseline_refreshed": False,
                "error": repr(exc),
            }
        try:
            baseline_refreshed = bool(
                _refresh_paralinguistic_baselines(session, tenant)
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Paralinguistic baseline refresh failed for tenant %s",
                tenant_id,
            )
        return {
            "tenant_id": str(tenant_id),
            "success": True,
            "totals": dict(counts),
            "baseline_refreshed": baseline_refreshed,
        }
    finally:
        session.close()


@celery_app.task(name="_aggregate_orchestration")
def _aggregate_orchestration(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce per-tenant orchestration results back into the original
    aggregate-shape return contract used by ``orchestrator_daily_all_tenants``."""
    totals: Dict[str, int] = {}
    baselines_refreshed = 0
    processed = 0
    failed: List[str] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        if r.get("success"):
            processed += 1
            for k, v in (r.get("totals") or {}).items():
                totals[k] = totals.get(k, 0) + int(v)
            if r.get("baseline_refreshed"):
                baselines_refreshed += 1
        else:
            failed.append(str(r.get("tenant_id") or "unknown"))
    summary = {
        "tenants_processed": processed,
        "profile_updates": totals,
        "paralinguistic_baselines_refreshed": baselines_refreshed,
        "failed_tenants": failed,
    }
    # The dispatcher no longer sync-waits on this chord, so this log line
    # IS the run's aggregate record — keep it structured and loud.
    log = logger.warning if failed else logger.info
    log(
        "orchestrator_daily aggregate: %d tenants processed, %d failed (%s)",
        processed, len(failed), ",".join(failed) or "-",
    )
    return summary


@celery_app.task(name="orchestrator_daily_all_tenants")
def orchestrator_daily_all_tenants() -> Dict[str, Any]:
    """Daily consolidation of delta reports into profile versions.

    Fans out one task per tenant via a Celery chord and returns a
    dispatch receipt immediately (no sync-wait — see the comment at the
    dispatch site). The chord callback ``_aggregate_orchestration``
    reduces the per-tenant results into the aggregate
    (tenants_processed, profile_updates, paralinguistic_baselines_refreshed)
    and logs it — that log line is the run's record.

    Failure semantics: a single tenant's exception never halts the rest —
    it surfaces in the aggregate's ``failed_tenants``.
    """
    from celery import chord, group

    from backend.app.models import Tenant

    session = _get_sync_session()
    try:
        tenant_ids = [str(t.id) for t in session.query(Tenant.id).all()]
    finally:
        session.close()

    if not tenant_ids:
        return {"dispatched_tenants": 0}

    # Dispatch and return immediately. The old version sync-waited here
    # (``.get(timeout=3600)``) — pinning a worker slot for up to 1h and,
    # on timeout, reporting "0 processed / all tenants failed" even when
    # every per-tenant subtask succeeded. The chord callback
    # (``_aggregate_orchestration``) is now the single honest aggregate:
    # it runs only after all subtasks finish and logs real outcomes.
    header = group(_orchestrate_one_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_aggregate_orchestration.s())
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


def _refresh_paralinguistic_baselines(session: Session, tenant: Any) -> bool:
    """Recompute per-tenant acoustic percentiles off the last 90 days of
    interactions and persist them on ``Tenant.paralinguistic_baselines``.

    Returns True when the tenant had enough paralinguistic-enabled
    interactions (≥10) to compute meaningful baselines, False otherwise.
    """
    from backend.app.models import Interaction, InteractionFeatures
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=90)
    rows = (
        session.query(InteractionFeatures.deterministic)
        .join(Interaction, Interaction.id == InteractionFeatures.interaction_id)
        .filter(
            Interaction.tenant_id == tenant.id,
            Interaction.channel == "voice",
            Interaction.created_at >= cutoff,
        )
        .all()
    )
    customer_db: List[float] = []
    agent_pitch_std: List[float] = []
    for (det,) in rows:
        block = ((det or {}).get("paralinguistic") or {})
        if not block or not block.get("available"):
            continue
        per_speaker = block.get("per_speaker") or {}
        agent = per_speaker.get("agent") or next(iter(per_speaker.values()), {}) or {}
        customer = per_speaker.get("customer")
        if customer is None and len(per_speaker) > 1:
            customer = list(per_speaker.values())[1]
        customer = customer or {}
        if (ps := agent.get("pitch_std_semitones")) is not None:
            agent_pitch_std.append(float(ps))
        if (cd := customer.get("intensity_db_p50")) is not None:
            customer_db.append(float(cd))

    if len(customer_db) < 10 and len(agent_pitch_std) < 10:
        return False

    def _pctile(values: List[float], p: float) -> Optional[float]:
        if not values:
            return None
        clean = sorted(values)
        idx = p * (len(clean) - 1)
        lo = int(idx)
        hi = min(len(clean) - 1, lo + 1)
        frac = idx - lo
        return round(clean[lo] + (clean[hi] - clean[lo]) * frac, 3)

    baselines = {
        "customer_intensity_db_p90": _pctile(customer_db, 0.9),
        "customer_intensity_db_p50": _pctile(customer_db, 0.5),
        "agent_pitch_std_semitones_p50": _pctile(agent_pitch_std, 0.5),
        "sample_counts": {
            "customer_intensity": len(customer_db),
            "agent_pitch_std": len(agent_pitch_std),
        },
        "computed_at": datetime.utcnow().isoformat(),
    }
    tenant.paralinguistic_baselines = baselines
    session.commit()
    return True


@celery_app.task(name="_orchestrate_one_tenant_weekly")
def _orchestrate_one_tenant_weekly(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant weekly orchestration. Returns a small status dict so
    the chord callback can aggregate counts without serialising the full
    orchestrator output back through Redis."""
    from backend.app.services.orchestrator import get_orchestrator

    session = _get_sync_session()
    try:
        orch = get_orchestrator()
        try:
            orch.run_weekly(session, tenant_id)
            return {"tenant_id": tenant_id, "success": True}
        except Exception:
            logger.exception(
                "Weekly orchestrator failed for tenant %s", tenant_id
            )
            return {"tenant_id": tenant_id, "success": False}
    finally:
        session.close()


@celery_app.task(name="_aggregate_orchestration_weekly")
def _aggregate_orchestration_weekly(per_tenant: List[Dict[str, Any]]) -> Dict[str, Any]:
    processed = 0
    failed: List[str] = []
    for r in per_tenant:
        if not isinstance(r, dict):
            continue
        if r.get("success"):
            processed += 1
        else:
            failed.append(str(r.get("tenant_id") or "unknown"))
    log = logger.warning if failed else logger.info
    log(
        "orchestrator_weekly aggregate: %d tenants processed, %d failed (%s)",
        processed, len(failed), ",".join(failed) or "-",
    )
    return {"tenants_processed": processed, "failed_tenants": failed}


@celery_app.task(name="orchestrator_weekly_all_tenants")
def orchestrator_weekly_all_tenants() -> Dict[str, Any]:
    """Weekly self-improvement reflection across all tenants.

    Was a sequential loop — a single slow tenant blocked every following
    tenant, and a 50-tenant run could stretch past 4 h. Now fans out
    via a Celery chord so per-tenant work parallelises across workers,
    with the callback aggregating into the small status dict the beat
    consumer expects.
    """
    from celery import chord, group

    from backend.app.models import Tenant

    session = _get_sync_session()
    try:
        tenant_ids = [str(t.id) for t in session.query(Tenant.id).all()]
    finally:
        session.close()

    if not tenant_ids:
        return {"dispatched_tenants": 0}

    # Non-blocking dispatch — see the daily orchestrator's comment.
    header = group(_orchestrate_one_tenant_weekly.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_aggregate_orchestration_weekly.s())
    return {"dispatched_tenants": len(tenant_ids), "chord_id": str(async_result.id)}


# ── Outcomes backfill & calibration ──────────────────────────────────────


@celery_app.task(name="outcomes_backfill_all_tenants")
def outcomes_backfill_all_tenants() -> Dict[str, Any]:
    """Backfill proxy outcomes from internal signals across all tenants."""
    from backend.app.models import Tenant
    from backend.app.services.outcomes_backfill import run_all

    session = _get_sync_session()
    totals: Dict[str, int] = {}
    tenants_done = 0
    try:
        from backend.app.tenant_ctx import tenant_context

        for tenant in session.query(Tenant).all():
            try:
                with tenant_context(tenant.id, session):
                    counts = run_all(session, tenant.id)
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
                tenants_done += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Outcome backfill failed for tenant %s", tenant.id
                )
    finally:
        session.close()
    return {"tenants_processed": tenants_done, "writes": totals}


@celery_app.task(name="calibration_fit_all_tenants")
def calibration_fit_all_tenants() -> Dict[str, Any]:
    """Refit Platt scaling for every configured scorer, per tenant."""
    from backend.app.models import Tenant
    from backend.app.services.calibration import fit_all_scorers

    session = _get_sync_session()
    activated = 0
    skipped = 0
    try:
        from backend.app.tenant_ctx import tenant_context

        for tenant in session.query(Tenant).all():
            try:
                with tenant_context(tenant.id, session):
                    results = fit_all_scorers(session, tenant.id)
                for r in results:
                    if r.activated:
                        activated += 1
                    else:
                        skipped += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Calibration failed for tenant %s", tenant.id
                )
    finally:
        session.close()
    return {"activated": activated, "skipped": skipped}


@celery_app.task(name="irt_fit_all_tenants")
def irt_fit_all_tenants() -> Dict[str, Any]:
    """Weekly IRT fit across every tenant's scorecard templates."""
    from backend.app.models import Tenant
    from backend.app.services.irt import fit_all_templates_for_tenant

    session = _get_sync_session()
    summary: Dict[str, int] = {"templates_fit": 0, "items_fit": 0, "items_retired": 0}
    try:
        from backend.app.tenant_ctx import tenant_context

        for tenant in session.query(Tenant).all():
            try:
                with tenant_context(tenant.id, session):
                    results = fit_all_templates_for_tenant(session, tenant.id)
                summary["templates_fit"] += len(results)
                for r in results:
                    summary["items_fit"] += r.n_items_fitted
                    summary["items_retired"] += len(r.retired_items)
            except Exception:  # noqa: BLE001
                logger.exception("IRT fit failed for tenant %s", tenant.id)
    finally:
        session.close()
    return summary


@celery_app.task(name="churn_train_all_tenants")
def churn_train_all_tenants() -> Dict[str, Any]:
    """Weekly Cox churn-model training; silently no-ops when data is thin."""
    from backend.app.models import Tenant
    from backend.app.services.churn_model import train_for_tenant

    session = _get_sync_session()
    summary = {"trained": 0, "insufficient_data": 0}
    try:
        from backend.app.tenant_ctx import tenant_context

        for tenant in session.query(Tenant).all():
            try:
                with tenant_context(tenant.id, session):
                    result = train_for_tenant(session, tenant.id)
                if result.status == "ok":
                    summary["trained"] += 1
                else:
                    summary["insufficient_data"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("Churn training failed for tenant %s", tenant.id)
    finally:
        session.close()
    return summary
# ── Continuous AI improvement tasks ──────────────────────────────────────


@celery_app.task(name="consume_feedback_stream")
def consume_feedback_stream() -> Dict[str, Any]:
    """Drain the Redis feedback stream into ``feedback_events``.

    Idempotent and safe to run on a 30s cadence.  Returns number of events
    persisted in this batch.
    """
    from backend.app.services import feedback_service

    session = _get_sync_session()
    try:
        return feedback_service.consume_batch(session)
    finally:
        session.close()


@celery_app.task(name="evaluate_analysis", bind=True, max_retries=3)
def evaluate_analysis(self, interaction_id: str) -> Dict[str, Any]:
    """LLM-judge the analysis insights for an interaction.  Chained 15-min after the producer."""
    from backend.app.services.llm_judge import evaluate_analysis as run

    session = _get_sync_session()
    try:
        return run(session, interaction_id)
    except Exception as exc:
        logger.exception("evaluate_analysis failed for %s", interaction_id)
        raise self.retry(exc=exc, countdown=300)
    finally:
        session.close()


@celery_app.task(name="evaluate_classification", bind=True, max_retries=3)
def evaluate_classification(self, interaction_id: str) -> Dict[str, Any]:
    """LLM-judge an email classification verdict."""
    from backend.app.services.llm_judge import evaluate_classification as run

    session = _get_sync_session()
    try:
        return run(session, interaction_id)
    except Exception as exc:
        logger.exception("evaluate_classification failed for %s", interaction_id)
        raise self.retry(exc=exc, countdown=300)
    finally:
        session.close()


@celery_app.task(name="evaluate_reply", bind=True, max_retries=3)
def evaluate_reply(self, interaction_id: str) -> Dict[str, Any]:
    """LLM-judge an outbound email reply (5 LLM dimensions; edit-distance is sync)."""
    from backend.app.services.llm_judge import evaluate_reply as run

    session = _get_sync_session()
    try:
        return run(session, interaction_id)
    except Exception as exc:
        logger.exception("evaluate_reply failed for %s", interaction_id)
        raise self.retry(exc=exc, countdown=300)
    finally:
        session.close()


@celery_app.task(name="refresh_few_shot_pools")
def refresh_few_shot_pools() -> Dict[str, Any]:
    """Promote high-quality interactions into each tenant's few-shot pool."""
    from backend.app.services.personalization_service import refresh_pools_all_tenants

    session = _get_sync_session()
    try:
        return refresh_pools_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="compute_wer_weekly")
def compute_wer_weekly() -> Dict[str, Any]:
    """Aggregate the prior 7 days of transcript_corrections into wer_metrics."""
    from backend.app.services.wer_service import compute_weekly

    session = _get_sync_session()
    try:
        return compute_weekly(session)
    finally:
        session.close()


@celery_app.task(name="discover_vocabulary_candidates")
def discover_vocabulary_candidates() -> Dict[str, Any]:
    """Surface new candidate keyterms from corrections + low-confidence segments."""
    from backend.app.services.vocabulary_service import discover_candidates_all_tenants

    session = _get_sync_session()
    try:
        return discover_candidates_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="cross_tenant_aggregate_metrics")
def cross_tenant_aggregate_metrics() -> Dict[str, Any]:
    """Compute opt-in cross-tenant aggregates (no tenant_id leakage)."""
    from backend.app.services.cross_tenant_metrics import aggregate_weekly

    session = _get_sync_session()
    try:
        return aggregate_weekly(session)
    finally:
        session.close()


@celery_app.task(name="quality_regression_check")
def quality_regression_check() -> Dict[str, Any]:
    """Watchdog: alert if 24h rolling quality drops > 5% vs. 7-day baseline."""
    from backend.app.services.regression_watchdog import check_all_active_rollouts

    session = _get_sync_session()
    try:
        return check_all_active_rollouts(session)
    finally:
        session.close()


@celery_app.task(name="variant_winner_selection")
def variant_winner_selection() -> Dict[str, Any]:
    """Promote / retire prompt variants based on accumulated quality scores."""
    from backend.app.services.variant_rollout import evaluate_active_experiments

    session = _get_sync_session()
    try:
        return evaluate_active_experiments(session)
    finally:
        session.close()


@celery_app.task(name="vocabulary_digest_weekly")
def vocabulary_digest_weekly() -> Dict[str, Any]:
    """Send the weekly Slack digest of pending vocabulary candidates."""
    from backend.app.services.digest_service import send_vocabulary_digests

    session = _get_sync_session()
    try:
        return send_vocabulary_digests(session)
    finally:
        session.close()


@celery_app.task(name="campaign_variant_winner_selection")
def campaign_variant_winner_selection() -> Dict[str, Any]:
    """Decide winners for active campaign A/B variants using engagement events."""
    from backend.app.services.campaign_winner_service import decide_active_campaigns

    session = _get_sync_session()
    try:
        return decide_active_campaigns(session)
    finally:
        session.close()


@celery_app.task(name="audio_retention_sweep")
def audio_retention_sweep() -> Dict[str, Any]:
    """Delete audio objects past their tenant's retention window.

    Tenant-agnostic — we scan the bucket and rely on the per-object
    ``retention_hours`` + ``stored_at`` tags set at upload time.  This
    means we honor per-tenant overrides even when the Tenant row's
    ``audio_retention_hours`` has changed since upload.
    """
    from backend.app.services.audio_storage import get_audio_store

    try:
        deleted = get_audio_store().sweep_expired()
        return {"deleted": deleted}
    except Exception:  # noqa: BLE001
        logger.exception("audio_retention_sweep failed")
        return {"deleted": 0, "error": True}


@celery_app.task(name="rebuild_tenant_context")
def rebuild_tenant_context(tenant_id: str, full: bool = False) -> Dict[str, Any]:
    """Rebuild LINDA's per-tenant company-context brief from the KB.

    Debounced via a Redis token key (see ``schedule_context_rebuild``) so a
    rapid flurry of KB uploads collapses into a single rebuild. If ``full`` is
    True, the builder streams every doc; otherwise it does an incremental
    merge on the most recent doc (populated in Redis by the caller).
    """
    from sqlalchemy import select as _select

    from backend.app.db import async_session
    from backend.app.models import KBDocument
    from backend.app.services.kb.context_builder import ContextBuilderService
    from backend.app.services.kb.context_dispatch import claim_debounce

    async def _runner() -> Dict[str, Any]:
        tid = uuid.UUID(tenant_id)

        # Honor the debounce: if someone bumped the timer forward while we
        # were asleep in the Celery queue, bail out — a fresh task is
        # already scheduled.
        if not full and not await claim_debounce(tid):
            return {"tenant_id": tenant_id, "skipped": "debounced"}

        builder = ContextBuilderService()
        async with async_session() as db:
            if full:
                brief = await builder.rebuild_all(db, tid)
                return {"tenant_id": tenant_id, "mode": "full", "brief_keys": list(brief.keys())}

            # Incremental: pick up the most recently updated doc for this
            # tenant and merge it in. A burst of uploads coalesces into this
            # single merge because the debounce key only fires once.
            stmt = (
                _select(KBDocument)
                .where(KBDocument.tenant_id == tid)
                .order_by(KBDocument.created_at.desc())
                .limit(1)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return {"tenant_id": tenant_id, "mode": "incremental", "skipped": "no_docs"}
            brief = await builder.merge_document(db, tid, row)
            return {"tenant_id": tenant_id, "mode": "incremental", "brief_keys": list(brief.keys())}

    return _run_async(_runner)


@celery_app.task(name="rebuild_customer_brief")
def rebuild_customer_brief(tenant_id: str, customer_id: str) -> Dict[str, Any]:
    """Rebuild one customer's brief (debounced via Redis). Fired on
    interaction close, outcome log, and admin demand."""
    from backend.app.db import async_session
    from backend.app.services.kb.context_dispatch import claim_customer_debounce
    from backend.app.services.kb.customer_brief_builder import CustomerBriefBuilder

    async def _runner() -> Dict[str, Any]:
        cid = uuid.UUID(customer_id)
        if not await claim_customer_debounce(cid):
            return {"customer_id": customer_id, "skipped": "debounced"}
        builder = CustomerBriefBuilder()
        async with async_session() as db:
            brief = await builder.build(db, uuid.UUID(tenant_id), cid)
            return {
                "customer_id": customer_id,
                "status": brief.get("current_status"),
                "source_interaction_count": brief.get("source_interaction_count"),
            }

    return _run_async(_runner)


@celery_app.task(name="tenant_brief_refiner_weekly")
def tenant_brief_refiner_weekly(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Run the TenantBriefRefiner for one tenant (if tenant_id given) or all
    tenants. Invoked by Celery beat once a week, and also as a fan-out from
    admin-triggered refines."""
    from backend.app.db import async_session
    from backend.app.models import Tenant as _Tenant
    from backend.app.services.kb.tenant_brief_refiner import TenantBriefRefiner
    from sqlalchemy import select as _select

    async def _runner() -> Dict[str, Any]:
        refiner = TenantBriefRefiner()
        async with async_session() as db:
            if tenant_id:
                tids = [uuid.UUID(tenant_id)]
            else:
                rows = await db.execute(_select(_Tenant.id))
                tids = [uuid.UUID(str(r[0])) for r in rows.all()]

            results: List[Dict[str, Any]] = []
            for tid in tids:
                try:
                    pb = await refiner.refine(db, tid)
                    results.append({"tenant_id": str(tid), "sample_size": pb.get("sample_size")})
                except Exception:
                    logger.exception("TenantBriefRefiner failed for tenant %s", tid)
                    results.append({"tenant_id": str(tid), "error": True})
        return {"tenants_processed": len(results), "results": results}

    return _run_async(_runner)


@celery_app.task(name="infer_from_sources_weekly")
def infer_from_sources_weekly(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Run the Infer-From-Sources agent for one tenant or all tenants.

    Emits TenantBriefSuggestion rows for the tenant admin to review.
    Never auto-writes to the tenant brief.
    """
    from backend.app.db import async_session
    from backend.app.models import Tenant as _Tenant
    from backend.app.services.kb.infer_from_sources import InferFromSources
    from sqlalchemy import select as _select

    async def _runner() -> Dict[str, Any]:
        agent = InferFromSources()
        async with async_session() as db:
            if tenant_id:
                tids = [uuid.UUID(tenant_id)]
            else:
                rows = await db.execute(_select(_Tenant.id))
                tids = [uuid.UUID(str(r[0])) for r in rows.all()]

            results: List[Dict[str, Any]] = []
            for tid in tids:
                try:
                    new_rows = await agent.run(db, tid)
                    results.append(
                        {
                            "tenant_id": str(tid),
                            "new_suggestions": len(new_rows),
                        }
                    )
                except Exception:
                    logger.exception(
                        "InferFromSources failed for tenant %s", tid
                    )
                    results.append({"tenant_id": str(tid), "error": True})
            return {"tenants_processed": len(results), "results": results}

    return _run_async(_runner)


@celery_app.task(name="vector_health_daily")
def vector_health_daily() -> Dict[str, Any]:
    """Daily sustained-threshold check for the vector store.

    Uses the async engine via ``asyncio.run`` since the check touches Redis
    async APIs and is cheap to boot a loop for.
    """
    from backend.app.db import async_session
    from backend.app.services.kb.vector_health_check import run_vector_health_check

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            return await run_vector_health_check(db)

    return _run_async(_runner)


@celery_app.task(name="crm_writeback", max_retries=2)
def crm_writeback(interaction_id: str) -> Dict[str, Any]:
    """Apply CRM write-backs (notes, activities) for one interaction.

    Dispatched after the voice pipeline lands insights. Reads
    ``Tenant.features_enabled`` to decide which kinds of write-back to
    attempt; the tenant stays in control. Exceptions are swallowed by
    the write-back service so a CRM outage never poisons this task.
    """
    from backend.app.db import async_session
    from backend.app.services.crm.writeback import write_back_interaction

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            summary = await write_back_interaction(db, uuid.UUID(interaction_id))
            await db.commit()
            return summary

    return _run_async(_runner)


@celery_app.task(name="crm_sync_tenant")
def crm_sync_tenant(tenant_id: str, provider: str) -> Dict[str, Any]:
    """Run a single CRM sync for ``(tenant_id, provider)``."""
    from backend.app.db import async_session
    from backend.app.services.crm.sync_service import sync_crm_for_tenant

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            summary = await sync_crm_for_tenant(
                db, uuid.UUID(tenant_id), provider
            )
            return {
                "provider": summary.provider,
                "status": summary.status,
                "customers_upserted": summary.customers_upserted,
                "contacts_upserted": summary.contacts_upserted,
                "briefs_rebuilt": summary.briefs_rebuilt,
                "error": summary.error,
            }

    return _run_async(_runner)


@celery_app.task(name="crm_sync_daily")
def crm_sync_daily() -> Dict[str, Any]:
    """Nightly fan-out: for every Integration on a CRM provider, run a sync.

    Tenants without CRM integrations are silently skipped. A provider that
    returns ``not implemented`` (e.g. the Pipedrive stub) is counted as
    skipped rather than failed.
    """
    from sqlalchemy import select as _select

    from backend.app.db import async_session
    from backend.app.models import Integration
    from backend.app.services.crm.sync_service import (
        SUPPORTED_PROVIDERS,
        sync_crm_for_tenant,
    )

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            stmt = _select(
                Integration.tenant_id, Integration.provider
            ).where(Integration.provider.in_(list(SUPPORTED_PROVIDERS)))
            rows = await db.execute(stmt)
            pairs = {
                (uuid.UUID(str(t)), p) for (t, p) in rows.all()
            }

            from backend.app.tenant_ctx import tenant_context_async

            results: List[Dict[str, Any]] = []
            for tenant_id, provider in pairs:
                try:
                    async with tenant_context_async(tenant_id, db):
                        summary = await sync_crm_for_tenant(db, tenant_id, provider)
                    results.append(
                        {
                            "tenant_id": str(tenant_id),
                            "provider": provider,
                            "status": summary.status,
                            "customers": summary.customers_upserted,
                            "contacts": summary.contacts_upserted,
                        }
                    )
                except Exception:
                    logger.exception(
                        "CRM sync failed for tenant=%s provider=%s",
                        tenant_id,
                        provider,
                    )
                    results.append(
                        {
                            "tenant_id": str(tenant_id),
                            "provider": provider,
                            "status": "failed",
                        }
                    )
        return {"runs": results, "count": len(results)}

    return _run_async(_runner)


@celery_app.task(name="tenant_export_to_s3", max_retries=1)
def tenant_export_to_s3(
    tenant_id: str,
    s3_key_prefix: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce an NDJSON GDPR export for ``tenant_id`` and upload it to
    the tenant-owned S3 bucket.

    This is the non-interactive companion to ``GET /tenants/{id}/export``
    — suitable for scheduled backups or an admin-triggered async
    export the UI polls for. Writes to
    ``{AWS_S3_BUCKET}/{s3_key_prefix}/{timestamp}.ndjson.gz`` by default.

    The bundle is gzipped on the fly so terabyte-scale tenants don't
    pay 10x storage for JSON boilerplate. Metadata (row counts per
    table, schema version) lands in the accompanying ``.meta.json``.
    """
    import gzip
    import io
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    from backend.app.db import async_session
    from backend.app.services import s3_audio
    from backend.app.services.tenant_dataops import export_tenant

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            buf = io.BytesIO()
            line_count = 0
            tenant_uuid = _uuid.UUID(tenant_id)
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                async for chunk in export_tenant(db, tenant_uuid):
                    gz.write(chunk)
                    line_count += 1
            body = buf.getvalue()

        prefix = (s3_key_prefix or "backups").strip("/")
        timestamp = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
        key = f"{prefix}/{tenant_id}/{timestamp}.ndjson.gz"
        s3_audio.upload_bytes(
            tenant_id=tenant_uuid,
            recording_id=_uuid.UUID(tenant_id),
            data=body,
            content_type="application/gzip",
            s3_key_override=key,
        )
        logger.info(
            "Tenant export uploaded: tenant=%s key=%s bytes=%d lines=%d",
            tenant_id, key, len(body), line_count,
        )
        return {
            "tenant_id": tenant_id,
            "s3_key": key,
            "bytes": len(body),
            "lines": line_count,
            "reason": reason,
            "exported_at": timestamp,
        }

    return _run_async(_runner)


@celery_app.task(name="tenant_backup_all_tenants")
def tenant_backup_all_tenants() -> Dict[str, Any]:
    """Nightly backup fan-out — one export per tenant.

    Dispatches ``tenant_export_to_s3`` per tenant so one slow export
    doesn't hold up the others. Beat schedule below wires this to run
    daily; disable per-tenant by setting
    ``tenants.features_enabled['scheduled_backups'] = False``.
    """
    from backend.app.models import Tenant

    session = _get_sync_session()
    try:
        dispatched = 0
        skipped = 0
        for tenant in session.query(Tenant).all():
            feats = (tenant.features_enabled or {})
            if feats.get("scheduled_backups") is False:
                skipped += 1
                continue
            try:
                tenant_export_to_s3.delay(str(tenant.id), reason="nightly_backup")
                dispatched += 1
            except Exception:
                logger.exception(
                    "Failed to dispatch backup for tenant %s", tenant.id
                )
        return {"dispatched": dispatched, "skipped": skipped}
    finally:
        session.close()


@celery_app.task(name="tenant_restore_from_s3")
def tenant_restore_from_s3(s3_key: str) -> Dict[str, Any]:
    """Restore a tenant export back into the database.

    Reads the NDJSON.gz bundle from S3, walks it line by line, and
    upserts every row into its destination table. Intended for two
    scenarios:

    1. **Disaster recovery** — a tenant's data got corrupted and we
       need yesterday's backup back.
    2. **Environment sync** — copy a tenant from prod to staging for
       repro work (make sure PII redaction ran before the snapshot).

    The operation is idempotent on the primary key (``ON CONFLICT DO
    UPDATE``), so running it twice is safe. Foreign-key ordering is
    honored because ``_tenant_tables_reverse_topo`` emits parents
    first during export — the restore walks the file in order.
    """
    import gzip
    import json as _json

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from backend.app.db import async_session
    from backend.app.models import Base
    from backend.app.services import s3_audio

    async def _runner() -> Dict[str, Any]:
        blob = s3_audio.download_object_bytes(s3_key)  # type: ignore[attr-defined]
        data = gzip.decompress(blob).decode("utf-8")

        per_table_counts: Dict[str, int] = {}
        async with async_session() as db:
            for line in data.splitlines():
                if not line.strip():
                    continue
                try:
                    doc = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if doc.get("_meta") or doc.get("_eof"):
                    continue
                table_name = doc.get("_table")
                row = doc.get("row") or {}
                if not table_name or not row:
                    continue
                table = Base.metadata.tables.get(table_name)
                if table is None:
                    logger.warning("restore: skipping unknown table %s", table_name)
                    continue
                # Unwrap base64 blobs produced during export.
                row = {
                    k: (
                        __import__("base64").b64decode(v["__b64__"])
                        if isinstance(v, dict) and "__b64__" in v
                        else v
                    )
                    for k, v in row.items()
                }
                # Only populate columns the table actually has — a
                # restore from an older schema shouldn't fail on a
                # column that was dropped since.
                allowed = {c.name for c in table.columns}
                row = {k: v for k, v in row.items() if k in allowed}
                # RLS: exports are single-tenant bundles; bind the tenant
                # from the first row that carries one so every INSERT's
                # WITH CHECK evaluates under that tenant.
                if row.get("tenant_id"):
                    from backend.app.tenant_ctx import bind_tenant_async

                    await bind_tenant_async(db, row["tenant_id"])
                stmt = pg_insert(table).values(**row)
                pk_cols = [c.name for c in table.primary_key]
                if pk_cols:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=pk_cols,
                        set_={
                            k: row[k] for k in row.keys() if k not in pk_cols
                        },
                    )
                await db.execute(stmt)
                per_table_counts[table_name] = (
                    per_table_counts.get(table_name, 0) + 1
                )
            await db.commit()
        return {"s3_key": s3_key, "restored": per_table_counts}

    return _run_async(_runner)


@celery_app.task(name="sync_knowledge_base")
def sync_knowledge_base(tenant_id: str, source_type: str) -> Dict[str, Any]:
    """Run one KB provider sync for a tenant.

    Dispatched by ``POST /kb/sync/{provider}`` and by the nightly
    scheduler (when we add one). Returns a summary the dispatcher can
    log / surface in the admin UI.
    """
    from backend.app.db import async_session
    from backend.app.services.kb.sync_runner import sync_kb_for_tenant

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            summary = await sync_kb_for_tenant(
                db, uuid.UUID(tenant_id), source_type
            )
            await db.commit()
            return {
                "source_type": summary.source_type,
                "status": summary.status,
                "docs_seen": summary.docs_seen,
                "docs_upserted": summary.docs_upserted,
                "chunks_written": summary.chunks_written,
                "error": summary.error,
            }

    return _run_async(_runner)


@celery_app.task(name="webhook_deliver")
def webhook_deliver(delivery_id: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Attempt one HTTP delivery for a WebhookDelivery row.

    The dispatcher re-enqueues retries via ``apply_async(countdown=...)``
    when it schedules the next attempt, so this task stays stateless.
    Tolerates the delivery row being gone (e.g., webhook deleted in the
    meantime) by returning status=missing.

    Two hard-won constraints:

    * The session MUST commit — ``deliver_one`` mutates the row (status,
      attempts) but owns no transaction. Without the commit every
      delivery outcome rolled back, so rows sat in ``pending`` with
      attempt_count=0 no matter what happened on the wire.
    * ``tenant_id`` should be passed explicitly. The prerun hook can
      resolve it from ``delivery_id`` via the SECURITY DEFINER bootstrap,
      but only once the row is committed — a task racing the caller's
      commit resolves nothing and no-ops as "missing". The explicit arg
      plus the dispatcher's enqueue countdown close that race; the arg
      stays Optional so tasks enqueued by a pre-upgrade dispatcher can
      still be consumed during a rolling deploy.
    """
    from backend.app.db import async_session
    from backend.app.services.webhook_dispatcher import deliver_one
    from backend.app.tenant_ctx import tenant_context_async

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            if tenant_id:
                async with tenant_context_async(tenant_id, db):
                    result = await deliver_one(db, uuid.UUID(delivery_id))
                    await db.commit()
                    return result
            # Legacy enqueue without tenant — only works pre-RLS.
            result = await deliver_one(db, uuid.UUID(delivery_id))
            await db.commit()
            return result

    return _run_async(_runner)


# Sweep tuning. MIN_AGE must comfortably exceed the dispatcher's enqueue
# countdown so the sweep never races a delivery that's simply on its way.
_SWEEP_MIN_AGE_S = 300
_SWEEP_DEAD_LETTER_AGE_S = 48 * 3600
_SWEEP_BATCH_LIMIT = 500


@celery_app.task(name="webhook_pending_sweep")
def webhook_pending_sweep() -> Dict[str, Any]:
    """Re-enqueue orphaned webhook deliveries; dead-letter stale ones.

    The dispatcher enqueues ``webhook_deliver`` right after flushing the
    delivery row, so a task can fire before the caller's transaction
    commits (row invisible → "missing"), and a worker restart can drop a
    scheduled retry. Both leave rows parked in ``pending`` forever — this
    sweep is the safety net the dispatcher comments always promised.

    Per pending row (walked per-tenant because webhook_deliveries is
    RLS-scoped):

    * older than ``_SWEEP_DEAD_LETTER_AGE`` → dead_letter. Delivering a
      days-old event would push stale business data to consumers (the
      exact rows stuck on prod include pre-fix payloads that must never
      go out).
    * never attempted and older than ``_SWEEP_MIN_AGE``, or a scheduled
      retry overdue by ``_SWEEP_MIN_AGE`` → re-enqueue ``webhook_deliver``.

    Runs every 10 minutes via beat; each pass is idempotent.
    """
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from backend.app.models import WebhookDelivery
    from backend.app.tenant_ctx import tenant_context

    now = datetime.now(_tz.utc)
    min_age_cutoff = now - _td(seconds=_SWEEP_MIN_AGE_S)
    dead_letter_cutoff = now - _td(seconds=_SWEEP_DEAD_LETTER_AGE_S)

    def _aware(dt: Optional[datetime]) -> Optional[datetime]:
        # Postgres returns tz-aware datetimes; sqlite (tests) naive UTC.
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=_tz.utc)
        return dt

    requeued = 0
    dead_lettered = 0
    # One session for the whole sweep (not one per tenant — 45 session
    # opens per beat tick was pure churn), and a cheap index-only EXISTS
    # probe per tenant BEFORE loading rows: almost every tenant has no
    # pending deliveries, and loading full rows (payload JSONB included)
    # for all of them every 10 minutes is what Sentry flagged as an N+1
    # on this transaction. RLS keeps the per-tenant loop itself — the
    # GUC scopes each query, so a single cross-tenant SELECT would see
    # zero rows under the app role.
    session = _get_sync_session()
    try:
        for tid in _all_tenant_ids():
            try:
                with tenant_context(uuid.UUID(tid), session):
                    has_pending = session.query(
                        session.query(WebhookDelivery.id)
                        .filter(
                            WebhookDelivery.tenant_id == uuid.UUID(tid),
                            WebhookDelivery.status == "pending",
                        )
                        .exists()
                    ).scalar()
                    if not has_pending:
                        # End the read-only transaction so the next
                        # tenant's context re-arms the GUC cleanly.
                        session.rollback()
                        continue
                    rows = (
                        session.query(WebhookDelivery)
                        .filter(
                            WebhookDelivery.tenant_id == uuid.UUID(tid),
                            WebhookDelivery.status == "pending",
                        )
                        .limit(_SWEEP_BATCH_LIMIT)
                        .all()
                    )
                    for d in rows:
                        created_at = _aware(d.created_at)
                        next_retry_at = _aware(d.next_retry_at)
                        if created_at is not None and created_at < dead_letter_cutoff:
                            d.status = "dead_letter"
                            d.last_error = (
                                "webhook_pending_sweep: stale — expired before "
                                "first successful delivery"
                            )
                            dead_lettered += 1
                            continue
                        never_attempted = (
                            (d.attempt_count or 0) == 0
                            and created_at is not None
                            and created_at < min_age_cutoff
                        )
                        retry_overdue = (
                            next_retry_at is not None
                            and next_retry_at < min_age_cutoff
                        )
                        if never_attempted or retry_overdue:
                            webhook_deliver.delay(str(d.id), tid)
                            requeued += 1
                    session.commit()
            except Exception:
                logger.exception("webhook_pending_sweep failed for tenant %s", tid)
                session.rollback()
    finally:
        session.close()

    result = {"requeued": requeued, "dead_lettered": dead_lettered}
    if requeued or dead_lettered:
        logger.info("webhook_pending_sweep: %s", result)
    return result


# Celery queues we sample for backpressure. Default queue is ``celery``;
# add new queue names here when task routing is introduced. Do NOT scan
# the keyspace — on per-command-billed Redis (Upstash) a SCAN + TYPE on
# every key every 30 s dominates the bill.
_SAMPLED_CELERY_QUEUES: tuple[str, ...] = ("priority", "default", "batch")


@celery_app.task(name="sample_queue_depth")
def sample_queue_depth() -> Dict[str, int]:
    """Read each Celery queue's Redis LIST length into the
    ``linda_celery_queue_depth`` gauge.

    One LLEN per known queue every 30 s.
    """
    try:
        import redis

        from backend.app.services.metrics import CELERY_QUEUE_DEPTH

        redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        depths: Dict[str, int] = {}
        pipe = redis_client.pipeline(transaction=False)
        for queue in _SAMPLED_CELERY_QUEUES:
            pipe.llen(queue)
        results = pipe.execute()
        for queue, length in zip(_SAMPLED_CELERY_QUEUES, results):
            length_int = int(length or 0)
            depths[queue] = length_int
            CELERY_QUEUE_DEPTH.labels(queue=queue).set(length_int)
        return depths
    except Exception:
        logger.debug("queue depth sampling failed", exc_info=True)
        return {}


@celery_app.task(name="event_retention_sweep")
def event_retention_sweep() -> Dict[str, Any]:
    """Daily retention sweep for high-volume event tables.

    Drops ``webhook_deliveries`` older than 90 days (sent / dead-letter
    only — pending retries are preserved regardless of age). Rolls
    ``feedback_events`` older than 180 days into
    ``feedback_daily_rollup`` and deletes the raw rows so calibration
    can still see historical volume without paying raw-row storage.
    """
    from backend.app.db import async_session
    from backend.app.services.event_retention import run_event_retention_sweep

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            return await run_event_retention_sweep(db)

    return _run_async(_runner)


@celery_app.task(name="renew_teams_subscriptions")
def renew_teams_subscriptions() -> Dict[str, Any]:
    """Renew Microsoft Graph subscriptions nearing expiry.

    Graph's shortest-lived subscribed resource
    (``onlineMeetings/getAllRecordings``) caps at ~60 minutes, so beat
    runs this every 15 minutes against the sweep's default
    ``within_minutes=15`` slack. Microsoft sends no retry/alert when a
    subscription lapses — without this sweep, Teams compliance
    recordings silently stop arriving.
    """
    from backend.app.db import async_session
    from backend.app.services.teams_recording.teams_graph import (
        renew_due_teams_subscriptions,
    )

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            return await renew_due_teams_subscriptions(db)

    return _run_async(_runner)


@celery_app.task(name="trial_expiry_daily")
def trial_expiry_daily() -> Dict[str, Any]:
    """Walk every sandbox tenant and act on the trial-end timeline.

    For each tenant on the ``sandbox`` tier with a non-NULL
    ``trial_ends_at``:

    * 3 / 1 days before ``trial_ends_at``: emit an "approaching" notice
      (logged + recorded in ``tenant_dataops_log``; if/when an email
      provider is wired the same code can switch to that transport).
    * On / past ``trial_ends_at``: emit an "expired" notice and flip
      ``Tenant.subscription_status`` to ``expired``. The
      ``require_active_subscription`` dependency already 402s
      revenue-burning endpoints once trial_ends_at is in the past, but
      the explicit status flag drives banners + reports.

    Idempotent: a tenant already at ``subscription_status="expired"``
    is skipped. Notices for any one (tenant, day-bucket) tuple are
    written through a unique reason key in tenant_dataops_log so a
    re-run of the same day doesn't double-log.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select

    from backend.app.db import async_session
    from backend.app.models import Tenant, TenantDataOpsLog
    from backend.app.tenant_ctx import tenant_context_async

    async def _runner() -> Dict[str, Any]:
        emitted = {"warned_3d": 0, "warned_1d": 0, "expired": 0}
        now = datetime.now(timezone.utc)

        async with async_session() as db:
            stmt = select(Tenant).where(
                Tenant.plan_tier == "sandbox",
                Tenant.trial_ends_at.is_not(None),
            )
            tenants = list((await db.execute(stmt)).scalars().all())

            for tenant in tenants:
                async with tenant_context_async(tenant.id, db):
                    ends = tenant.trial_ends_at
                    if ends is None:
                        continue
                    seconds_left = (ends - now).total_seconds()
                    days_left = seconds_left / 86_400

                    # Pick the first matching bucket. The thresholds are
                    # left-closed: anything in (1, 3] days fires the 3d
                    # bucket so the warning lands well before the 1d one.
                    bucket: Optional[str] = None
                    if seconds_left <= 0:
                        bucket = "expired"
                    elif days_left <= 1:
                        bucket = "warned_1d"
                    elif days_left <= 3:
                        bucket = "warned_3d"
                    if bucket is None:
                        continue

                    # Idempotency guard — skip if we already logged this
                    # bucket today.
                    day_key = now.strftime("%Y-%m-%d")
                    reason_key = f"trial_{bucket}:{day_key}"
                    already = (
                        await db.execute(
                            select(TenantDataOpsLog.id)
                            .where(
                                TenantDataOpsLog.tenant_id == tenant.id,
                                TenantDataOpsLog.operation == "trial_notice",
                                TenantDataOpsLog.reason == reason_key,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if already is not None:
                        continue

                    if bucket == "expired":
                        if (
                            getattr(tenant, "subscription_status", None)
                            != "expired"
                        ):
                            tenant.subscription_status = "expired"
                    logger.info(
                        "trial_expiry_daily: tenant=%s bucket=%s ends_at=%s",
                        tenant.id,
                        bucket,
                        ends.isoformat(),
                    )
                    db.add(
                        TenantDataOpsLog(
                            tenant_id=tenant.id,
                            operation="trial_notice",
                            status="success",
                            reason=reason_key,
                            counts={"days_left": round(days_left, 2)},
                        )
                    )
                    emitted[bucket] = emitted.get(bucket, 0) + 1

                    await db.flush()
            await db.commit()
        return emitted

    return _run_async(_runner)


# ──────────────────────────────────────────────────────────
# Action Plan: inbound email matcher (Celery task)
# ──────────────────────────────────────────────────────────


@celery_app.task(
    name="action_plan_match_inbound_email", bind=True, max_retries=2,
)
def action_plan_match_inbound_email(
    self,
    interaction_id_str: str,
) -> Optional[str]:
    """For an inbound email Interaction, try to match it to an open
    Action Step via RFC 822 References + run Call D extraction.

    Returns the matched step_id (str) when one was found, None
    otherwise. Failures here NEVER bubble: we log and move on so a
    flaky LLM call doesn't kill email ingest.
    """

    async def _runner() -> Optional[str]:
        from backend.app.db import async_session as _async_session_factory
        from backend.app.models import ActionStep as _StepModel
        from backend.app.models import Interaction as _IxModel
        from backend.app.services.action_plan.engine import ActionPlanEngine
        from backend.app.services.action_plan.extractor import (
            ResponseExtractor,
            match_inbound_email,
        )

        async with _async_session_factory() as db:
            ix = await db.get(_IxModel, uuid.UUID(interaction_id_str))
            if ix is None or ix.channel != "email" or ix.direction != "inbound":
                return None
            match = await match_inbound_email(
                db,
                tenant_id=ix.tenant_id,
                in_reply_to=ix.in_reply_to,
                references=list(ix.references or []),
            )
            if match.step_id is None or match.reason in {"no_match", "step_closed"}:
                return None
            step = await db.get(_StepModel, match.step_id)
            if step is None:
                return None
            extractor = ResponseExtractor()
            body = ix.raw_text or ""
            extraction = await extractor.extract_for_step(
                step=step,
                source_label="inbound email",
                source_content=body,
            )
            from backend.app.models import StepResponse as _RespModel
            response = _RespModel(
                step_id=step.id,
                tenant_id=step.tenant_id,
                source="inbound_email",
                email_message_id=ix.id,
                extracted_data=extraction.extracted,
                source_quotes=extraction.source_quotes,
                unfilled_reasons=extraction.unfilled_reasons,
                extraction_confidence=extraction.confidence,
            )
            db.add(response)
            await db.flush()
            engine = ActionPlanEngine()
            await engine.apply_response(db, step=step, response=response)
            await db.commit()
            return str(step.id)

    try:
        return _run_async(_runner)
    except Exception:  # noqa: BLE001 - never let matching kill the task
        logger.exception(
            "action_plan_match_inbound_email failed for interaction %s "
            "(non-fatal)",
            interaction_id_str,
        )
        return None


@celery_app.task(
    name="action_plan_run_due_regenerations", bind=True,
)
def action_plan_run_due_regenerations(self) -> int:
    """Beat-scheduled tick that runs Call C for every step whose
    debounce timer has elapsed. Returns the count regenerated."""

    async def _runner() -> int:
        from backend.app.db import async_session as _async_session_factory
        from backend.app.services.action_plan.engine import (
            run_due_regenerations,
        )

        async with _async_session_factory() as db:
            count = await run_due_regenerations(db, limit=100)
            await db.commit()
            return count

    try:
        return _run_async(_runner)
    except Exception:
        logger.exception("action_plan_run_due_regenerations failed (non-fatal)")
        return 0


@celery_app.task(
    name="action_plan_run_due_executions", bind=True,
)
def action_plan_run_due_executions(self) -> Dict[str, Any]:
    """Beat-scheduled tick for the governed auto-executor.

    Default-OFF: the very first thing this does is check
    ``settings.AUTO_EXECUTION_ENABLED`` and return a no-op without
    touching the database when it's False (today's — and the shipped —
    default). When on, fans out per tenant under that tenant's RLS
    context (``tenant_context_async``) so each tenant's policy only ever
    sees its own steps, same wiring style as ``trial_expiry_daily``.
    """

    async def _runner() -> Dict[str, Any]:
        if not settings.AUTO_EXECUTION_ENABLED:
            return {"enabled": False, "tenants": 0}

        from sqlalchemy import select as _select

        from backend.app.db import async_session as _async_session_factory
        from backend.app.models import Tenant as _Tenant
        from backend.app.services.action_plan.executor import run_due_executions
        from backend.app.tenant_ctx import tenant_context_async

        totals: Dict[str, Any] = {"enabled": True, "tenants": 0}
        async with _async_session_factory() as db:
            tenant_ids = list(
                (await db.execute(_select(_Tenant.id))).scalars().all()
            )
            for tid in tenant_ids:
                async with tenant_context_async(tid, db):
                    try:
                        result = await run_due_executions(db, tenant_id=tid, limit=100)
                    except Exception:  # noqa: BLE001 - one tenant's failure doesn't kill the tick
                        logger.exception(
                            "action_plan_run_due_executions failed for tenant %s "
                            "(non-fatal)", tid,
                        )
                        continue
                totals["tenants"] += 1
                for key, value in result.items():
                    if key == "enabled":
                        continue
                    if isinstance(value, int):
                        totals[key] = totals.get(key, 0) + value
        return totals

    try:
        return _run_async(_runner)
    except Exception:
        logger.exception("action_plan_run_due_executions failed (non-fatal)")
        return {"enabled": settings.AUTO_EXECUTION_ENABLED, "tenants": 0}


# ── Manager-view tasks (anomaly scan, recommendations, resolution) ────


@celery_app.task(name="manager_anomaly_scan_all_tenants")
def manager_anomaly_scan_all_tenants() -> Dict[str, Any]:
    """Run the three anomaly detectors for every tenant. Fans out alert
    delivery (in-app + Slack) inline. See
    ``backend.app.services.anomaly_detector``.
    """
    from backend.app.services.anomaly_detector import scan_all_tenants
    from backend.app.services.manager_alert_fanout import fanout

    session = _get_sync_session()
    try:
        result = scan_all_tenants(session)
        # Fanout: pull the freshly inserted alerts and deliver. The
        # detector already commits; here we re-load only those that
        # were created in the last 60 seconds to avoid double-delivery.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        from sqlalchemy import select as _select

        from backend.app.models import ManagerAlert

        cutoff = _dt.now(_tz.utc) - _td(seconds=60)
        # Per tenant under its RLS context — an unscoped scan would see
        # zero rows now that the policies are live.
        from backend.app.models import Tenant
        from backend.app.tenant_ctx import tenant_context

        for tenant in session.query(Tenant).all():
            with tenant_context(tenant.id, session):
                fresh = (
                    session.execute(
                        _select(ManagerAlert).where(
                            ManagerAlert.created_at >= cutoff,
                            ManagerAlert.tenant_id == tenant.id,
                        )
                    )
                    .scalars()
                    .all()
                )
                if fresh:
                    fanout(session, fresh)
                    session.commit()
        return result
    finally:
        session.close()


@celery_app.task(name="manager_recommendations_build")
def manager_recommendations_build() -> Dict[str, Any]:
    """Daily Haiku-driven recommendation refresh per tenant."""
    from backend.app.services.manager_recommendation_builder import (
        build_for_all_tenants,
    )

    session = _get_sync_session()
    try:
        return build_for_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="manager_recommendations_expire")
def manager_recommendations_expire() -> Dict[str, Any]:
    """Sweep recommendations whose ``expires_at`` has passed."""
    from backend.app.services.manager_recommendation_builder import expire_old

    session = _get_sync_session()
    try:
        expired = expire_old(session)
        return {"expired": expired}
    finally:
        session.close()


@celery_app.task(name="manager_anomaly_resolve")
def manager_anomaly_resolve() -> Dict[str, Any]:
    """Mark stale manager_alerts whose underlying spike has subsided."""
    from backend.app.services.anomaly_detector import resolve_stale

    session = _get_sync_session()
    try:
        resolved = resolve_stale(session)
        return {"resolved": resolved}
    finally:
        session.close()


@celery_app.task(name="customer_memory_dormant_sweep")
def customer_memory_dormant_sweep() -> Dict[str, Any]:
    """Nightly: concerns not mentioned in 90 days transition to dormant
    so stale worries stop crowding briefs (and their token budget)."""
    from backend.app.services.customer_memory import sweep_dormant_concerns

    session = _get_sync_session()
    try:
        transitioned = sweep_dormant_concerns(session)
        session.commit()
        return {"concerns_transitioned": transitioned}
    finally:
        session.close()


@celery_app.task(name="orchestrator_daily_one_tenant")
def orchestrator_daily_one_tenant(tenant_id: str) -> Dict[str, Any]:
    """Force-refresh one tenant's BusinessProfile + sibling profiles.

    Called from the manager-page "Refresh now" CTA. Rate-limited at the
    API layer (1/hr/tenant via Redis) to bound Opus cost. Internally
    just delegates to ``_orchestrate_one_tenant`` — the same per-tenant
    body the daily chord uses.
    """
    return _orchestrate_one_tenant(tenant_id)
