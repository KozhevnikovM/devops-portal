terraform {
  required_providers {
    vcd = {
      source  = "vmware/vcd"
      version = ">=3.10.0"
    }
  }
  required_version = ">= 1.5.5"
}

resource "vcd_vapp_vm" "vm" {
  vapp_name        = var.vapp_name
  name             = var.name
  hardware_version = var.hardware_version
  vapp_template_id = var.vapp_template_id
  memory           = var.memory
  cpus             = var.cpus
  cpu_cores        = var.cpu_cores
  org              = var.org
  vdc              = var.vdc
  computer_name    = var.name

  network {
    adapter_type       = var.network_adapter_type
    type               = var.network_type
    connected          = true
    name               = var.network_name
    ip_allocation_mode = var.network_ip_allocation_mode
    is_primary         = true
  }

  dynamic "override_template_disk" {
    for_each = var.resize_disk ? [0] : []
    content {
      bus_type        = "paravirtual"
      size_in_mb      = var.disk_size
      bus_number      = 0
      unit_number     = 0
      iops            = var.disk_iops_per_gb * var.disk_size / 1024
      storage_profile = var.disk_storage_policy
    }
  }

  guest_properties = length(var.guest_properties) > 0 ? var.guest_properties : {}

  dynamic "customization" {
    for_each = length(var.customization) > 0 ? [var.customization] : []
    content {
      force                      = customization.value["force"]
      change_sid                 = customization.value["change_sid"]
      allow_local_admin_password = customization.value["allow_local_admin_password"]
      auto_generate_password     = customization.value["auto_generate_password"]
      admin_password             = customization.value["admin_password"]
      initscript                 = customization.value["initscript"]
    }
  }
}
