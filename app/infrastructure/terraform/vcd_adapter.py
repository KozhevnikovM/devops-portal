import asyncio
import json
import logging
import os
import re
import shutil
import textwrap
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# Terraform prints this on the "Error acquiring the state lock" message, followed by a
# "Lock Info" block whose `ID:` line carries the lock id needed to force-unlock it.
_LOCK_ERROR_MARKER = "Error acquiring the state lock"
_LOCK_ID_RE = re.compile(r"^\s*ID:\s*(\S+)", re.MULTILINE)

# VCD rejects creating a vApp that already exists — happens when an apply created the vApp but a
# reboot (SIGKILL) killed terraform before the new resource was persisted to state.
_ALREADY_EXISTS_RE = re.compile(r"entity (\S+) already exists")


class TerraformError(Exception):
    pass


class TerraformVcdAdapter:
    """Provisions VMs on VMware Cloud Director via the terraform CLI."""

    def _workspace_dir(self, workspace_id: str) -> Path:
        return Path(settings.TF_WORKSPACES_DIR) / workspace_id

    def _provider_block(self, api_token: str | None = None) -> str:
        ssl = str(settings.VCD_ALLOW_UNVERIFIED_SSL).lower()
        if api_token or settings.VCD_API_TOKEN:
            # Credentials supplied via VCD_TOKEN env var at subprocess time — not written to disk.
            return textwrap.dedent(f"""\
                provider "vcd" {{
                  url                  = "{settings.VCD_URL}"
                  org                  = "{settings.VCD_ORG}"
                  vdc                  = "{settings.VCD_VDC}"
                  user                 = "none"
                  password             = "none"
                  auth_type            = "api_token"
                  allow_api_token_file = true
                  max_retry_timeout    = 1800
                  allow_unverified_ssl = {ssl}
                }}""")
        # Credentials supplied via VCD_USER / VCD_PASSWORD env vars at subprocess time.
        return textwrap.dedent(f"""\
            provider "vcd" {{
              url                  = "{settings.VCD_URL}"
              org                  = "{settings.VCD_ORG}"
              vdc                  = "{settings.VCD_VDC}"
              auth_type            = "integrated"
              max_retry_timeout    = 1800
              allow_unverified_ssl = {ssl}
            }}""")

    def _cred_env(self, api_token: str | None = None) -> dict:
        """Credential env vars for the terraform subprocess — never written to disk."""
        token = api_token or settings.VCD_API_TOKEN
        if token:
            return {"VCD_TOKEN": token}
        return {"VCD_USER": settings.VCD_USER, "VCD_PASSWORD": settings.VCD_PASSWORD}

    def _write_workspace(self, workspace_dir: Path, config: dict, api_token: str | None = None) -> None:
        workspace_dir.mkdir(parents=True, exist_ok=True)

        main_tf = textwrap.dedent(f"""\
            terraform {{
              required_providers {{
                vcd = {{
                  source  = "vmware/vcd"
                  version = ">=3.10.0"
                }}
              }}
              required_version = ">= 1.5.5"
              backend "pg" {{
                conn_str    = "{settings.TF_PG_CONN_STR}"
                schema_name = "tfstate"
              }}
            }}

            {self._provider_block(api_token)}

            resource "vcd_vapp" "this" {{
              name = var.name
            }}

            resource "vcd_vapp_org_network" "this" {{
              vapp_name              = vcd_vapp.this.name
              org_network_name       = var.network_name
              reboot_vapp_on_removal = true
            }}

            module "vm" {{
              source           = "{settings.TF_MODULE_SOURCE}"
              name             = var.name
              vapp_name        = vcd_vapp.this.name
              network_name     = var.network_name
              vapp_template_id = var.vapp_template_id
              cpus             = var.cpus
              memory           = var.memory
              resize_disk      = true
              disk_size        = var.disk_size
              customization    = var.customization
              depends_on       = [vcd_vapp_org_network.this]
            }}

            output "primary_ip" {{
              value = module.vm.primary_ip
            }}

            variable "name"             {{ type = string }}
            variable "network_name"     {{ type = string }}
            variable "vapp_template_id" {{ type = string }}
            variable "cpus"             {{ type = number }}
            variable "memory"           {{ type = number }}
            variable "disk_size"        {{ type = number }}
            variable "customization"    {{ type = map(any) }}
        """)
        (workspace_dir / "main.tf").write_text(main_tf)

        # Write variables as tfvars.json so json.dump quotes/escapes every value — no input
        # (admin free-text vapp_template_id, name, password, …) can break out and inject HCL.
        tfvars = {
            "name":             config["name"],
            "network_name":     settings.VCD_NETWORK_NAME,
            "vapp_template_id": config["vapp_template_id"],
            "cpus":             config["cpus"],
            "memory":           config["memory"],
            "disk_size":        config["disk_size"],
            "customization": {
                "force":                      False,
                "change_sid":                 True,
                "allow_local_admin_password": True,
                "auto_generate_password":     False,
                "admin_password":             config["vm_password"],
                "initscript":                 "",
            },
        }
        (workspace_dir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")

    async def _run(self, *args: str, cwd: Path, on_progress=None, extra_env: dict | None = None) -> str:
        env = {**os.environ, "TF_CLI_CONFIG_FILE": str(Path("/app/terraform/terraformrc"))}
        if extra_env:
            env.update(extra_env)
        proc = await asyncio.create_subprocess_exec(
            "terraform", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        lines: list[str] = []
        last_push = asyncio.get_running_loop().time()
        async for raw in proc.stdout:
            line = raw.decode().rstrip()
            if line:
                lines.append(line)
            now = asyncio.get_running_loop().time()
            if on_progress and (now - last_push >= 15):
                on_progress("\n".join(lines[-3:]))
                last_push = now
        await proc.wait()
        output = "\n".join(lines)
        logger.debug("terraform %s:\n%s", " ".join(args), output)
        if proc.returncode != 0:
            raise TerraformError(f"terraform {args[0]} failed (exit {proc.returncode}):\n{output}")
        return output

    async def apply(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress=None,
    ) -> dict:
        workspace_dir = self._workspace_dir(workspace_id)
        cred_env = self._cred_env(api_token)
        self._write_workspace(workspace_dir, config, api_token)

        await self._run("init", "-no-color", cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
        await self._run("workspace", "select", "-or-create", workspace_id, cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)

        try:
            await self._apply(workspace_dir, on_progress, cred_env=cred_env)
        except TerraformError as exc:
            if not self._is_orphaned_vapp(str(exc), config["name"]):
                raise
            # The vApp exists in VCD but not in state (an apply created it, then a reboot killed
            # terraform before it persisted state) — refresh can't reconcile a resource it never
            # recorded. Import the orphan, destroy it (which also clears any partial VMs/networks
            # inside the vApp), and apply again from a clean slate.
            logger.warning(
                "vApp %s exists in VCD but not in state — importing, destroying, recreating",
                config["name"],
            )
            if on_progress:
                on_progress(f"Recovering orphaned vApp {config['name']}")
            import_id = f"{settings.VCD_ORG}.{settings.VCD_VDC}.{config['name']}"
            await self._run("import", "-no-color", "vcd_vapp.this", import_id, cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
            await self._destroy_state(workspace_id, workspace_dir, on_progress=on_progress, cred_env=cred_env)
            await self._apply(workspace_dir, on_progress, cred_env=cred_env)

        output_json = await self._run("output", "-json", cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
        outputs = json.loads(output_json)
        ip = outputs["primary_ip"]["value"]
        return {"ip": ip}

    async def _apply(self, workspace_dir: Path, on_progress=None, cred_env: dict | None = None) -> None:
        await self._run(
            "apply", "-auto-approve", "-no-color",
            f"-refresh={str(settings.TF_APPLY_REFRESH).lower()}",
            f"-parallelism={settings.TF_APPLY_PARALLELISM}",
            cwd=workspace_dir,
            on_progress=on_progress,
            extra_env=cred_env,
        )

    @staticmethod
    def _is_orphaned_vapp(message: str, vapp_name: str) -> bool:
        """True if `message` is a VCD 'entity already exists' conflict for our own vApp."""
        match = _ALREADY_EXISTS_RE.search(message)
        return match is not None and match.group(1) == vapp_name

    async def destroy(
        self,
        workspace_id: str,
        config: dict,
        api_token: str | None = None,
        on_progress=None,
        force: bool = False,
    ) -> None:
        workspace_dir = self._workspace_dir(workspace_id)
        cred_env = self._cred_env(api_token)
        self._write_workspace(workspace_dir, config, api_token)

        await self._run("init", "-no-color", cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
        try:
            await self._run("workspace", "select", workspace_id, cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
        except TerraformError:
            # Workspace never existed in PG — nothing was provisioned, nothing to destroy.
            logger.info("No PG state found for workspace %s, skipping destroy", workspace_id)
            shutil.rmtree(workspace_dir, ignore_errors=True)
            return

        await self._destroy_state(workspace_id, workspace_dir, on_progress=on_progress, force=force, cred_env=cred_env)
        await self._run("workspace", "select", "default", cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
        await self._run("workspace", "delete", workspace_id, cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
        shutil.rmtree(workspace_dir, ignore_errors=True)

    async def _destroy_state(
        self, workspace_id: str, workspace_dir: Path, on_progress=None, force: bool = False,
        cred_env: dict | None = None,
    ) -> None:
        """Run `terraform destroy`, recovering from a stale state lock.

        A release is a terminal teardown of an isolated, single-use per-booking workspace, so a
        lock left behind by an interrupted apply/destroy (worker killed/OOM, container restart)
        must not block it. If destroy fails to acquire the lock, force-unlock the lock id reported
        in that error and retry — re-reading the current lock each pass and tolerating a
        force-unlock that itself fails (the lock may already be gone, or its id may have changed).
        A non-lock failure, or exhausting the attempts, propagates to the task's retry/FAILED path.

        When force=True, a non-zero exit from terraform destroy is treated as a warning: the error
        is logged but execution continues so the workspace can still be deleted from the PG backend.
        Use only for admin force-release of bookings whose cloud resource is in an unrecoverable state.
        """
        attempts = 3
        for attempt in range(attempts):
            try:
                await self._run("destroy", "-auto-approve", "-no-color", cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
                return
            except TerraformError as exc:
                lock_id = self._stale_lock_id(str(exc))
                if lock_id is None or attempt == attempts - 1:
                    if force:
                        logger.warning(
                            "terraform destroy failed for %s in force mode — proceeding with workspace deletion: %s",
                            workspace_id, exc,
                        )
                        return
                    raise
                logger.warning(
                    "Stale state lock %s on %s — force-unlocking (attempt %d/%d)",
                    lock_id, workspace_id, attempt + 1, attempts - 1,
                )
                if on_progress:
                    on_progress(f"Stale state lock {lock_id} — force-unlocking")
                try:
                    await self._run("force-unlock", "-force", lock_id, cwd=workspace_dir, on_progress=on_progress, extra_env=cred_env)
                except TerraformError as unlock_exc:
                    # Lock already released, or its id changed — the next destroy re-reads the
                    # current lock state, so log and carry on rather than aborting teardown.
                    logger.warning("force-unlock of %s failed (%s); retrying destroy", lock_id, unlock_exc)

    @staticmethod
    def _stale_lock_id(message: str) -> str | None:
        """Extract the lock id from a `terraform destroy` lock-acquisition error, else None."""
        if _LOCK_ERROR_MARKER not in message:
            return None
        match = _LOCK_ID_RE.search(message)
        return match.group(1) if match else None
