variable "project_name" {
  type    = string
  default = "trading-intelligence"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "container_image" {
  type        = string
  description = "Full image URI to deploy. CI updates ECS services after pushing to ECR."
}

variable "db_name" {
  type    = string
  default = "trading_system"
}

variable "db_username" {
  type    = string
  default = "trading"
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "admin_bootstrap_password" {
  type      = string
  sensitive = true
}

variable "api_admin_token" {
  type      = string
  sensitive = true
}

variable "admin_session_secret" {
  type        = string
  sensitive   = true
  description = "High-entropy secret used to sign admin JWT/session tokens."
}

variable "alpaca_paper_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "alpaca_paper_secret_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "alpaca_live_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "alpaca_live_secret_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "raw_archive_bucket" {
  type        = string
  description = "Globally unique S3 bucket name for raw provider payload archives."
}
