mock_provider "yandex" {}

variables {
  cloud_id     = "mockcloud"
  folder_id    = "mockfolder"
  name         = "otus-dz09-test"
  subnet_zones = { client = "ru-central1-d", web = "ru-central1-a", web-d = "ru-central1-d" }
  platform     = "standard-v3"
  image_id     = "mockimage"
  disk_type    = "network-hdd"
  subnets      = { client = "10.42.2.0/24", web = "10.42.1.0/24", web-d = "10.42.3.0/24" }
  admin_cidrs  = ["8.8.8.8/32"]
  operator_key = "ssh-ed25519 mock-operator"
  ansible_key  = "ssh-ed25519 mock-ansible"
  ip_probe_zip = "variables.tf"
  nodes = {
    web-a  = { subnet = "web", host = 11, cores = 2, memory = 4, disk = 10 }
    web-b  = { subnet = "web", host = 12, cores = 2, memory = 4, disk = 10 }
    web-c  = { subnet = "web-d", host = 13, cores = 2, memory = 4, disk = 20 }
    client = { subnet = "client", host = 10, cores = 2, memory = 4, disk = 20 }
  }
}

run "private_web_nodes_and_single_ingress" {
  command = plan

  assert {
    condition = toset(keys(yandex_compute_instance.lab)) == toset(["web-a", "web-b", "web-c", "client"]) && alltrue([
      for name, vm in yandex_compute_instance.lab : vm.network_interface[0].nat == (name == "client")
    ])
    error_message = "Expected three private web VMs and one public controller."
  }

  assert {
    condition = toset([
      for rule in yandex_vpc_security_group.client.ingress : rule.port
      if contains(rule.v4_cidr_blocks, "8.8.8.8/32")
    ]) == toset([22, 80, 443])
    error_message = "Only SSH, HTTP and HTTPS are available to the operator IPv4."
  }

  assert {
    condition     = yandex_compute_instance_iam_binding.verify.role == "compute.operator"
    error_message = "The verification account may operate only the test VM."
  }
}

run "reject_unconfirmed_operator" {
  command = plan
  variables {
    admin_cidrs = []
  }
  expect_failures = [yandex_vpc_security_group.client]
}
