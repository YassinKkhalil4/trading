"use client";

import { getCandles, type CandleTimeFrame } from "@/lib/api";
import { useDashboardStore, type StrategyMarker } from "@/store/use-dashboard-store";
import { useQuery } from "@tanstack/react-query";
import { createChart, type IChartApi, type ISeriesApi, type SeriesMarker, type Time } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

const TIME_FRAMES: { label: string; queryValue: string; apiValue: CandleTimeFrame }[] = [
  { label: "1M", queryValue: "1m", apiValue: "1Min" },
  { label: "5M", queryValue: "5m", apiValue: "5Min" },
  { label: "15M", queryValue: "15m", apiValue: "15Min" },
  { label: "1H", queryValue: "1h", apiValue: "1Hour" },
];

function toChartTime(value: string): Time {
  return Math.floor(new Date(value).getTime() / 1000) as Time;
}

function markerText(marker: StrategyMarker) {
  return `${marker.side.toUpperCase()} @ ${marker.price}${marker.strategyId ? ` · ${marker.strategyId}` : ""}`;
}

export function CandleChart() {
  const symbol = useDashboardStore((state) => state.activeSymbol);
  const strategyMarkers = useDashboardStore((state) => state.strategyMarkers);
  const [selectedFrame, setSelectedFrame] = useState("1m");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markerSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const activeFrame = TIME_FRAMES.find((frame) => frame.queryValue === selectedFrame) ?? TIME_FRAMES[0];
  const { data, isLoading } = useQuery({
    queryKey: ["candles", symbol, activeFrame.apiValue],
    queryFn: () => getCandles(symbol, 500, undefined, activeFrame.apiValue),
  });

  const visibleMarkers = useMemo(
    () =>
      strategyMarkers
        .filter((marker) => marker.symbol === symbol)
        .map<SeriesMarker<Time>>((marker) => ({
          time: toChartTime(marker.timestamp),
          position: "inBar",
          color: marker.side === "buy" ? "#22c55e" : "#ef4444",
          shape: marker.side === "buy" ? "arrowUp" : "arrowDown",
          text: markerText(marker),
        })),
    [strategyMarkers, symbol],
  );

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const frame = params.get("tf");
    if (frame && TIME_FRAMES.some((option) => option.queryValue === frame)) {
      setSelectedFrame(frame);
    }
  }, []);

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
    const series = chart.addCandlestickSeries({
      upColor: "#10b981",
      downColor: "#f43f5e",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#f43f5e",
    });
    seriesRef.current = series;
    markerSeriesRef.current = chart.addLineSeries({
      color: "rgba(0, 0, 0, 0)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    return () => {
      seriesRef.current = null;
      markerSeriesRef.current = null;
      chartRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current || !data?.candles) return;
    seriesRef.current.setData(
      data.candles.map((candle) => ({
        time: toChartTime(candle.timestamp),
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  useEffect(() => {
    markerSeriesRef.current?.setData(
      strategyMarkers
        .filter((marker) => marker.symbol === symbol)
        .map((marker) => ({ time: toChartTime(marker.timestamp), value: marker.price }))
        .sort((left, right) => Number(left.time) - Number(right.time)),
    );
    markerSeriesRef.current?.setMarkers(visibleMarkers);
  }, [strategyMarkers, symbol, visibleMarkers]);

  function setTimeFrame(queryValue: string) {
    setSelectedFrame(queryValue);
    const url = new URL(window.location.href);
    url.searchParams.set("tf", queryValue);
    window.history.replaceState(null, "", `${url.pathname}?${url.searchParams.toString()}${url.hash}`);
  }

  return (
    <section className="rounded-2xl border bg-slate-950 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">{symbol} Candle Chart</h2>
        <div className="flex items-center gap-3">
          <div className="rounded-lg border border-slate-800 bg-slate-900 p-1" aria-label="Time frame toggle">
            {TIME_FRAMES.map((frame) => (
              <button
                key={frame.queryValue}
                type="button"
                onClick={() => setTimeFrame(frame.queryValue)}
                className={`rounded-md px-2 py-1 text-xs font-semibold transition ${
                  frame.queryValue === activeFrame.queryValue
                    ? "bg-emerald-500 text-slate-950"
                    : "text-slate-400 hover:bg-slate-800 hover:text-slate-100"
                }`}
              >
                {frame.label}
              </button>
            ))}
          </div>
          <span className="text-sm text-slate-500">{isLoading ? "Loading" : "Live-ready"}</span>
        </div>
      </div>
      <div ref={containerRef} />
    </section>
  );
}
