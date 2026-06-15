import { clearAuthSession, getAuthToken, persistAuthSession } from "@/store/use-auth-store";

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

type RequestOptions = RequestInit & { retryOnUnauthorized?: boolean };

function authHeaders(): HeadersInit {
  const token = getAuthToken();
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function refreshAuthToken() {
  const token = getAuthToken();
  if (!token) return false;
  const response = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    clearAuthSession();
    return false;
  }
  persistAuthSession(await response.json());
  return true;
}

export async function fetchJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { retryOnUnauthorized = true, headers, ...requestOptions } = options;
  const response = await fetch(`${API_BASE}${path}`, {
    ...requestOptions,
    headers: { ...authHeaders(), ...headers },
    cache: "no-store",
  });
  if (response.status === 401 && retryOnUnauthorized && (await refreshAuthToken())) {
    return fetchJson<T>(path, { ...options, retryOnUnauthorized: false });
  }
  if (!response.ok) {
    throw new Error(`API ${response.status}: ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export function refreshSession() {
  return refreshAuthToken();
}

export function getCandles(symbol: string, limit = 500, cursor?: string, timeframe: CandleTimeFrame = "1Min") {
  const params = new URLSearchParams({ symbol, limit: String(limit), timeframe });
  if (cursor) params.set("cursor", cursor);
  return fetchJson<{ candles: Candle[]; next_cursor: string | null }>(
    `/api/v1/market/candles?${params}`,
  );
}


export type MarketRegimeSnapshot = {
  id?: string;
  market_regime: string;
  confidence: number;
  allowed_bias?: string | null;
  risk_multiplier: number;
  breakout_permission?: boolean;
  mean_reversion_permission?: string | null;
  reason?: string | null;
  source_timestamp?: string;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
};

export function getLatestMarketRegime() {
  return fetchJson<{ regime: MarketRegimeSnapshot | null }>("/api/v1/market/regime/latest");
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
export type ExecutionPosition = {
  id?: string;
  symbol: string;
  quantity: number;
  average_price?: number | null;
  broker_quantity?: number | null;
  broker_average_price?: number | null;
  reconciliation_status?: string;
  environment_mode?: string;
  source_timestamp?: string;
  updated_at?: string;
  [key: string]: unknown;
};

export type ExposureSnapshot = {
  id?: string;
  account_equity: number;
  total_exposure: number;
  sector_exposure: Record<string, number>;
  strategy_exposure: Record<string, number>;
  symbol_exposure: Record<string, number>;
  source_timestamp?: string;
  reason?: string | null;
  [key: string]: unknown;
};

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

export function getRiskExposures(limit = 100) {
  return fetchJson<{ exposure_snapshots: ExposureSnapshot[] }>(`/api/v1/risk/exposures?limit=${limit}`);
}


export type OpportunityDecision = {
  id: string;
  decision_type: string;
  outcome: string;
  entity_type?: string | null;
  entity_id?: string | null;
  strategy_id?: string | null;
  rule_version?: string | null;
  reason: string;
  payload?: Record<string, unknown> | null;
  source_timestamp?: string;
  created_at?: string;
  [key: string]: unknown;
};

export function getOpportunityDecisions(limit = 200) {
  return fetchJson<{ decisions: OpportunityDecision[] }>(`/api/v1/decisions?limit=${limit}`);
}

export interface AlphaScannerRunRequest {
  strategy_id: string;
  symbols?: string[];
}

export interface AlphaScannerRunResponse {
  accepted: boolean;
  task_id: string;
  reason: string;
}

export interface ActivateSymbolRequest {
  symbol: string;
  name?: string;
  sector?: string;
  reason?: string;
}

export type ActivateSymbolResponse = Record<string, unknown>;

export interface FillReconciliationResponse {
  success?: boolean;
  ok?: boolean;
  reason?: string;
  mismatch_detected?: boolean;
  [key: string]: unknown;
}

export function runAlphaScanner(request: AlphaScannerRunRequest) {
  return fetchJson<AlphaScannerRunResponse>("/api/v1/alpha/scanners/run", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function activateSymbol(request: ActivateSymbolRequest) {
  return fetchJson<ActivateSymbolResponse>("/api/v1/symbols/activate", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function runFillReconciliation() {
  return fetchJson<FillReconciliationResponse>("/api/v1/reconciliation/fills/run-once", {
    method: "POST",
    body: JSON.stringify({}),
  });
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
