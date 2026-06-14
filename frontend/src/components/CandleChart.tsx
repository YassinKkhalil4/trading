"use client";

import { getCandles } from "@/lib/api";
import { useDashboardStore } from "@/store/use-dashboard-store";
import { useQuery } from "@tanstack/react-query";
import { createChart, type IChartApi } from "lightweight-charts";
import { useEffect, useRef } from "react";

export function CandleChart() {
  const symbol = useDashboardStore((state) => state.activeSymbol);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const { data, isLoading } = useQuery({
    queryKey: ["candles", symbol],
    queryFn: () => getCandles(symbol, 500),
  });

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      height: 420,
      layout: { background: { color: "#020617" }, textColor: "#cbd5e1" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      rightPriceScale: { borderColor: "#334155" },
      timeScale: { borderColor: "#334155" },
    });
    chartRef.current = chart;
    return () => chart.remove();
  }, []);

  useEffect(() => {
    if (!chartRef.current || !data?.candles) return;
    const series = chartRef.current.addCandlestickSeries({
      upColor: "#10b981",
      downColor: "#f43f5e",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#f43f5e",
    });
    series.setData(
      data.candles.map((candle) => ({
        time: Math.floor(new Date(candle.timestamp).getTime() / 1000) as never,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
      })),
    );
    chartRef.current.timeScale().fitContent();
    return () => chartRef.current?.removeSeries(series);
  }, [data]);

  return (
    <section className="rounded-2xl border bg-slate-950 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">{symbol} Candle Chart</h2>
        <span className="text-sm text-slate-500">{isLoading ? "Loading" : "Live-ready"}</span>
      </div>
      <div ref={containerRef} />
    </section>
  );
}
