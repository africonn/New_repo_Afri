# ============================================================
# sap_auth_additions.tf
# Add to your terraform/ folder — extends compute.tf
# Adds SAP OAuth env vars to App Runner + new Secrets
# ============================================================

variable "sap_client_id" {
  description = "SAP OAuth2 client ID from app registration"
  default     = ""
}

variable "sap_client_secret" {
  description = "SAP OAuth2 client secret"
  sensitive   = true
  default     = ""
}

variable "sap_token_url" {
  description = "SAP OAuth2 token endpoint"
  default     = "https://api.ariba.com/v2/oauth/token"
}

variable "sap_redirect_uri" {
  description = "Your App Runner callback URL — set after first deploy"
  default     = ""
}

variable "supplier_id" {
  description = "Your supplier ID on the SAP/Ariba network"
  default     = "AFRICONN"
}

resource "aws_secretsmanager_secret" "sap_tokens" {
  name                    = "africonn/sap-tokens"
  description             = "SAP OAuth2 token bundle — auto-managed by auth handler"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "sap_tokens_placeholder" {
  secret_id     = aws_secretsmanager_secret.sap_tokens.id
  secret_string = jsonencode({
    status    = "pending_authorization"
    message   = "DC must complete OAuth authorization at /sap/callback"
    stored_at = "1970-01-01T00:00:00+00:00"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "sap_credentials" {
  name                    = "africonn/sap-credentials"
  description             = "SAP OAuth2 client_id and client_secret"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "sap_credentials" {
  secret_id = aws_secretsmanager_secret.sap_credentials.id
  secret_string = jsonencode({
    client_id     = var.sap_client_id
    client_secret = var.sap_client_secret
    token_url     = var.sap_token_url
    redirect_uri  = var.sap_redirect_uri
  })
}

output "sap_callback_url" {
  description = "Give this URL to the DC to register as OAuth redirect URI"
  value       = "https://${aws_apprunner_service.mcp.service_url}/sap/callback"
}

output "sap_auth_status_url" {
  description = "Check SAP connection status here"
  value       = "https://${aws_apprunner_service.mcp.service_url}/sap/status"
}

output "sap_tokens_secret_arn" {
  description = "ARN of the SAP tokens secret in Secrets Manager"
  value       = aws_secretsmanager_secret.sap_tokens.arn
}
