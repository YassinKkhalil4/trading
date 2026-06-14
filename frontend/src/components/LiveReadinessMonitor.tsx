"use client";

import { getLiveReadinessStatus, type ReadinessStatus } from "@/lib/api";
import { AlertTriangle, CheckCircle2, CircleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

type ReadinessKey = keyof Omit<ReadinessStatus, "reasons" | "checked_at">;

const labels: Record<ReadinessKey, string> = {
  broker_connected: "Broker connected",
  database_sync: "Database sync",
  kill_switch_engaged: "Kill switch clear",
  risk_limits_ok: "Risk limits OK",
};

export function LiveReadinessMonitor() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<ReadinessStatus | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const payload = await getLiveReadinessStatus();
        if (!cancelled) {
          setStatus(payload);
          setError(null);
        }
      } catch (refreshError) {
        if (!cancelled) setError(refreshError as Error);
      }
    }
    refresh();
    const interval = window.setInterval(refresh, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  const failures = useMemo(() => {
    if (!status) return [] as ReadinessKey[];
    return (Object.keys(labels) as ReadinessKey[]).filter((key) => status[key] === false);
  }, [status]);
  const healthy = !error && failures.length === 0;
  const pulse = error || failures.length > 0 ? "animate-pulse bg-rose-500 shadow-rose-500/60" : "bg-emerald-400 shadow-emerald-400/50";

  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="w-full rounded-2xl border border-slate-800 bg-slate-900/90 p-4 text-left">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={`h-4 w-4 rounded-full shadow-lg ${pulse}`} aria-hidden />
            <div>
              <div className="text-xs uppercase tracking-[0.25em] text-slate-500">Live Readiness</div>
              <div className={`mt-1 flex items-center gap-2 font-semibold ${healthy ? "text-emerald-200" : "text-rose-200"}`}>
                {healthy ? <CheckCircle2 className="h-5 w-5" /> : <CircleAlert className="h-5 w-5" />}
                {healthy ? "Ready for monitored trading" : "Readiness failure"}
              </div>
            </div>
          </div>
          <div className="text-right text-xs text-slate-500">{status?.checked_at ? new Date(status.checked_at).toLocaleTimeString() : "30s poll"}</div>
        </div>
      </button>
      {open ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true">
          <div className="max-h-[80vh] w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-700 bg-slate-950 shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 p-4">
              <div><h3 className="text-lg font-semibold text-white">Live Readiness Detail</h3><p className="text-sm text-slate-400">Specific blockers that prevent blind trading.</p></div>
              <button type="button" onClick={() => setOpen(false)} className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white"><X className="h-5 w-5" /></button>
            </div>
            <div className="space-y-3 p-4">
              {error ? <div className="rounded-lg border border-rose-800 bg-rose-950 p-3 text-rose-200"><AlertTriangle className="mr-2 inline h-4 w-4" />Unable to load readiness: {error.message}</div> : null}
              {(Object.keys(labels) as ReadinessKey[]).map((key) => {
                const passed = status?.[key] !== false;
                return <div key={key} className={`rounded-lg border p-3 ${passed ? "border-emerald-900 bg-emerald-950/20 text-emerald-200" : "border-rose-800 bg-rose-950 text-rose-200"}`}><div className="font-medium">{labels[key]}</div><div className="mt-1 text-sm opacity-80">{passed ? "Passing" : status?.reasons?.[key] ?? "Broker socket timeout or dependent gate failed."}</div></div>;
              })}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
