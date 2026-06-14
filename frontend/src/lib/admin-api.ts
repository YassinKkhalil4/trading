export type AdminRole = "viewer" | "trader" | "admin";

export type AdminUser = {
  id: string;
  username: string;
  role: AdminRole | string;
  is_active: boolean;
  failed_login_attempts?: number | null;
  locked_until?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type ManualOperation = {
  id: string;
  label: string;
  description: string;
  path: string;
  body?: Record<string, unknown>;
  minimumRole: "trader" | "admin";
};

export const MANUAL_OPERATIONS: ManualOperation[] = [
  {
    id: "dashboard_sync_alpaca_paper",
    label: "Sync Alpaca Paper",
    description: "Run broker fill reconciliation so paper orders, fills, positions, and sync logs match the provider.",
    path: "/reconciliation/fills/run-once",
    body: {},
    minimumRole: "trader",
  },
  {
    id: "dashboard_reconcile_fills",
    label: "Reconcile Fills",
    description: "Execute the fill reconciliation endpoint and surface mismatches through audited API results.",
    path: "/reconciliation/fills/run-once",
    body: {},
    minimumRole: "trader",
  },
  {
    id: "dashboard_run_alpaca_stream_batch",
    label: "Run Alpaca Stream Batch",
    description: "Capture a bounded Alpaca market-data websocket batch; this is not a sample-data path.",
    path: "/streams/alpaca/market-data/run-once",
    body: { symbols: [], channels: ["trades", "quotes", "bars"], max_messages: 100 },
    minimumRole: "trader",
  },
  {
    id: "dashboard_run_production_scanners",
    label: "Run Production Scanners",
    description: "Queue production scanner execution behind server-side preflight gates.",
    path: "/scanners/production/run",
    body: { symbols: [] },
    minimumRole: "trader",
  },
  {
    id: "dashboard_generate_live_readiness_report",
    label: "Generate Live Readiness Report",
    description: "Persist a fresh live-readiness report without bypassing any live trading gate.",
    path: "/live-readiness/report",
    body: {},
    minimumRole: "trader",
  },
];

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function authHeaders(): HeadersInit {
  if (typeof window === "undefined") return { "Content-Type": "application/json" };
  const token = window.localStorage.getItem("admin_auth_token");
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`API ${response.status}: ${await response.text()}`);
  return response.json() as Promise<T>;
}

export function listAdminUsers(limit = 100) {
  return fetchJson<{ admin_users: AdminUser[] }>(`/admin/users?${new URLSearchParams({ limit: String(limit) })}`);
}

export function upsertAdminUser(input: { username: string; password: string; role: AdminRole; reason: string }) {
  return fetchJson<{ admin_user: AdminUser }>("/admin/users", { method: "POST", body: JSON.stringify(input) });
}

export function setAdminUserRole(input: { username: string; role: AdminRole; reason: string }) {
  return fetchJson<{ admin_user: AdminUser }>("/admin/users/role", { method: "POST", body: JSON.stringify(input) });
}

export function setAdminUserActive(input: { username: string; is_active: boolean; reason: string }) {
  return fetchJson<{ admin_user: AdminUser }>("/admin/users/active", { method: "POST", body: JSON.stringify(input) });
}

export function clearAdminUserLockout(input: { username: string; reason: string }) {
  return fetchJson<{ admin_user: AdminUser }>("/admin/users/unlock", { method: "POST", body: JSON.stringify(input) });
}

export function runManualOperation(operation: ManualOperation) {
  return fetchJson<Record<string, unknown>>(operation.path, { method: "POST", body: JSON.stringify(operation.body ?? {}) });
}
