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


def _hcl_escape(s: str) -> str:
    """Escape a string for use inside a double-quoted HCL string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "")


def _build_initscript(user_data: str) -> str:
    """Wrap user-data in a bash script that applies it via cloud-init modules.

    Writing to the NoCloud seed directory (/var/lib/cloud/seed/nocloud/) does not
    work: the initscript runs during the VMware datasource processing (stage 2),
    after cloud-init-local has already selected the datasource (stage 1). The seed
    is written too late to be picked up. See docs/bugfix/98-nocloud-seed-ignored.md.

    Instead, overwrite the instance user-data file directly and re-run the config
    and final module stages so cloud-init applies the cloud-config in the same boot.
    """
    return (
        "#!/bin/bash\n"
        "cat > /var/lib/cloud/instance/user-data.txt << 'USERDATA'\n"
        f"{user_data}\n"
        "USERDATA\n"
        "cloud-init modules --mode=config\n"
        "cloud-init modules --mode=final\n"
    )


class TerraformVcdAdapter:
    """Provisions VMs on VMware Cloud Director via the terraform CLI."""

    def _workspace_dir(self, workspace_id: str) -> Path:
        return Path(settings.TF_WORKSPACES_DIR) / workspace_id

    def _provider_block(self, api_token: str | None = None) -> str:
        ssl = str(settings.VCD_ALLOW_UNVERIFIED_SSL).lower()
        token = api_token or settings.VCD_API_TOKEN
        if token:
            return textwrap.dedent(f"""\
                provider "vcd" {{
                  url                  = "{settings.VCD_URL}"
                  org                  = "{settings.VCD_ORG}"
                  vdc                  = "{settings.VCD_VDC}"
                  user                 = "none"
                  password             = "none"
                  auth_type            = "api_token"
                  api_token            = "{token}"
                  allow_api_token_file = true
                  max_retry_timeout    = 1800
                  allow_unverified_ssl = {ssl}
                }}""")
        return textwrap.dedent(f"""\
            provider "vcd" {{
              url                  = "{settings.VCD_URL}"
              org                  = "{settings.VCD_ORG}"
              vdc                  = "{settings.VCD_VDC}"
              user                 = "{settings.VCD_USER}"
              password             = "{settings.VCD_PASSWORD}"
              auth_type            = "integrated"
              max_retry_timeout    = 1800
              allow_unverified_ssl = {ssl}
            }}""")

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

        tfvars_lines = [
            f'name             = "{config["name"]}"',
            f'network_name     = "{settings.VCD_NETWORK_NAME}"',
            f'vapp_template_id = "{config["vapp_template_id"]}"',
            f'cpus             = {config["cpus"]}',
            f'memory           = {config["memory"]}',
            f'disk_size        = {config["disk_size"]}',
            'customization = {',
            '  force                      = false',
            '  change_sid                 = true',
            '  allow_local_admin_password = true',
            '  auto_generate_password     = false',
            f'  admin_password             = "{config["vm_password"]}"',
            f'  initscript                 = "{_hcl_escape(_build_initscript(config["user_data"])) if config.get("user_data") else ""}"',
            '}',
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

    async def apply(self, workspace_id: str, config: dict, api_token: str | None = None) -> dict:
        workspace_dir = self._workspace_dir(workspace_id)
        self._write_workspace(workspace_dir, config, api_token)

        await self._run("init", "-no-color", cwd=workspace_dir)
        await self._run("workspace", "select", "-or-create", workspace_id, cwd=workspace_dir)
        await self._run(
            "apply", "-auto-approve", "-no-color",
            f"-refresh={str(settings.TF_APPLY_REFRESH).lower()}",
            f"-parallelism={settings.TF_APPLY_PARALLELISM}",
            cwd=workspace_dir,
        )

        output_json = await self._run("output", "-json", cwd=workspace_dir)
        outputs = json.loads(output_json)
        ip = outputs["primary_ip"]["value"]
        return {"ip": ip}

    async def destroy(self, workspace_id: str, config: dict, api_token: str | None = None) -> None:
        workspace_dir = self._workspace_dir(workspace_id)
        self._write_workspace(workspace_dir, config, api_token)

        await self._run("init", "-no-color", cwd=workspace_dir)
        try:
            await self._run("workspace", "select", workspace_id, cwd=workspace_dir)
        except TerraformError:
            # Workspace never existed in PG — nothing was provisioned, nothing to destroy.
            logger.info("No PG state found for workspace %s, skipping destroy", workspace_id)
            shutil.rmtree(workspace_dir, ignore_errors=True)
            return

        await self._run("destroy", "-auto-approve", "-no-color", cwd=workspace_dir)
        await self._run("workspace", "select", "default", cwd=workspace_dir)
        await self._run("workspace", "delete", workspace_id, cwd=workspace_dir)
        shutil.rmtree(workspace_dir, ignore_errors=True)
