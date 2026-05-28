# VCD VM Template with cloud-init: Preparation Guide

This guide explains how to create a VMware Cloud Director (VCD) VM template that uses
cloud-init, while retaining IP assignment from VCD's **Static IP Pool** via Guest
Customization (GC).

---

## How it works

When a VM is deployed from a template in VCD with **Static IP Pool**, VCD assigns an IP
address via **VMware Guest Customization (GC)**:

1. On first power-on, VCD sends a customization package to the VM via `vmtoolsd`.
2. `libdeployPkgPlugin.so` (part of `open-vm-tools`) receives the package.
3. **If cloud-init is installed**: the plugin delegates the entire customization to
   cloud-init and sets `sSkipReboot=true`. It does NOT write network config itself.
4. cloud-init reads the customization data as `DataSourceVMware [seed=imc]`.
5. cloud-init also reads `guestinfo.ovfenv` (OVF environment XML set by VCD), which
   contains `vCloud_bootproto_0`, `vCloud_ip_0`, `vCloud_gateway_0`, etc.
6. cloud-init generates network config from these properties and writes
   `/etc/sysconfig/network-scripts/ifcfg-ens192` using the `sysconfig` renderer.
7. NetworkManager activates the interface with the assigned static IP.

**Key insight**: VCD only sends `vCloud_ip_0` and related static IP properties when the
VM's NIC is set to **Static IP Pool** mode in VCD. If the NIC mode is **DHCP**, VCD
sends `vCloud_bootproto_0=dhcp` with no IP — and no static IP is assigned.

---

## Prerequisites

- `open-vm-tools` installed and `vmtoolsd` running (provides `libdeployPkgPlugin.so`)
- VCD network configured with a **Static IP Pool**
- VM NIC mode set to **Static - IP Pool** in VCD (not DHCP)
- Guest Customization enabled on the VM/vApp template

Verify the deployPkg plugin is present:
```bash
ls /usr/lib64/open-vm-tools/plugins/vmsvc/libdeployPkgPlugin.so
```

---

## Template Preparation

### Step 1 — Install cloud-init

```bash
dnf install -y cloud-init
```

### Step 2 — Set datasource

```bash
cat > /etc/cloud/cloud.cfg.d/90-datasource.cfg << 'EOF'
datasource_list: [VMware, None]
EOF
```

### Step 3 — Verify cloud.cfg settings

```bash
grep -n 'disable_vmware_customization\|distro\|renderers' /etc/cloud/cloud.cfg
```

Required values:
- `disable_vmware_customization: false` — allows GC to delegate to cloud-init
- `distro:` — must match an existing class in
  `/usr/lib/python3*/site-packages/cloudinit/distros/`. For RHEL-based systems use
  `rhel` if your distro class is not present.
- `renderers: ['sysconfig', ...]` — `sysconfig` must be first for RHEL/RHEL-based distros

If `distro: <your-distro>` has no matching Python class, change it:
```bash
sed -i 's/distro: <name>/distro: rhel/' /etc/cloud/cloud.cfg
```

### Step 4 — Prevent NM from auto-creating a DHCP fallback profile

Without this, NetworkManager creates a "Wired connection 1" DHCP profile on any
interface with no matching profile, racing with cloud-init's written config.

```bash
cat > /etc/NetworkManager/conf.d/no-auto-default.conf << 'EOF'
[main]
no-auto-default=*
EOF
```

### Step 5 — Remove the existing NM connection

The template's `System ens192` connection (with its UUID) will conflict with the
cloud-init-written ifcfg on clones. Delete it so clones start clean:

```bash
nmcli connection delete "System ens192" 2>/dev/null || true
```

### Step 6 — Test cloud-init before templating

Reboot the VM and verify:
```bash
cloud-init status --long
# Expected: status: done, detail: DataSourceVMware [seed=imc]  (or DataSourceNone if no GC ran)
# No errors in recoverable_errors
```

### Step 7 — Clean and power off

```bash
cloud-init clean --logs --seed
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
poweroff   # NOT reboot — template must be captured while powered off
```

### Step 8 — Create catalog template in VCD

1. Capture the powered-off VM as a catalog item.
2. In the catalog item properties, verify **NIC 0 IP allocation = Static - IP Pool**.

---

## VCD Deployment Requirements

Every VM deployed from the template must have:

| Setting | Value |
|---|---|
| NIC IP allocation | **Static - IP Pool** (not DHCP, not Manual) |
| Guest OS Customization | **Enabled** |
| Customize on First Boot | **Enabled** |

> These settings are checked **before** first power-on. If "Customize on First Boot" is
> not enabled, VCD will not send the GC package and cloud-init will fall back to
> `DataSourceNone` with a DHCP config.

---

## Debug Steps

### 1 — Verify the GC plugin is present

```bash
ls /usr/lib64/open-vm-tools/plugins/vmsvc/ | grep deploy
# Expected: libdeployPkgPlugin.so
```

