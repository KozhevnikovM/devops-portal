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

import yaml

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


def _render_playbook(config_roles: list, secrets_path: str | None = None) -> str:
    """Render a play whose roles come from the snapshot, each with its vars (inline JSON = YAML).

    When *secrets_path* is provided, ``no_log: true`` is set at the play level to prevent
    Ansible from printing secret variable values in task output or verbose logs.
    """
    lines = ["- hosts: vm", "  become: true", "  gather_facts: true"]
    if secrets_path:
        lines.append("  no_log: true")
        lines.append("  vars_files:")
        lines.append(f"    - {secrets_path}")
    lines.append("  roles:")
    for role in config_roles:
        lines.append(f"    - role: {role['ansible_role']}")
        vars_ = role.get("vars") or {}
        if vars_:
            lines.append(f"      vars: {json.dumps(vars_)}")
    return "\n".join(lines) + "\n"


def _decrypt_all_secrets(config_roles: list) -> dict:
    """Decrypt and merge secret_vars from all roles atomically.

    Decrypts the full merged dict before returning anything — callers never receive a
    partial result. Raises SecretDecryptionError (wrapped InvalidToken) on any failure.
    """
    from cryptography.fernet import InvalidToken
    from app.domain.exceptions import SecretDecryptionError
    from app.infrastructure.crypto import decrypt_dict

    merged: dict = {}
    for role in config_roles:
        sv = role.get("secret_vars") or {}
        if not sv:
            continue
        try:
            merged.update(decrypt_dict(sv, settings.SECRETS_ENCRYPTION_KEY))
        except (InvalidToken, ValueError) as exc:
            raise SecretDecryptionError(
                f"Failed to decrypt secret_vars for role '{role.get('name', '?')}': {exc}"
            ) from exc
    return merged


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

        # Decrypt all secrets atomically before opening any file.
        # If any key fails to decrypt, SecretDecryptionError is raised before secrets.yml is created.
        merged_secrets = _decrypt_all_secrets(roles)

        # tempfile.TemporaryDirectory creates the dir with 0o700 (Python stdlib guarantee).
        with tempfile.TemporaryDirectory(prefix="portal-ansible-") as tmp:
            inv = Path(tmp) / "inventory.ini"
            play = Path(tmp) / "configure_vm.yml"
            inv.write_text(_render_inventory(ip, password))

            secrets_path = None
            if merged_secrets:
                secrets_file = Path(tmp) / "secrets.yml"
                secrets_file.write_text(yaml.safe_dump(merged_secrets))
                secrets_file.chmod(0o600)
                secrets_path = str(secrets_file)

            play.write_text(_render_playbook(roles, secrets_path=secrets_path))
            self._run(["ansible-playbook", "-i", str(inv), str(play)], on_progress)

    def _run(self, cmd: list[str], on_progress=None) -> None:
        env = {
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_ROLES_PATH": settings.ANSIBLE_ROLES_PATH,
            "ANSIBLE_COLLECTIONS_PATH": settings.ANSIBLE_COLLECTIONS_PATH,
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
