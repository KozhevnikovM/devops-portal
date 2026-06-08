"""Post-provision VM configuration runners (v0.8.0).

The worker acts as the configuration control node: once a VM is provisioned, it retries an SSH
connect (every ``CONFIG_SSH_RETRY_INTERVAL`` up to ``CONFIG_SSH_TIMEOUT``) and then runs the
booking's startup script (P1.2) — and, later, Ansible roles (P2.2). Two failure modes are kept
distinct so the provision task can react differently:

* ``VmUnreachableError`` — the VM never accepted SSH → the VM is unusable → booking FAILED.
* ``ConfigScriptError`` — SSH worked but the script exited non-zero → the VM is up → booking READY
  but flagged ``config_failed``.

``StubConfigRunner`` is used in stub/dev mode where there is no real VM.
"""
import logging
import time
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Base for post-provision configuration failures."""


class VmUnreachableError(ConfigError):
    """The VM never became reachable over SSH within the timeout (infrastructure failure)."""


class ConfigScriptError(ConfigError):
    """SSH worked but the configuration script exited non-zero (software failure)."""


class ConfigRunner(Protocol):
    def connect(self, ip: str, password: str, on_progress=None): ...
    def run_script(self, client, script: str, on_progress=None) -> None: ...
    def close(self, client) -> None: ...


class StubConfigRunner:
    """No-op runner for stub/dev mode — there is no real VM to SSH into."""

    def connect(self, ip: str, password: str, on_progress=None):
        logger.info("StubConfigRunner: pretending %s is reachable", ip)
        return None  # sentinel client

    def run_script(self, client, script: str, on_progress=None) -> None:
        logger.info("StubConfigRunner: skipping startup script")

    def close(self, client) -> None:
        return None


class SshConfigRunner:
    """Connect to a provisioned VM over SSH and run its startup script.

    The worker is the control node. ``connect`` retries until sshd answers (or the timeout); the
    script runs on the user's own VM, not on the worker.
    """

    def connect(self, ip: str, password: str, on_progress=None):
        """Retry an SSH connect every ``CONFIG_SSH_RETRY_INTERVAL`` up to ``CONFIG_SSH_TIMEOUT``.

        Returns a connected paramiko client, or raises ``VmUnreachableError`` on timeout.
        """
        import paramiko  # lazy: only the worker (with paramiko installed) takes this path

        pkey = (
            paramiko.RSAKey.from_private_key_file(settings.VM_SSH_PRIVATE_KEY)
            if settings.VM_SSH_PRIVATE_KEY else None
        )
        interval = max(1, settings.CONFIG_SSH_RETRY_INTERVAL)
        deadline = time.monotonic() + settings.CONFIG_SSH_TIMEOUT
        attempt = 0
        last_err = None
        while True:
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
                if time.monotonic() + interval >= deadline:
                    break
                if on_progress:
                    on_progress(f"Waiting for SSH on {ip} (attempt {attempt})…")
                time.sleep(interval)
        raise VmUnreachableError(
            f"VM {ip} not reachable over SSH within {settings.CONFIG_SSH_TIMEOUT}s: {last_err}"
        )

    @staticmethod
    def run_script(client, script: str, on_progress=None) -> None:
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
            raise ConfigScriptError(f"startup script failed (exit {exit_code}): {err or tail}")

    @staticmethod
    def close(client) -> None:
        if client is not None:
            client.close()


def build_config_runner() -> ConfigRunner:
    """Pick the runner like the terraform adapter: stub in dev, SSH against real VMs."""
    return StubConfigRunner() if settings.USE_STUB_TERRAFORM else SshConfigRunner()
