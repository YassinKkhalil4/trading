output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "api_url" {
  value = "http://${aws_lb.app.dns_name}"
}

output "dashboard_url" {
  value = "http://${aws_lb.app.dns_name}:8501"
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
