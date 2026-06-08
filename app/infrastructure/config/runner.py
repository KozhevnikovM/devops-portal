"""Post-provision VM configuration runners (v0.8.0).

The worker acts as the configuration control node: once a VM is provisioned and reachable, it
SSHes in and runs the booking's startup script (P1.2) — and, later, Ansible roles (P2.2). The
``ConfigRunner`` Protocol keeps the provision task decoupled from the concrete executor; a
``StubConfigRunner`` is used in stub/dev mode where there is no real VM.
"""
import logging
import time
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Post-provision configuration failed (SSH unreachable, non-zero script exit, …)."""


class ConfigRunner(Protocol):
    def run(self, booking, *, ip: str, password: str, on_progress=None) -> None: ...


class StubConfigRunner:
    """No-op runner for stub/dev mode — there is no real VM to SSH into."""

    def run(self, booking, *, ip: str, password: str, on_progress=None) -> None:
        logger.info("StubConfigRunner: skipping configuration for booking %s (ip=%s)", booking.id, ip)


class SshConfigRunner:
    """Run a booking's ``startup_script`` on the VM over SSH.

    Waits for sshd to come up (up to ``CONFIG_SSH_TIMEOUT``), connects as ``VM_SSH_USER`` with the
    VM password (or ``VM_SSH_PRIVATE_KEY`` when set), and runs the script through ``bash -s``,
    streaming output via ``on_progress``. Raises ``ConfigError`` if SSH never comes up or the script
    exits non-zero. The script runs on the user's own VM, not on the worker.
    """

    def run(self, booking, *, ip: str, password: str, on_progress=None) -> None:
        script = booking.startup_script
        if not script:
            return
        client = self._connect(ip, password, on_progress)
        try:
            self._run_script(client, script, on_progress)
        finally:
            client.close()

    def _connect(self, ip: str, password: str, on_progress=None):
        import paramiko  # lazy: only the worker (with paramiko installed) takes this path

        pkey = (
            paramiko.RSAKey.from_private_key_file(settings.VM_SSH_PRIVATE_KEY)
            if settings.VM_SSH_PRIVATE_KEY else None
        )
        deadline = time.monotonic() + settings.CONFIG_SSH_TIMEOUT
        attempt = 0
        last_err = None
        while time.monotonic() < deadline:
            attempt += 1
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=ip, port=settings.VM_SSH_PORT, username=settings.VM_SSH_USER,
                    password=None if pkey else password, pkey=pkey,
                    timeout=10, banner_timeout=15, auth_timeout=15,
                    look_for_keys=False, allow_agent=False,
                )
                if on_progress:
                    on_progress(f"SSH connected to {ip} as {settings.VM_SSH_USER}")
                return client
            except Exception as exc:  # retry until the deadline — the VM may still be booting
                last_err = exc
                client.close()
                if on_progress and attempt % 3 == 0:
                    on_progress(f"Waiting for SSH on {ip}…")
                time.sleep(5)
        raise ConfigError(
            f"SSH to {ip} not available within {settings.CONFIG_SSH_TIMEOUT}s: {last_err}"
        )

    @staticmethod
    def _run_script(client, script: str, on_progress=None) -> None:
        # Feed the whole script to bash over stdin so multi-line scripts run as one unit.
        stdin, stdout, stderr = client.exec_command("bash -s")
        stdin.write(script)
        stdin.channel.shutdown_write()

        lines: list[str] = []
        for raw in stdout:
            line = raw.rstrip("\n")
            if line:
                lines.append(line)
                if on_progress:
                    on_progress("\n".join(lines[-3:]))

        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr.read().decode(errors="replace").strip()
            tail = "\n".join(lines[-5:])
            raise ConfigError(f"startup script failed (exit {exit_code}): {err or tail}")


def build_config_runner() -> ConfigRunner:
    """Pick the runner like the terraform adapter: stub in dev, SSH against real VMs."""
    return StubConfigRunner() if settings.USE_STUB_TERRAFORM else SshConfigRunner()
