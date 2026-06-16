output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "enable_https" {
  value       = var.enable_https
  description = "Whether the ALB terminates TLS and redirects HTTP to HTTPS."
}

output "alb_dns_name" {
  value       = aws_lb.app.dns_name
  description = "DNS name of the application load balancer."
}

output "api_url" {
  value       = var.enable_https ? "https://${aws_lb.app.dns_name}" : "http://${aws_lb.app.dns_name}"
  description = "Primary API URL for the active ALB listener mode."
}

output "api_url_http" {
  value       = "http://${aws_lb.app.dns_name}"
  description = "HTTP API URL (redirects to HTTPS when enable_https is true)."
}

output "api_url_https" {
  value       = var.enable_https ? "https://${aws_lb.app.dns_name}" : null
  description = "HTTPS API URL when TLS is enabled; null in HTTP-only mode."
}

output "rds_endpoint" {
  value     = aws_db_instance.postgres.address
  sensitive = true
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "raw_archive_bucket" {
  value = aws_s3_bucket.raw_archive.bucket
}
