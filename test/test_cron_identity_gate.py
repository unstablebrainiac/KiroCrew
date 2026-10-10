"""A cron fire runs the Kiro account-identity gate before it acquires its session.

A cron fire whose sub-agents are still running skips its session reset, and the
next fire of a ``persistent_session`` job, or of the same ``agent_sequence`` step,
reuses that live session. After an account switch or a ``kiro-cli logout`` outside
the dashboard its child still holds the previous account's credential. The
dashboard retires such a child with a per-turn gate,
``chat_runner._retire_sessions_on_identity_change``, run before ``get_or_create``.
The cron path ran no such gate, so on a host where only crons run an agent cron
kept answering on the previous account. These tests drive the real cron callback
through the real gate, over a fake prerequisite service and the gateway's own
session manager, and pin the order: gate first, then acquire.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from member_memory_helpers import write_member_home

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import CronJob, CronSchedule

_REPLY = "Agent response here"


class _Prerequisites:
    """The ``KiroPrerequisiteService`` surface the identity gate reads."""

    def __init__(self, *, changed: bool, live: str) -> None:
        self._changed = changed
        self._live = live
        self.identity_observation_generation = 0
        self.reconciled: list[str] = []

    async def identity_changed_since_sessions(self) -> tuple[bool, str]:
        return self._changed, self._live

    def note_sessions_reconciled(
        self, fingerprint: str, *, observations_before: int | None = None
    ) -> None:
        self.reconciled.append(fingerprint)

    def mark_signed_out(self) -> None:
        pass


def _record_gate_and_acquire(sessions: MagicMock, order: list[str], acquire_error=None) -> None:
    """Wire the gate's session surface and ``get_or_create`` to log their calls in order."""

    async def _flag(live: str) -> list[str]:
        order.append("flag")
        return []

    async def _retire(fingerprint: str = "") -> tuple[list[str], bool]:
        order.append(f"retire:{fingerprint}")
        return [], True

    async def _acquire(key: str, **_kwargs):
        order.append(f"acquire:{key}")
        if acquire_error is not None:
            raise acquire_error
        return MagicMock(), True, False

    sessions.flag_identity_stamp_mismatches = AsyncMock(side_effect=_flag)
    sessions.retire_kiro_identity_sessions = AsyncMock(side_effect=_retire)
    sessions.pending_identity_sweep_fingerprint = ""
    sessions.identity_sweep_waiting_on = ()
    sessions.get_or_create = AsyncMock(side_effect=_acquire)


def _dashboard_state(sessions: MagicMock, prerequisites: _Prerequisites) -> MagicMock:
    state = MagicMock()
    state.get_slot = MagicMock(return_value=None)
    state.has_slot = MagicMock(return_value=False)
    state.notify = MagicMock()
    state.kiro_prerequisite_service = prerequisites
    # One session manager, as in production: _init_dashboard and _init_api_server
    # hand the gateway's own to the dashboard.
    state.sessions = sessions
    return state


