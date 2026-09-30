"""Task-level progress batching: barriers, attempt isolation and freshness (#444).

The real ``provision_vm_task`` / ``teardown_vm_task`` run with the repository mocked to log every
write in order, and with the recorder built on a fake clock + timer, so ordering between buffered
progress and lifecycle writes is checked deterministically.
"""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.booking_status import (
    PROVISIONING_PROGRESS_STATUSES,
    TEARDOWN_PROGRESS_STATUSES,
)
from app.domain.enums import BookingStatus
from app.infrastructure.config.ansible import AnsibleConfigError
from app.infrastructure.config.runner import ConfigScriptError, VmUnreachableError
from app.infrastructure.progress_recorder import ProgressRecorder
from tests.test_progress_coalescing import FakeScheduler
from tests.test_provision_session_lifetime import _SessionFactory


class Harness:
    def __init__(self, *, status_after_apply=BookingStatus.PROVISIONING, startup_script=None, config_roles=None):
        self.sched = FakeScheduler()
        self.events: list[tuple] = []
        self.recorders: list[ProgressRecorder] = []
        self.timers_by_attempt: list[list] = []
        self.sessions = _SessionFactory()
        self.status_after_apply = status_after_apply
        self.booking = MagicMock(
            status=status_after_apply, startup_script=startup_script, config_roles=config_roles,
            extra_vars={}, environment_label="", vm_password=None,
        )
        self.progress_fail = 0
        repo = MagicMock()
        self._gets = 0

        def sync_get(_s, _id):
            self._gets += 1
            if self._gets % 2 == 1:  # first read of each attempt: the "is it released?" check
                return MagicMock(status=BookingStatus.PENDING, vm_password=None)
            return self.booking

        def append(_s, _id, chunk, last, accepting):
            if self.progress_fail:
                self.progress_fail -= 1
                raise RuntimeError("db down")
            self.events.append(("progress", chunk, last, accepting))

        repo.sync_get.side_effect = sync_get
        repo.sync_append_progress.side_effect = append
        repo.sync_set_status_message.side_effect = lambda _s, _id, m: self.events.append(("message", m))
        repo.sync_update_status.side_effect = lambda _s, _id, st, **kw: self.events.append(("status", st))
        self.repo = repo
        self.env_repo = MagicMock()
        self.teardown_task = MagicMock()
        self.teardown_task.delay.side_effect = lambda *a, **kw: self.events.append(("teardown",))

    def _make_recorder(self, persist, *, label):
        timers: list = []
        self.timers_by_attempt.append(timers)

        def timer_factory(delay, cb):
            t = self.sched(delay, cb)
            timers.append(t)
            return t

        rec = ProgressRecorder(
            persist, interval_s=0.5, message_threshold=50, char_threshold=16_384,
            clock=lambda: self.sched.now, timer_factory=timer_factory, label=label,
        )
        self.recorders.append(rec)
        return rec

    def patches(self, *, stub=True, apply=None, connect=None, run_script=None, apply_roles=None, extra=()):
        stack = ExitStack()
        terraform = MagicMock()
        terraform.apply = apply
        config_runner = MagicMock()
        config_runner.connect.side_effect = connect or (lambda ip, pw, on_progress=None: MagicMock())
        if run_script:
            config_runner.run_script.side_effect = run_script
        ansible_runner = MagicMock()
        if apply_roles:
            ansible_runner.apply_roles.side_effect = apply_roles
        for target, value in [
            ("app.tasks.provision.SyncSessionLocal", self.sessions),
            ("app.tasks.provision.repo", self.repo),
            ("app.tasks.provision.env_repo", self.env_repo),
            ("app.tasks.provision.image_repo", MagicMock()),
            ("app.tasks.provision.hw_config_repo", MagicMock()),
            ("app.tasks.provision.terraform", terraform),
            ("app.tasks.provision.config_runner", config_runner),
            ("app.tasks.provision.ansible_runner", ansible_runner),
            ("app.tasks.provision.provisioning_lock", MagicMock()),
            ("app.tasks.provision.teardown_vm_task", self.teardown_task),
            ("app.tasks.provision.recorder_from_settings", self._make_recorder),
            ("app.tasks.provision.settings.USE_STUB_TERRAFORM", stub),
            ("app.tasks.provision.settings.VCD_API_TOKENS", ""),
            ("app.tasks.provision.settings.VCD_API_TOKEN", ""),
            *extra,
        ]:
            stack.enter_context(patch(target, value))
        return stack

    def run(self, **kw):
        with self.patches(**kw):
            from app.tasks.provision import provision_vm_task
            provision_vm_task.apply(args=[str(uuid4()), str(uuid4()), str(uuid4())])

    # ── queries ──
    def persisted_log(self) -> str:
        return "".join(e[1] for e in self.events if e[0] == "progress")

    def index(self, pred) -> int:
        return next(i for i, e in enumerate(self.events) if pred(e))

    def index_of_progress_containing(self, text: str) -> int:
        return self.index(lambda e: e[0] == "progress" and text in e[1])


