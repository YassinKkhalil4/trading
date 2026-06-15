import {
  getActionFeedEvents,
  getCandles,
  getExecutionOrders,
  getExecutionPositions,
  getLiveReadinessStatus,
  getLatestMarketRegime,
  getRiskExposures,
  getStrategies,
  type CandleTimeFrame,
} from "@/lib/api";
import { useQuery } from "@tanstack/react-query";

export const queryKeys = {
  actionFeed: ["action-feed"] as const,
  candles: (symbol: string, timeframe: CandleTimeFrame) => ["candles", symbol, timeframe] as const,
  executionOrders: ["execution", "orders"] as const,
  executionPositions: ["execution", "positions"] as const,
  liveReadiness: ["live-readiness"] as const,
  marketRegime: ["market", "regime", "latest"] as const,
  riskExposures: ["risk", "exposures"] as const,
  strategies: ["strategies"] as const,
};

export function useActionFeedEvents(limit = 100) {
  return useQuery({ queryKey: [...queryKeys.actionFeed, limit], queryFn: () => getActionFeedEvents(limit), refetchInterval: 15_000 });
}

export function useCandles(symbol: string, timeframe: CandleTimeFrame, limit = 500) {
  return useQuery({ queryKey: queryKeys.candles(symbol, timeframe), queryFn: () => getCandles(symbol, limit, undefined, timeframe), refetchInterval: 10_000 });
}

export function useExecutionOrders(limit = 100) {
  return useQuery({ queryKey: [...queryKeys.executionOrders, limit], queryFn: () => getExecutionOrders(limit), refetchInterval: 15_000 });
}

export function useExecutionPositions(limit = 100) {
  return useQuery({ queryKey: [...queryKeys.executionPositions, limit], queryFn: () => getExecutionPositions(limit), refetchInterval: 15_000 });
}

export function useLiveReadinessStatus() {
  return useQuery({ queryKey: queryKeys.liveReadiness, queryFn: getLiveReadinessStatus, refetchInterval: 30_000, refetchIntervalInBackground: true });
}

export function useLatestMarketRegime() {
  return useQuery({ queryKey: queryKeys.marketRegime, queryFn: getLatestMarketRegime, refetchInterval: 30_000, refetchIntervalInBackground: true });
}

export function useOpportunityDecisions(limit = 200) {
  return useQuery({ queryKey: [...queryKeys.opportunityDecisions, limit], queryFn: () => getOpportunityDecisions(limit), refetchInterval: 10_000 });
}

export function useRiskExposures(limit = 100) {
  return useQuery({ queryKey: [...queryKeys.riskExposures, limit], queryFn: () => getRiskExposures(limit), refetchInterval: 15_000 });
}

export function useStrategies() {
  return useQuery({ queryKey: queryKeys.strategies, queryFn: getStrategies, refetchInterval: 60_000 });
}
