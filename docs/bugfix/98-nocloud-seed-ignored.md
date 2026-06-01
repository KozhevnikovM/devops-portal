# Bugfix #98: /var/lib/cloud/seed/nocloud/user-data ignored on VM creation

## Root cause

The `initscript` approach for delivering user-data has a timing problem.

cloud-init's boot stages are:

1. **`cloud-init-local.service`** — scans for available datasources, selects one,
   writes network config.
2. **`cloud-init.service`** — processes the selected datasource's user-data.
3. **`cloud-config.service` / `cloud-final.service`** — runs cloud-config modules.

The datasource is selected in **stage 1**. At that moment the NoCloud seed directory
(`/var/lib/cloud/seed/nocloud/`) does not yet exist, so cloud-init skips `NoCloud`
and falls through to `DataSourceVMware [seed=imc]`.

The `initscript` runs in **stage 2 or 3** — after the datasource is already locked in.
By the time it writes `user-data` and `meta-data` into the NoCloud seed directory,
cloud-init will not re-scan datasources. The seed files are silently ignored.

This is confirmed by `cloud-init query --all`:

```json
"subplatform": "imc (vmware-tools)"   ← VMware selected, not NoCloud
"userdata": "file:/var/lib/cloud/instance/user-data.txt"
```

The `user-data.txt` either contains the initscript shell script itself (which VCD
embeds as user-data in the GC spec), or is empty.

## What changes

Replace the two-step approach (initscript writes seed → NoCloud picks it up) with
direct execution inside the initscript. The initscript IS executed by cloud-init as a
customization script during the VMware datasource processing — use it directly.

Two options depending on the use case:

### Option A — Shell commands directly (simplest)

For straightforward configuration, write the initscript as plain shell:

```hcl
customization = {
  # ...
  initscript = <<-EOT
    #!/bin/bash
    sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
    sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    systemctl restart sshd
  EOT
}
```

### Option B — cloud-config via `cloud-init modules` (declarative)

To retain cloud-config YAML syntax, write the config to the instance user-data file,
clear the module semaphores, and re-run the module stages:

```hcl
customization = {
  # ...
  initscript = <<-EOT
    #!/bin/bash
    cat > /var/lib/cloud/instance/user-data.txt << 'CLOUDCONFIG'
    #cloud-config
    disable_root: false
    ssh_pwauth: true
    packages:
      - nginx
    runcmd:
      - systemctl enable --now nginx
    CLOUDCONFIG
    rm -rf /var/lib/cloud/instance/sem
    cloud-init modules --mode=config
    cloud-init modules --mode=final
  EOT
}
```

**Why semaphores must be cleared**: cloud-init records completed modules in
`/var/lib/cloud/instance/sem/` as `<module>.once` files. Config modules
(e.g. `set_passwords`, `users_groups`, `runcmd`) run with `frequency: once-per-instance`
and are already marked done before the initscript executes. Without clearing the
semaphores, `cloud-init modules --mode=config` skips all of them and user-data
directives are silently ignored.

Clearing the entire `sem/` directory is safe: the config and final stage modules are
designed to be idempotent (e.g. `set_passwords` sets a value; `runcmd` replaces the
previous output). Network config and hostname are set in the `init` stage and are
unaffected.

## Expected behaviour after the fix

- `cloud-init status --long` shows `status: done`, no errors.
- `sudo cloud-init query userdata` returns the intended cloud-config.
- `/var/log/cloud-init-output.log` shows the runcmd / package install output.
- No `/var/lib/cloud/seed/nocloud/` directory is created.

## Notes

- Remove `NoCloud` from `datasource_list` in `90-datasource.cfg` — it is no longer
  needed and only adds datasource scan latency.
- The `cloud-init modules --mode=config` call in Option B is idempotent for most
  modules (modules with `frequency: once-per-instance` will not re-run if they already
  ran). Test against the specific modules needed.
