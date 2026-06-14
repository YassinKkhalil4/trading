export type Candle = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap?: number | null;
  symbol: string;
};

export type Position = {
  symbol: string;
  quantity: number;
  market_value: number;
  unrealized_pl?: number;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`API ${response.status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export function getCandles(symbol: string, limit = 500, cursor?: string) {
  const params = new URLSearchParams({ symbol, limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  return fetchJson<{ candles: Candle[]; next_cursor: string | null }>(
    `/api/v1/market/candles?${params}`,
  );
}

export type LiveReadinessGate = {
  gate_name: string;
  passed: boolean;
  status?: string;
  severity?: "warning" | "blocker" | "info" | string;
  reason?: string;
  message?: string;
  [key: string]: unknown;
};

export type LiveReadinessDetail = {
  overall_status: string;
  checked_at?: string;
  gates: LiveReadinessGate[];
  [key: string]: unknown;
};

export type ExecutionOrder = Record<string, unknown>;
export type ExecutionPosition = Record<string, unknown>;

export function getLiveReadinessDetail() {
  return fetchJson<LiveReadinessDetail>("/api/v1/live-readiness/detail");
}

export function getExecutionOrders(limit = 100) {
  return fetchJson<{ orders: ExecutionOrder[] }>(`/api/v1/execution/orders?limit=${limit}`);
}

export function getExecutionPositions(limit = 100) {
  return fetchJson<{ positions: ExecutionPosition[] }>(`/api/v1/execution/positions?limit=${limit}`);
}
