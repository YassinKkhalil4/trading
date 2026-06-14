"use client";

import { getLiveReadinessDetail, type LiveReadinessGate } from "@/lib/api";
import type React from "react";
import { AlertTriangle, CheckCircle2, CircleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

type ReadinessTone = "green" | "yellow" | "red";

function gateHasWarning(gate: LiveReadinessGate) {
  const status = String(gate.status ?? "").toUpperCase();
  return status.includes("WARN") || gate.severity === "warning";
}

function readinessTone(gates: LiveReadinessGate[], overallStatus?: string): ReadinessTone {
  if (gates.some((gate) => gate.passed === false && !gateHasWarning(gate))) return "red";
  if (gates.some((gate) => gate.passed === false || gateHasWarning(gate))) return "yellow";
  const normalized = String(overallStatus ?? "").toUpperCase();
  if (normalized.includes("FAIL") || normalized.includes("BLOCK")) return "red";
  if (normalized.includes("WARN")) return "yellow";
  return "green";
}

const toneStyles: Record<ReadinessTone, { label: string; dot: string; panel: string; icon: React.ReactNode }> = {
  green: {
    label: "All checks pass",
    dot: "bg-emerald-400 shadow-emerald-400/50",
    panel: "border-emerald-500/40 text-emerald-200",
    icon: <CheckCircle2 className="h-5 w-5" />,
  },
  yellow: {
    label: "Warnings present",
    dot: "bg-amber-400 shadow-amber-400/50",
    panel: "border-amber-500/40 text-amber-200",
    icon: <AlertTriangle className="h-5 w-5" />,
  },
  red: {
    label: "Blockers present",
    dot: "bg-rose-500 shadow-rose-500/50",
    panel: "border-rose-500/40 text-rose-200",
    icon: <CircleAlert className="h-5 w-5" />,
  },
};

export function LiveReadinessMonitor() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Awaited<ReturnType<typeof getLiveReadinessDetail>> | null>(null);
  const [isFetching, setIsFetching] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function refreshReadiness() {
      setIsFetching(true);
      try {
        const detail = await getLiveReadinessDetail();
        if (!cancelled) {
          setData(detail);
          setError(null);
        }
      } catch (refreshError) {
        if (!cancelled) setError(refreshError as Error);
      } finally {
        if (!cancelled) setIsFetching(false);
      }
    }

    refreshReadiness();
    const intervalId = window.setInterval(refreshReadiness, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);
  const failedChecks = useMemo(
    () => (data?.gates ?? []).filter((gate) => gate.passed === false || gateHasWarning(gate)),
    [data?.gates],
  );
  const tone = error ? "red" : readinessTone(data?.gates ?? [], data?.overall_status);
  const styles = toneStyles[tone];

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={`w-full rounded-2xl border bg-slate-900/90 p-4 text-left ${styles.panel}`}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={`h-4 w-4 rounded-full shadow-lg ${styles.dot}`} aria-hidden />
            <div>
              <div className="text-xs uppercase tracking-[0.25em] text-slate-500">Live Readiness</div>
              <div className="mt-1 flex items-center gap-2 font-semibold">
                {styles.icon}
                {styles.label}
              </div>
            </div>
          </div>
          <div className="text-right text-xs text-slate-500">
            <div>{isFetching ? "Refreshing" : "30s poll"}</div>
            {data?.checked_at ? <div>{new Date(data.checked_at).toLocaleTimeString()}</div> : null}
          </div>
        </div>
      </button>

      {open ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="max-h-[80vh] w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-700 bg-slate-950 shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 p-4">
              <div>
                <h3 className="text-lg font-semibold text-white">Live Readiness Detail</h3>
                <p className="text-sm text-slate-400">Specific checks requiring attention before live execution.</p>
              </div>
              <button type="button" onClick={() => setOpen(false)} className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white">
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="max-h-[60vh] space-y-3 overflow-y-auto p-4">
              {error ? <div className="rounded-lg border border-rose-500/40 bg-rose-950/30 p-3 text-rose-200">Unable to load readiness detail: {error.message}</div> : null}
              {!error && failedChecks.length === 0 ? <div className="rounded-lg border border-emerald-500/40 bg-emerald-950/20 p-3 text-emerald-200">No failed checks or warnings reported.</div> : null}
              {failedChecks.map((gate) => (
                <div key={gate.gate_name} className="rounded-lg border border-slate-800 bg-slate-900 p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-medium text-white">{gate.gate_name}</div>
                    <span className={gateHasWarning(gate) ? "text-amber-300" : "text-rose-300"}>{gateHasWarning(gate) ? "WARNING" : "BLOCKER"}</span>
                  </div>
                  <div className="mt-2 text-sm text-slate-400">{gate.reason ?? gate.message ?? "No detail supplied."}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