def burst_apply(lines, *, fail=None, after=None):
    """A fake terraform.apply that records ``lines`` with the clock frozen (so all but the first are
    buffered), then optionally runs ``after(on_progress)`` and/or raises ``fail``."""
    async def apply(workspace_id, config, api_token=None, on_progress=None):
        for line in lines:
            on_progress(line)
        if after:
            after(on_progress)
        if fail:
            raise fail
        return {"ip": "10.0.0.9"}
    return apply


# ── lifecycle barriers ────────────────────────────────────────────────────────────────────────

def test_normal_completion_flushes_all_progress_before_clear_and_ready():
    h = Harness()
    lines = [f"tf {i}" for i in range(10)]
    h.run(apply=burst_apply(lines))
    last = h.index_of_progress_containing("tf 9")
    assert last < h.index(lambda e: e == ("message", None))
    assert last < h.index(lambda e: e == ("status", BookingStatus.READY))
    assert h.persisted_log() == "".join(line + "\n" for line in lines)
    assert len([e for e in h.events if e[0] == "progress"]) < len(lines)  # batched
    assert all(e[3] == PROVISIONING_PROGRESS_STATUSES for e in h.events if e[0] == "progress")


def test_configuration_script_failure_output_lands_before_error_message_and_ready():
    h = Harness(startup_script="echo hi")

    def run_script(client, script, on_progress=None):
        for i in range(8):
            on_progress(f"script {i}")
        raise ConfigScriptError("exit 1")

    h.run(stub=False, apply=burst_apply([]), run_script=run_script)
    last = h.index_of_progress_containing("script 7")
    assert last < h.index(lambda e: e == ("message", "exit 1"))
    assert last < h.index(lambda e: e == ("status", BookingStatus.READY))
    assert h.events[-1] == ("status", BookingStatus.READY)


def test_ansible_failure_output_lands_before_error_message_and_ready():
    h = Harness(config_roles=["nginx"])

    def apply_roles(booking, **kw):
        for i in range(8):
            kw["on_progress"](f"TASK [nginx] {i}\nok: [host]")
        raise AnsibleConfigError("role nginx failed")

    h.run(stub=False, apply=burst_apply([]), apply_roles=apply_roles)
    last = h.index_of_progress_containing("TASK [nginx] 7\nok: [host]")
    assert last < h.index(lambda e: e == ("message", "role nginx failed"))
    assert last < h.index(lambda e: e == ("status", BookingStatus.READY))


def test_terraform_failure_output_lands_before_failure_message_and_retry():
    h = Harness()
    h.run(apply=burst_apply([f"tf {i}" for i in range(6)], fail=RuntimeError("VCD error")))
    last = h.index_of_progress_containing("tf 5")
    assert last < h.index(lambda e: e == ("message", "Failed — see audit log"))
    assert last < h.index(lambda e: e == ("status", BookingStatus.RETRY))


def test_ssh_unreachable_output_lands_before_failure_message():
    h = Harness(startup_script="echo hi")

    def connect(ip, pw, on_progress=None):
        for i in range(5):
            on_progress(f"Waiting for SSH on {ip} (attempt {i})…")
        raise VmUnreachableError("unreachable")

    h.run(stub=False, apply=burst_apply([]), connect=connect)
    last = h.index_of_progress_containing("(attempt 4)")
    assert last < h.index(lambda e: e == ("message", "Failed — see audit log"))
    statuses = [e[1] for e in h.events if e[0] == "status"]
    assert BookingStatus.RETRY in statuses


def test_release_during_provisioning_flushes_before_teardown_handoff_and_closes():
    h = Harness(status_after_apply=BookingStatus.RELEASING)
    h.run(stub=False, apply=burst_apply([f"tf {i}" for i in range(6)]))
    assert h.index_of_progress_containing("tf 5") < h.index(lambda e: e == ("teardown",))
    rec = h.recorders[0]
    n = len(h.events)
    rec.record("late line")
    h.sched.advance(10)
    assert len(h.events) == n


# ── attempt isolation ─────────────────────────────────────────────────────────────────────────

def test_retry_never_persists_the_previous_attempts_progress():
    h = Harness()
    attempts = {"n": 0}

    async def apply(workspace_id, config, api_token=None, on_progress=None):
        attempts["n"] += 1
        tag = f"attempt{attempts['n']}"
        for i in range(5):
            on_progress(f"{tag} line {i}")
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return {"ip": "10.0.0.9"}

    h.run(apply=apply)
    assert len(h.recorders) == 2
    first_retry = h.index(lambda e: e == ("status", BookingStatus.RETRY))
    attempt1_progress = [i for i, e in enumerate(h.events) if e[0] == "progress" and "attempt1" in e[1]]
    assert attempt1_progress and max(attempt1_progress) < first_retry
    # attempt 2's own chunks never carry attempt-1 text
    assert all("attempt1" not in e[1] for e in h.events[first_retry:] if e[0] == "progress")
    # a timer from attempt 1 firing late (after its attempt returned) persists nothing
    n = len(h.events)
    for timer in h.timers_by_attempt[0]:
        timer.callback()
    h.sched.advance(10)
    assert len(h.events) == n


