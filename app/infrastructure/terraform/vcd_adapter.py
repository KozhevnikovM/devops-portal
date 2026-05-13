import asyncio
import json
import logging
import os
import shutil
import textwrap
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


class TerraformError(Exception):
    pass


class TerraformVcdAdapter:
    """Provisions VMs on VMware Cloud Director via the terraform CLI."""

    def _workspace_dir(self, workspace_id: str) -> Path:
        return Path(settings.TF_WORKSPACES_DIR) / workspace_id

    def _write_workspace(self, workspace_dir: Path, config: dict) -> None:
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
            }}

            provider "vcd" {{}}

            module "vm" {{
              source           = "{settings.TF_MODULE_SOURCE}"
              name             = var.name
              vapp_name        = var.vapp_name
              network_name     = var.network_name
              vapp_template_id = var.vapp_template_id
              cpus             = var.cpus
              memory           = var.memory
              resize_disk      = true
              disk_size        = var.disk_size
              org              = var.org
              vdc              = var.vdc
            }}

            output "primary_ip" {{
              value = module.vm.primary_ip
            }}

            variable "name"             {{ type = string }}
            variable "vapp_name"        {{ type = string }}
            variable "network_name"     {{ type = string }}
            variable "vapp_template_id" {{ type = string }}
            variable "cpus"             {{ type = number }}
            variable "memory"           {{ type = number }}
            variable "disk_size"        {{ type = number }}
            variable "org"              {{ type = string }}
            variable "vdc"              {{ type = string }}
        """)
        (workspace_dir / "main.tf").write_text(main_tf)

        tfvars_lines = [
            f'name             = "{config["name"]}"',
            f'vapp_name        = "{settings.VCD_VAPP_NAME}"',
            f'network_name     = "{settings.VCD_NETWORK_NAME}"',
            f'vapp_template_id = "{settings.VCD_VAPP_TEMPLATE_ID}"',
            f'cpus             = {config["cpus"]}',
            f'memory           = {config["memory"]}',
            f'disk_size        = {config["disk_size"]}',
            f'org              = "{settings.VCD_ORG}"',
            f'vdc              = "{settings.VCD_VDC}"',
        ]
        (workspace_dir / "terraform.tfvars").write_text("\n".join(tfvars_lines) + "\n")

    async def _run(self, *args: str, cwd: Path) -> str:
        proc = await asyncio.create_subprocess_exec(
            "terraform", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TF_CLI_CONFIG_FILE": str(Path("/app/terraform/terraformrc"))},
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        logger.debug("terraform %s:\n%s", " ".join(args), output)
        if proc.returncode != 0:
            raise TerraformError(f"terraform {args[0]} failed (exit {proc.returncode}):\n{output}")
        return output

    async def apply(self, workspace_id: str, config: dict) -> dict:
        workspace_dir = self._workspace_dir(workspace_id)
        self._write_workspace(workspace_dir, config)

        await self._run("init", "-no-color", cwd=workspace_dir)
        await self._run("apply", "-auto-approve", "-no-color", cwd=workspace_dir)

        output_json = await self._run("output", "-json", cwd=workspace_dir)
        outputs = json.loads(output_json)
        ip = outputs["primary_ip"]["value"]
        return {"ip": ip}

    async def destroy(self, workspace_id: str) -> None:
        workspace_dir = self._workspace_dir(workspace_id)
        if not workspace_dir.exists():
            return
        await self._run("destroy", "-auto-approve", "-no-color", cwd=workspace_dir)
        shutil.rmtree(workspace_dir, ignore_errors=True)