Note: the file is named `libdeployPkgPlugin.so`, NOT `libdeployPkg.so`.

---

### 2 — Check that GC ran and delegated to cloud-init

```bash
tail -20 /var/log/vmware-imc/toolsDeployPkg.log
```

**Good output:**
```
Deployment for cloud-init succeeded.
Deployment delegated to Cloud-init. Returning success.
sSkipReboot: 'true'
Ran DeployPkg_DeployPackageFromFile successfully
```

**Bad — GC never ran:** The log file doesn't exist or has a very old timestamp.
- Check that Guest Customization + Customize on First Boot are enabled in VCD.
- Check that `libdeployPkgPlugin.so` is present.

---

### 3 — Check what VCD sent via guestinfo

```bash
vmware-rpctool "info-get guestinfo.ovfenv"
```

**Good output** (Static IP Pool):
```xml
<Property oe:key="vCloud_bootproto_0" oe:value="static"/>
<Property oe:key="vCloud_ip_0" oe:value="10.x.x.x"/>
<Property oe:key="vCloud_netmask_0" oe:value="255.255.252.0"/>
<Property oe:key="vCloud_gateway_0" oe:value="10.x.x.1"/>
<Property oe:key="vCloud_dns1_0" oe:value="x.x.x.x"/>
```

**Bad output** (NIC mode is DHCP in VCD):
```xml
<Property oe:key="vCloud_bootproto_0" oe:value="dhcp"/>
<!-- no vCloud_ip_0, no gateway, no netmask -->
```

**Fix**: In VCD, change the VM NIC allocation from **DHCP** to **Static - IP Pool**.
Then force a re-customization (Power Off → Force Guest Customization → Power On).

---

### 4 — Check cloud-init datasource and status

```bash
cloud-init status --long
```

| `detail` value | Meaning |
|---|---|
| `DataSourceVMware [seed=imc]` | GC ran and cloud-init processed it — correct |
| `DataSourceNone` | GC package was not present at boot (see step 2 and VCD settings) |
| `DataSourceVMware [seed=guestinfo]` | cloud-init read guestinfo directly (no GC package) |

---

### 5 — Check the network config cloud-init generated

```bash
cat /etc/sysconfig/network-scripts/ifcfg-ens192
```

**Good:**
```ini
# Created by cloud-init automatically, do not edit.
BOOTPROTO=none
DEVICE=ens192
IPADDR=10.x.x.x
NETMASK=255.255.252.0
GATEWAY=10.x.x.1
DNS1=x.x.x.x
ONBOOT=yes
TYPE=Ethernet
```

**Bad — DHCP written:**
```ini
BOOTPROTO=dhcp
```
This means VCD sent `vCloud_bootproto_0=dhcp` (wrong NIC mode) — see step 3.

---

### 6 — Check NetworkManager state

```bash
nmcli device status
nmcli connection show
```

`ens192` should show `connected`. If it shows `disconnected`:

```bash
# Check for conflicting connections
nmcli connection show

# Reload ifcfg and bring up connection
nmcli connection reload
nmcli connection up ens192
```

If a **"Wired connection 1"** profile exists and races to connect via DHCP:
```bash
nmcli connection delete "Wired connection 1"
# Ensure no-auto-default.conf is in place (see Step 4)
```

---

### 7 — Check cloud-init log for network config details

```bash
grep -i "network\|vmware\|datasource\|vCloud" /var/log/cloud-init.log | grep -v DEBUG | head -30
```

Key lines to look for:
- `Applying network configuration from ds ... 'type': 'dhcp'` → DHCP fallback (bad)
- `Applying network configuration from ds ... 'type': 'static'` → static IP (good)
- `config_nic.py: Debian OS not detected. Skipping the configure step` — harmless on
  RHEL-based systems; cloud-init uses the sysconfig renderer instead

---

## Common Mistakes

| Symptom | Cause | Fix |
|---|---|---|
| VM gets no IP, `DataSourceNone` | GC never ran (Customize on First Boot not enabled) | Enable in VCD before power-on |
| VM gets no IP, `DataSourceVMware [seed=imc]`, ifcfg has `BOOTPROTO=dhcp` | NIC mode is DHCP in VCD | Change NIC to Static IP Pool in VCD |
| VM gets same IP as template | GC ran but cloud-init was not installed; old HWADDR-bound ifcfg used | Install cloud-init, follow this guide |
| `cloud-init clean` + reboot still shows `DataSourceNone` | VCD only sends GC package once; reboot doesn't re-trigger | Use VCD "Force Guest Customization" or deploy a new VM |
| `nmcli connection up filename ...` failed warning in cloud-init | Old `System ens192` NM profile with stale UUID conflicts | Delete NM connection before templating (Step 5) |
| `libdeployPkg.so not found` | Wrong filename — it's `libdeployPkgPlugin.so` | Check correct path |
