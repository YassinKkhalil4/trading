import { fetchJson } from "@/lib/api";

export type AuditLog = {
  id: string;
  created_at?: string | null;
  source_timestamp?: string | null;
  actor?: string | null;
  event_type?: string | null;
  entity_type?: string | null;
  entity_id?: string | null;
  reason?: string | null;
  payload?: unknown;
};

export function getAuditLogs(limit = 200) {
  const params = new URLSearchParams({ limit: String(limit) });
  return fetchJson<{ audit_logs: AuditLog[] }>(`/api/v1/audit/logs?${params}`);
}
