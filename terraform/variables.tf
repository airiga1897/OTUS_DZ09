variable "cloud_id" {
  type = string
}
variable "folder_id" {
  type = string
}
variable "name" {
  type = string
}
variable "subnet_zones" {
  type = map(string)
}
variable "platform" {
  type = string
}
variable "image_id" {
  type = string
}
variable "disk_type" {
  type = string
}
variable "subnets" {
  type = map(string)
}
variable "admin_cidrs" {
  type = list(string)
  validation {
    condition     = alltrue([for cidr in var.admin_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Administrative access requires individual IPv4 /32 addresses."
  }
}
variable "operator_key" {
  type = string
}
variable "ansible_key" {
  type = string
}
variable "nodes" {
  type = map(object({
    subnet = string
    host   = number
    cores  = number
    memory = number
    disk   = number
  }))
}
variable "ip_probe_zip" {
  type = string
}

output "nodes" {
  value = {
    for name, node in yandex_compute_instance.lab : name => {
      id      = node.id
      zone    = node.zone
      private = node.network_interface[0].ip_address
      public  = node.network_interface[0].nat_ip_address
    }
  }
}
