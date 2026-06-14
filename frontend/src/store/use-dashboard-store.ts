import { create } from "zustand";
import { persist } from "zustand/middleware";

type DashboardState = {
  activeSymbol: string;
  symbols: string[];
  setActiveSymbol: (symbol: string) => void;
  setSymbols: (symbols: string[]) => void;
};

export const useDashboardStore = create<DashboardState>()(
  persist(
    (set) => ({
      activeSymbol: "AAPL",
      symbols: ["AAPL", "MSFT", "NVDA", "SPY"],
      setActiveSymbol: (symbol) => set({ activeSymbol: symbol.toUpperCase() }),
      setSymbols: (symbols) =>
        set({
          symbols: symbols
            .map((symbol) => symbol.trim().toUpperCase())
            .filter((symbol, index, all) => symbol && all.indexOf(symbol) === index),
        }),
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