# ── freshness and connection lifetime ─────────────────────────────────────────────────────────

def test_isolated_ssh_wait_line_persists_immediately_and_burst_then_silence_within_interval():
    h = Harness()
    seen = {}

    def after(on_progress):
        on_progress("Waiting for 10.0.0.9 to become reachable…")
        seen["after_isolated"] = h.persisted_log()
        h.sched.advance(2.0)
        on_progress("b1")                       # leading edge
        on_progress("b2")
        on_progress("b3")                       # buffered
        seen["before_silence"] = h.persisted_log()
        seen["sessions_open_between_flushes"] = h.sessions.open_now
        h.sched.advance(0.5)                    # silence for one interval, apply still running
        seen["after_silence"] = h.persisted_log()

    h.sched.advance(5.0)
    h.run(apply=burst_apply([], after=after))
    assert seen["after_isolated"].endswith("Waiting for 10.0.0.9 to become reachable…\n")
    assert not seen["before_silence"].endswith("b3\n")
    assert seen["after_silence"].endswith("b1\nb2\nb3\n")
    assert seen["sessions_open_between_flushes"] == 0


def test_progress_db_failure_mid_apply_does_not_fail_the_attempt():
    h = Harness()
    h.progress_fail = 1                          # the leading-edge persist fails
    h.run(apply=burst_apply(["a", "b", "c"]))
    statuses = [e[1] for e in h.events if e[0] == "status"]
    assert statuses[-1] == BookingStatus.READY
    assert BookingStatus.RETRY not in statuses
    assert h.persisted_log() == "a\nb\nc\n"     # retained, then persisted by the barrier


def test_token_lock_refreshed_on_every_callback_even_when_buffered():
    h = Harness()
    redis_client = MagicMock()
    extra = (
        ("app.tasks.provision._token_pool", lambda: ["tok"]),
        ("app.tasks.provision._acquire_token", lambda tokens, client: ("vcd_token_lock:0:0", "tok")),
        ("app.tasks.provision.redis_lib.Redis.from_url", lambda url: redis_client),
    )
    h.run(stub=False, apply=burst_apply([f"tf {i}" for i in range(10)]), extra=extra)
    assert redis_client.expire.call_count >= 10
    assert len([e for e in h.events if e[0] == "progress"]) < 10


# ── teardown ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fail", [False, True])
def test_teardown_flushes_progress_before_lifecycle_writes(fail):
    sched = FakeScheduler()
    events: list[tuple] = []
    repo = MagicMock()
    repo.sync_get.return_value = MagicMock(image_id=uuid4(), hw_config_id=uuid4(), vm_password="p")
    repo.sync_append_progress.side_effect = lambda _s, _id, chunk, last, acc: events.append(("progress", chunk, acc))
    repo.sync_set_status_message.side_effect = lambda _s, _id, m: events.append(("message", m))
    repo.sync_update_status.side_effect = lambda _s, _id, st, **kw: events.append(("status", st))

    async def destroy(workspace_id, config, api_token=None, on_progress=None, force=False):
        for i in range(6):
            on_progress(f"destroy {i}")
        if fail:
            raise RuntimeError("destroy failed")

    terraform = MagicMock()
    terraform.destroy = destroy

    def make(persist, *, label):
        return ProgressRecorder(
            persist, interval_s=0.5, message_threshold=50, char_threshold=16_384,
            clock=lambda: sched.now, timer_factory=sched, label=label,
        )

    with (
        patch("app.tasks.teardown.SyncSessionLocal", _SessionFactory()),
        patch("app.tasks.teardown.repo", repo),
        patch("app.tasks.teardown.image_repo", MagicMock()),
        patch("app.tasks.teardown.hw_config_repo", MagicMock()),
        patch("app.tasks.teardown.terraform", terraform),
        patch("app.tasks.teardown.recorder_from_settings", make),
    ):
        from app.tasks.teardown import teardown_vm_task
        teardown_vm_task.apply(args=[str(uuid4())], kwargs={"force": True})

    last = next(i for i, e in enumerate(events) if e[0] == "progress" and "destroy 5" in e[1])
    assert last < events.index(("message", None))
    assert last < events.index(("status", BookingStatus.RELEASED))
    assert all(e[2] == TEARDOWN_PROGRESS_STATUSES for e in events if e[0] == "progress")
