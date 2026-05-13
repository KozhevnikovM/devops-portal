output "primary_ip" {
  value = vcd_vapp_vm.vm.network[0].ip
}

output "hostname" {
  value = vcd_vapp_vm.vm.name
}

output "id" {
  value = vcd_vapp_vm.vm.id
}
