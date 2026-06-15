"use client";

import { useLatestMarketRegime } from "@/lib/queries";

function formatRegime(value?: string | null) {
  return value?.replaceAll("_", " ") ?? "UNKNOWN";
}

function formatMultiplier(value?: number | null) {
  return typeof value === "number" ? `${value.toFixed(2)}×` : "—";
}

export function RegimeGauge({ regime, confidence }: { regime?: string; confidence?: number }) {
  const { data, error, isLoading } = useLatestMarketRegime();
  const snapshot = data?.regime;
  const activeRegime = snapshot?.market_regime ?? regime ?? "UNKNOWN";
  const activeConfidence = snapshot?.confidence ?? confidence ?? 0;
  const boundedConfidence = Math.max(0, Math.min(100, activeConfidence));

  return (
    <div className="rounded-xl border bg-slate-900 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500">Market Regime</div>
          <div className="mt-3 text-xl font-semibold text-white">{formatRegime(activeRegime)}</div>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-950 px-3 py-2 text-right">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">Risk multiplier</div>
          <div className="text-lg font-semibold text-system">{formatMultiplier(snapshot?.risk_multiplier)}</div>
        </div>
      </div>
      <div className="mt-3 h-2 rounded-full bg-slate-800" aria-label={`Regime confidence ${boundedConfidence.toFixed(0)} percent`}>
        <div className="h-2 rounded-full bg-system" style={{ width: `${boundedConfidence}%` }} />
      </div>
      <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
        <span>{isLoading ? "Hydrating latest regime…" : error ? "Unable to load latest regime" : `${boundedConfidence.toFixed(0)}% confidence`}</span>
        <span>{snapshot?.source_timestamp ? new Date(snapshot.source_timestamp).toLocaleTimeString() : "30s poll"}</span>
      </div>
      {snapshot?.reason ? <div className="mt-2 line-clamp-2 text-xs text-slate-400">{snapshot.reason}</div> : null}
    </div>
  );
}
