variable "vapp_name" {
  type = string
}

variable "name" {
  type = string
}

variable "hardware_version" {
  default = "vmx-19"
}

variable "vapp_template_id" {
  type = string
}

variable "memory" {
  default = 8192
}

variable "cpus" {
  default = 4
}

variable "cpu_cores" {
  default = 1
}

variable "org" {
  default = ""
}

variable "vdc" {
  default = ""
}

variable "network_adapter_type" {
  default = "VMXNET3"
}

variable "network_type" {
  default = "org"
}

variable "network_name" {
  type = string
}

variable "network_ip_allocation_mode" {
  default = "POOL"
}

variable "resize_disk" {
  type    = bool
  default = false
}

variable "disk_size" {
  default = 22384
}

variable "disk_iops_per_gb" {
  default = 10
}

variable "disk_storage_policy" {
  default = "Bronze"
}

variable "guest_properties" {
  default = {}
  type    = map(string)
}

variable "customization" {
  type    = map(string)
  default = {}
}
