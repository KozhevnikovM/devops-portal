# Used only for `terraform providers mirror` to populate providers-mirror/.
# Not deployed — the actual provider declaration lives in modules/vapp_vm/main.tf.
terraform {
  required_providers {
    vcd = {
      source  = "vmware/vcd"
      version = ">=3.10.0"
    }
  }
}
