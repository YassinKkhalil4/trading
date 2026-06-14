export function RegimeGauge({ regime, confidence }: { regime: string; confidence: number }) {
  return (
    <div className="rounded-xl border bg-slate-900 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">Market Regime</div>
      <div className="mt-3 text-xl font-semibold text-white">{regime}</div>
      <div className="mt-3 h-2 rounded-full bg-slate-800">
        <div className="h-2 rounded-full bg-system" style={{ width: `${Math.max(0, Math.min(100, confidence))}%` }} />
      </div>
    </div>
  );
}
