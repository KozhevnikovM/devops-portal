"""Apply Ansible roles to a provisioned VM (v0.8.0 P2.2).

The worker is the control node: it renders a throwaway inventory + playbook from the booking's
``config_roles`` snapshot and runs ``ansible-playbook`` over SSH against the VM. A non-zero run
raises ``AnsibleConfigError`` — handled like a failed startup script: the VM is reachable and
usable, so the booking goes READY but flagged ``config_failed``.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from app.config import settings
from app.infrastructure.config.runner import ConfigError

logger = logging.getLogger(__name__)


class AnsibleConfigError(ConfigError):
    """ansible-playbook exited non-zero while applying a booking's roles."""


def _render_inventory(ip: str, password: str) -> str:
    parts = [
        "target",
        f"ansible_host={ip}",
        f"ansible_user={settings.VM_SSH_USER}",
        f"ansible_port={settings.VM_SSH_PORT}",
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'",
    ]
    if settings.VM_SSH_PRIVATE_KEY:
        parts.append(f"ansible_ssh_private_key_file={settings.VM_SSH_PRIVATE_KEY}")
    else:
        parts.append(f"ansible_password={password}")
        parts.append("ansible_become_password={}".format(password))
    return "[vm]\n" + " ".join(parts) + "\n"


def _render_playbook(config_roles: list) -> str:
    """Render a play whose roles come from the snapshot, each with its vars (inline JSON = YAML)."""
    lines = ["- hosts: vm", "  become: true", "  gather_facts: true", "  roles:"]
    for role in config_roles:
        lines.append(f"    - role: {role['ansible_role']}")
        vars_ = role.get("vars") or {}
        if vars_:
            lines.append(f"      vars: {json.dumps(vars_)}")
    return "\n".join(lines) + "\n"


class StubAnsibleRunner:
    """No-op runner for stub/dev mode — there is no real VM to configure."""

    def apply_roles(self, booking, *, ip: str, password: str, on_progress=None) -> None:
        if booking.config_roles:
            logger.info("StubAnsibleRunner: skipping %d role(s) for %s", len(booking.config_roles), booking.id)


class AnsibleConfigRunner:
    def apply_roles(self, booking, *, ip: str, password: str, on_progress=None) -> None:
        roles = booking.config_roles or []
        if not roles:
            return
        with tempfile.TemporaryDirectory(prefix="portal-ansible-") as tmp:
            inv = Path(tmp) / "inventory.ini"
            play = Path(tmp) / "configure_vm.yml"
            inv.write_text(_render_inventory(ip, password))
            play.write_text(_render_playbook(roles))
            self._run(["ansible-playbook", "-i", str(inv), str(play)], on_progress)

    def _run(self, cmd: list[str], on_progress=None) -> None:
        env = {
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_ROLES_PATH": settings.ANSIBLE_ROLES_PATH,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "ANSIBLE_STDOUT_CALLBACK": "default",
        }
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        lines: list[str] = []
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line:
                lines.append(line)
                if on_progress:
                    on_progress("\n".join(lines[-3:]))
        try:
            proc.wait(timeout=settings.ANSIBLE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AnsibleConfigError(f"ansible-playbook timed out after {settings.ANSIBLE_TIMEOUT}s")
        if proc.returncode != 0:
            tail = "\n".join(lines[-8:])
            raise AnsibleConfigError(f"ansible-playbook failed (exit {proc.returncode}):\n{tail}")


def build_ansible_runner():
    """Pick the runner like the terraform/SSH runners: stub in dev, real against VMs."""
    return StubAnsibleRunner() if settings.USE_STUB_TERRAFORM else AnsibleConfigRunner()
