import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ActionFeedSeverity = "SUCCESS" | "INFO" | "WARNING" | "ERROR";

export type StrategyMarker = {
  id: string;
  symbol: string;
  side: "buy" | "sell";
  price: number;
  timestamp: string;
  strategyId?: string;
};

export type ActionFeedEvent = {
  id: string;
  type: string;
  severity: ActionFeedSeverity;
  message: string;
  timestamp: string;
  symbol?: string;
  payload?: Record<string, unknown>;
};

type DashboardState = {
  activeSymbol: string;
  symbols: string[];
  actionFeed: ActionFeedEvent[];
  strategyMarkers: StrategyMarker[];
  setActiveSymbol: (symbol: string) => void;
  setSymbols: (symbols: string[]) => void;
  pushActionFeedEvent: (event: Omit<ActionFeedEvent, "id" | "timestamp"> & { id?: string; timestamp?: string }) => void;
  pushStrategyMarker: (marker: Omit<StrategyMarker, "id"> & { id?: string }) => void;
};

export const useDashboardStore = create<DashboardState>()(
  persist(
    (set) => ({
      activeSymbol: "AAPL",
      symbols: ["AAPL", "MSFT", "NVDA", "SPY"],
      actionFeed: [],
      strategyMarkers: [],
      setActiveSymbol: (symbol) => set({ activeSymbol: symbol.toUpperCase() }),
      setSymbols: (symbols) =>
        set({
          symbols: symbols
            .map((symbol) => symbol.trim().toUpperCase())
            .filter((symbol, index, all) => symbol && all.indexOf(symbol) === index),
        }),
      pushActionFeedEvent: (event) =>
        set((state) => ({
          actionFeed: [
            ...state.actionFeed,
            {
              ...event,
              id: event.id ?? `${event.type}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
              timestamp: event.timestamp ?? new Date().toISOString(),
            },
          ].slice(-50),
        })),
      pushStrategyMarker: (marker) =>
        set((state) => ({
          strategyMarkers: [
            ...state.strategyMarkers.filter((existing) => existing.id !== marker.id),
            {
              ...marker,
              id: marker.id ?? `${marker.symbol}-${marker.side}-${marker.timestamp}-${Date.now()}`,
            },
          ].slice(-200),
        })),
    }),
    {
      name: "institutional-trading-dashboard",
      partialize: (state) => ({
        activeSymbol: state.activeSymbol,
        symbols: state.symbols,
      }),
    },
  ),
);
