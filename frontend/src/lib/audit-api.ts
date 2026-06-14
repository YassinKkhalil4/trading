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

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function authHeaders(): HeadersInit {
  if (typeof window === "undefined") return { "Content-Type": "application/json" };
  const token = window.localStorage.getItem("admin_auth_token");
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`API ${response.status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export function getAuditLogs(limit = 200) {
  const params = new URLSearchParams({ limit: String(limit) });
  return fetchJson<{ audit_logs: AuditLog[] }>(`/api/v1/audit/logs?${params}`);
}
