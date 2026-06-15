"use client";

import { useRiskExposures } from "@/lib/queries";

const EXPOSURE_LIMITS = {
  total: 100,
  sector: 30,
  strategy: 40,
} as const;

function toNumber(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function maxExposure(exposures: Record<string, unknown> | null | undefined): { label: string; value: number } {
  if (!exposures || Object.keys(exposures).length === 0) return { label: "None", value: 0 };
  return Object.entries(exposures).reduce(
    (current, [label, value]) => {
      const numericValue = toNumber(value);
      return numericValue > current.value ? { label, value: numericValue } : current;
    },
    { label: "None", value: 0 },
  );
}

function ExposureBar({ label, value, limit, detail }: { label: string; value: number; limit: number; detail: string }) {
  const percentOfLimit = limit > 0 ? Math.min((value / limit) * 100, 100) : 0;
  const warning = value >= limit * 0.8;

  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-3 text-xs">
        <span className="font-medium uppercase tracking-wide text-slate-400">{label}</span>
        <span className={warning ? "text-amber-200" : "text-slate-300"}>
          {value.toFixed(2)}% / {limit.toFixed(0)}%
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-slate-800" title={detail}>
        <div className={`h-full rounded-full ${warning ? "bg-amber-400" : "bg-sky-400"}`} style={{ width: `${percentOfLimit}%` }} />
      </div>
      <div className="mt-1 text-xs text-slate-500">{detail}</div>
    </div>
  );
}

export function ExposureSummary() {
  const { data, error, isLoading } = useRiskExposures(1);
  const snapshot = data?.exposure_snapshots?.[0];
  const topSector = maxExposure(snapshot?.sector_exposure);
  const topStrategy = maxExposure(snapshot?.strategy_exposure);

  return (
    <div className="rounded-xl border bg-slate-900 p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500">Exposure Summary</div>
          <h2 className="mt-1 text-lg font-semibold text-white">Portfolio Risk Envelope</h2>
        </div>
        <div className="text-right text-xs text-slate-500">
          {snapshot?.source_timestamp ? new Date(snapshot.source_timestamp).toLocaleTimeString() : "15s poll"}
        </div>
      </div>

      <div className="mt-4 space-y-4">
        {error ? <div className="rounded-lg border border-rose-800 bg-rose-950 p-3 text-sm text-rose-200">Unable to load exposures: {error.message}</div> : null}
        {isLoading ? <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm text-slate-500">Loading current exposure…</div> : null}
        {!isLoading && !snapshot && !error ? <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm text-slate-500">No exposure snapshots available.</div> : null}
        {snapshot ? (
          <>
            <ExposureBar label="Total exposure" value={toNumber(snapshot.total_exposure)} limit={EXPOSURE_LIMITS.total} detail="Gross portfolio exposure against account equity." />
            <ExposureBar label="Sector exposure" value={topSector.value} limit={EXPOSURE_LIMITS.sector} detail={`Largest sector: ${topSector.label}`} />
            <ExposureBar label="Strategy exposure" value={topStrategy.value} limit={EXPOSURE_LIMITS.strategy} detail={`Largest strategy: ${topStrategy.label}`} />
          </>
        ) : null}
      </div>
    </div>
  );
}
