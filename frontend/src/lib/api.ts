export type CandleTimeFrame = "1Min" | "5Min" | "15Min" | "1Hour";

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

export function getCandles(symbol: string, limit = 500, cursor?: string, timeframe: CandleTimeFrame = "1Min") {
  const params = new URLSearchParams({ symbol, limit: String(limit), timeframe });
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

export interface ReadinessStatus {
  broker_connected: boolean;
  database_sync: boolean;
  kill_switch_engaged: boolean;
  risk_limits_ok: boolean;
  reasons?: Partial<Record<keyof Omit<ReadinessStatus, "reasons">, string>>;
  checked_at?: string;
}

export function getLiveReadinessStatus() {
  return fetchJson<ReadinessStatus>("/api/v1/risk/live-readiness");
}

export function getLiveReadinessDetail() {
  return fetchJson<LiveReadinessDetail>("/api/v1/live-readiness/detail");
}

export type ActionFeedEvent = {
  id: string;
  timestamp: string;
  severity: "INFO" | "WARN" | "CRITICAL";
  entity_id?: string | null;
  message: string;
};

export function getActionFeedEvents(limit = 100) {
  return fetchJson<{ events: ActionFeedEvent[] }>(`/api/v1/events?limit=${limit}`);
}

export function getExecutionOrders(limit = 100) {
  return fetchJson<{ orders: ExecutionOrder[] }>(`/api/v1/execution/orders?limit=${limit}`);
}

export function getExecutionPositions(limit = 100) {
  return fetchJson<{ positions: ExecutionPosition[] }>(`/api/v1/execution/positions?limit=${limit}`);
}


export type Strategy = {
  strategy_id: string;
  name: string;
  version: string;
  status: string;
  minimum_backtest_trades: number;
  max_drawdown_limit?: number | null;
  evidence_quality_score?: number | null;
  [key: string]: unknown;
};

export function getStrategies() {
  return fetchJson<{ strategies: Strategy[] }>("/strategies");
}