def _make_gw(prerequisites: _Prerequisites, order: list[str]):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw._cfg = MagicMock()
    gw._cfg.agent.provider = "acp"
    gw._cfg.hooks = {}
    gw._approval_mode = None
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.ctx_builder.build_message = MagicMock(return_value=("full prompt", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    _record_gate_and_acquire(gw.sessions, order)
    gw.dashboard_state = _dashboard_state(gw.sessions, prerequisites)
    return gw


async def _run_cron(gw, job):
    """Run *job* through the real ``_cron_callback`` captured from ``_init_cron``."""
    captured_cb = None
    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_in_embed_pool",
            AsyncMock(return_value=("full prompt", None)),
        ),
        patch("kiro_crew.slack.gateway.stream_and_collect", AsyncMock(return_value=_REPLY)),
        patch("kiro_crew.slack.gateway.persist_token_record_async", AsyncMock()),
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
    ):
        mock_ctx.return_value = (f"cron:{job.id}", job.message)

        def capture_cron(on_job=None, **_kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured_cb is not None
        return await captured_cb(job)


def _agent_job(job_id: str) -> CronJob:
    return CronJob(
        id=job_id,
        name="agent-cron",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
    )


@pytest.mark.asyncio
async def test_a_changed_account_retires_stale_sessions_before_the_cron_acquires() -> None:
    order: list[str] = []
    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = _make_gw(prerequisites, order)

    result = await _run_cron(gw, _agent_job("gate"))

    # The sweep ran against the live account BEFORE the session was acquired,
    # so the stale child is gone when get_or_create looks for one.
    assert order == ["flag", "retire:fp-current", "acquire:cron:gate"]
    # A complete sweep reconciles the baseline, so the next fire does not re-sweep.
    assert prerequisites.reconciled == ["fp-current"]
    # The run itself still lands.
    assert _REPLY in result


@pytest.mark.asyncio
async def test_an_unchanged_account_acquires_without_a_sweep() -> None:
    order: list[str] = []
    prerequisites = _Prerequisites(changed=False, live="fp-current")
    gw = _make_gw(prerequisites, order)

    result = await _run_cron(gw, _agent_job("steady"))

    # Only the cheap per-turn stamp check runs; nothing is retired.
    assert order == ["flag", "acquire:cron:steady"]
    gw.sessions.retire_kiro_identity_sessions.assert_not_awaited()
    assert prerequisites.reconciled == []
    assert _REPLY in result


@pytest.mark.asyncio
async def test_a_sequence_step_runs_the_gate_before_its_acquire(monkeypatch) -> None:
    from kiro_crew.slack import gateway

    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    # Resolution reads ``live.snapshot() or self._cfg``; pin both to the test config
    # so the two crew aliases are visible without a live config watcher.
    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)

    order: list[str] = []
    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.sessions = MagicMock()
    _record_gate_and_acquire(
        gw.sessions, order, acquire_error=RuntimeError("stop before the provider")
    )
    gw.ctx_builder = MagicMock()
    gw.slack = gw.conv_log = gw.subagent_mgr = None
    gw.dashboard_state = _dashboard_state(gw.sessions, prerequisites)
    gw._owner_id = "owner"
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._cfg = cfg
    gw.cron_svc = None
    callbacks = []

    async def create(on_job=None, **_kwargs):
        callbacks.append(on_job)
        service = MagicMock()
        service.start = AsyncMock()
        return service

    monkeypatch.setattr(gateway.CronService, "create", create)
    monkeypatch.setattr(
        gateway, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
    )
    await gw._init_cron()

    job = CronJob(id="seq", name="sequence", message="task", agent_sequence=["beta", "alpha"])
    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callbacks[0](job)

    # The step's own acquire, not only the single-agent one, is gated.
    assert order == ["flag", "retire:fp-current", "acquire:cron:seq:beta"]


@pytest.mark.asyncio
async def test_a_kept_alive_cron_session_from_the_old_account_is_retired_before_the_cron_acquires() -> (
    None
):
    from kiro_crew.session import SessionManager

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = MagicMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.memory_mode = kwargs.get("memory_mode", "persistent")
        provider.is_process_alive = lambda: True
        provider.has_active_turn = lambda: False
        provider.runtime_abort_target = lambda: None
        provider.uses_kiro_identity_store = True
        return provider

    manager = SessionManager(KiroCrewConfig(), provider_factory=factory)
    # The previous fire's session, kept alive past its run while sub-agents
    # finish: started before the account change, idle now.
    stale, _is_new, _resumed = await manager.get_or_create("cron:alive")
    manager.release("cron:alive")

    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = _make_gw(prerequisites, [])
    # The gate's surface and the acquire run on the real manager, so the sweep
    # sees the live session and the acquire makes the real reuse decision.
    gw.sessions.flag_identity_stamp_mismatches = manager.flag_identity_stamp_mismatches
    gw.sessions.retire_kiro_identity_sessions = manager.retire_kiro_identity_sessions
    gw.sessions.pending_identity_sweep_fingerprint = ""
    gw.sessions.identity_sweep_waiting_on = ()
    acquired = []

    async def _acquire(key: str, **kwargs):
        result = await manager.get_or_create(key, **kwargs)
        acquired.append(result[0])
        return result

    gw.sessions.get_or_create = AsyncMock(side_effect=_acquire)

    try:
        result = await _run_cron(gw, _agent_job("alive"))

        # Without the gate the fire reuses the live session on the old account.
        stale.shutdown.assert_awaited()
        assert len(acquired) == 1 and acquired[0] is not stale
        assert _REPLY in result
    finally:
        await manager.close_all()
