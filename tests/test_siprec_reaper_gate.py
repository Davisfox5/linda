"""The SIPREC reaper must not poll Postgres unless SIPREC is deployed.

This is a cost guard, and its failure mode is silent: an always-on 60s
database poll breaks nothing, it just means an idle deployment can never
scale to zero. On Neon, scale-to-zero requires an interval with no client
connections at all, so a periodic query pins the compute endpoint awake
around the clock and bills for it. That is exactly what happened — the
reaper ran unconditionally while the SRS process group was commented out
in both fly.toml and fly.production.toml, so it polled continuously for a
feature that was not deployed.

The default therefore has to stay False, and the loop has to be genuinely
conditional rather than started-then-idled.
"""

import inspect

from backend.app.config import Settings


def test_the_reaper_is_off_by_default():
    """If this flips to True, every deployment silently starts polling."""
    assert Settings.model_fields["SIPREC_REAPER_ENABLED"].default is False


def test_the_interval_is_configurable():
    assert Settings.model_fields["SIPREC_REAP_INTERVAL_S"].default == 60.0


def _lifespan_source() -> str:
    from backend.app import main

    return inspect.getsource(main.lifespan)


def test_the_loop_is_started_conditionally_not_unconditionally():
    """The task must be created inside the flag check.

    Starting the loop and having it no-op internally would still wake the
    database on every tick, which is the entire cost being avoided.
    """
    src = _lifespan_source()
    assert "if settings.SIPREC_REAPER_ENABLED:" in src
    create_idx = src.index("create_task(_siprec_reap_loop())")
    guard_idx = src.index("if settings.SIPREC_REAPER_ENABLED:")
    assert guard_idx < create_idx, "reap task is created outside the flag guard"


def test_shutdown_tolerates_a_task_that_was_never_started():
    """With the flag off there is no task to cancel; cancelling None raises."""
    src = _lifespan_source()
    assert "if reap_task is not None:" in src


def test_the_loop_reads_the_configured_interval():
    src = _lifespan_source()
    assert "settings.SIPREC_REAP_INTERVAL_S" in src
    assert "await _asyncio.sleep(60.0)" not in src, "interval is hard-coded again"


def test_siprec_is_still_undeployed_in_both_fly_configs():
    """If the SRS process group is resurrected, this test should fail and
    force whoever did it to decide about the reaper deliberately."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    for name in ("fly.toml", "fly.production.toml"):
        text = (repo / name).read_text(encoding="utf-8")
        active = [
            ln for ln in text.splitlines()
            if "siprec" in ln.lower() and not ln.strip().startswith("#")
        ]
        assert not active, (
            f"{name} has uncommented SIPREC config: {active}. "
            "Decide whether SIPREC_REAPER_ENABLED should now be true."
        )
