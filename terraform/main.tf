terraform {
  required_version = ">= 1.6, < 2.0"
  backend "local" {}
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "0.244.0"
    }
  }
}

provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
}

resource "yandex_vpc_network" "lab" {
  name = var.name
}

resource "yandex_vpc_gateway" "lab" {
  name = "${var.name}-nat"
  shared_egress_gateway {}
}

resource "yandex_vpc_route_table" "lab" {
  name       = "${var.name}-egress"
  network_id = yandex_vpc_network.lab.id

  static_route {
    destination_prefix = "0.0.0.0/0"
    gateway_id         = yandex_vpc_gateway.lab.id
  }
}

resource "yandex_vpc_subnet" "lab" {
  for_each       = var.subnets
  name           = "${var.name}-${each.key}"
  zone           = var.subnet_zones[each.key]
  network_id     = yandex_vpc_network.lab.id
  route_table_id = each.key != "client" ? yandex_vpc_route_table.lab.id : null
  v4_cidr_blocks = [each.value]
}

resource "yandex_vpc_security_group" "client" {
  name       = "${var.name}-client"
  network_id = yandex_vpc_network.lab.id

  lifecycle {
    precondition {
      condition     = length(var.admin_cidrs) > 0
      error_message = "Confirm the operator IPv4 before creating the lab."
    }
  }

  dynamic "ingress" {
    for_each = toset([22, 80, 443])
    content {
      protocol       = "TCP"
      port           = ingress.value
      v4_cidr_blocks = var.admin_cidrs
    }
  }

  dynamic "ingress" {
    for_each = { "gossip-tcp" = ["TCP", 8301], "gossip-udp" = ["UDP", 8301], "db" = ["TCP", 3306] }
    content {
      protocol       = ingress.value[0]
      port           = tonumber(ingress.value[1])
      v4_cidr_blocks = [for name, cidr in var.subnets : cidr if name != "client"]
    }
  }

  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_vpc_security_group" "web" {
  name       = "${var.name}-web"
  network_id = yandex_vpc_network.lab.id

  dynamic "ingress" {
    for_each = toset([22, 443])
    content {
      protocol          = "TCP"
      port              = ingress.value
      security_group_id = yandex_vpc_security_group.client.id
    }
  }

  ingress {
    protocol       = "TCP"
    port           = 8300
    v4_cidr_blocks = values(var.subnets)
  }

  dynamic "ingress" {
    for_each = toset(["TCP", "UDP"])
    content {
      protocol       = ingress.value
      port           = 8301
      v4_cidr_blocks = values(var.subnets)
    }
  }

  dynamic "ingress" {
    for_each = toset(["TCP", "UDP"])
    content {
      protocol       = ingress.value
      port           = 8302
      v4_cidr_blocks = [for name, cidr in var.subnets : cidr if name != "client"]
    }
  }

  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_iam_service_account" "verify" {
  name      = "${var.name}-verify"
  folder_id = var.folder_id
}

resource "yandex_compute_instance" "lab" {
  for_each           = var.nodes
  name               = "${var.name}-${each.key}"
  zone               = var.subnet_zones[each.value.subnet]
  platform_id        = var.platform
  service_account_id = each.key == "client" ? yandex_iam_service_account.verify.id : null
  labels             = { otus_lab = var.name }

  resources {
    cores         = each.value.cores
    memory        = each.value.memory
    core_fraction = 100
  }

  boot_disk {
    initialize_params {
      name     = "${var.name}-${each.key}"
      size     = each.value.disk
      type     = var.disk_type
      image_id = var.image_id
    }
    auto_delete = true
  }

  network_interface {
    subnet_id  = yandex_vpc_subnet.lab[each.value.subnet].id
    ip_address = cidrhost(var.subnets[each.value.subnet], each.value.host)
    nat        = each.key == "client"
    security_group_ids = [
      each.key == "client" ? yandex_vpc_security_group.client.id : yandex_vpc_security_group.web.id
    ]
  }

  metadata = {
    ssh-keys = "ubuntu:${each.key == "client" ? var.operator_key : var.ansible_key}"
  }

  metadata_options {
    aws_v1_http_endpoint = 1
    aws_v1_http_token    = 2
    gce_http_endpoint    = 1
    gce_http_token       = each.key == "client" ? 1 : 2
  }
}

resource "yandex_compute_instance_iam_binding" "verify" {
  instance_id = yandex_compute_instance.lab["web-b"].id
  role        = "compute.operator"
  members     = ["serviceAccount:${yandex_iam_service_account.verify.id}"]
}

resource "yandex_function" "ip_probe" {
  name              = "${var.name}-ip-probe"
  folder_id         = var.folder_id
  labels            = { otus_lab = var.name }
  runtime           = "python312"
  entrypoint        = "index.handler"
  memory            = 128
  execution_timeout = 5
  user_hash         = filesha256(var.ip_probe_zip)

  content {
    zip_filename = var.ip_probe_zip
  }

  log_options {
    disabled = true
  }
}
