"use client";

import { getAuditLogs, type AuditLog } from "@/lib/audit-api";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

function summarizePayload(payload: unknown) {
  if (payload == null) return "—";
  const serialized = JSON.stringify(payload);
  if (!serialized) return "—";
  return serialized.length > 96 ? `${serialized.slice(0, 96)}…` : serialized;
}

function JsonPayloadModal({ log, onClose }: { log: AuditLog; onClose: () => void }) {
  const formattedPayload = useMemo(() => JSON.stringify(log.payload ?? {}, null, 2), [log.payload]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-6 backdrop-blur">
      <section className="max-h-[85vh] w-full max-w-4xl overflow-hidden rounded-2xl border bg-slate-900 shadow-2xl">
        <header className="flex items-start justify-between gap-4 border-b p-5">
          <div>
            <div className="text-xs uppercase tracking-[0.3em] text-system">Audit payload</div>
            <h2 className="mt-2 text-xl font-semibold text-white">{log.event_type ?? "Audit event"}</h2>
            <p className="mt-1 text-sm text-slate-400">
              {log.actor ?? "system"} · {formatDate(log.created_at ?? log.source_timestamp)}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border px-3 py-2 text-sm text-slate-200 transition hover:bg-slate-800"
          >
            Close
          </button>
        </header>
        <div className="max-h-[60vh] overflow-auto p-5">
          <pre className="whitespace-pre-wrap rounded-xl bg-slate-950 p-4 font-mono text-sm leading-6 text-slate-100">
            {formattedPayload}
          </pre>
        </div>
      </section>
    </div>
  );
}

export default function AuditLogPage() {
  const [selectedLog, setSelectedLog] = useState<AuditLog | null>(null);
  const { data, error, isLoading, isFetching } = useQuery({
    queryKey: ["audit-logs"],
    queryFn: () => getAuditLogs(200),
    refetchInterval: 30_000,
  });
  const logs = data?.audit_logs ?? [];

  return (
    <main className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-col justify-between gap-4 rounded-2xl border bg-slate-900 p-6 md:flex-row md:items-center">
          <div>
            <div className="text-sm uppercase tracking-[0.3em] text-system">Admin</div>
            <h1 className="mt-2 text-3xl font-semibold text-white">Audit Logs</h1>
            <p className="mt-2 max-w-3xl text-sm text-slate-400">
              Review manual operations, system triggers, and the structured JSON payloads that explain
              each audited action.
            </p>
          </div>
          <div className="rounded-xl border bg-slate-950 px-4 py-3 text-sm text-slate-300">
            {isFetching ? "Refreshing…" : `${logs.length.toLocaleString()} latest events`}
          </div>
        </header>

        {error ? (
          <div className="rounded-2xl border border-risk bg-risk/10 p-4 text-risk">
            {(error as Error).message}
          </div>
        ) : null}

        <section className="overflow-hidden rounded-2xl border bg-slate-900">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-left text-sm">
              <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-3">Timestamp</th>
                  <th className="px-4 py-3">Actor</th>
                  <th className="px-4 py-3">Event</th>
                  <th className="px-4 py-3">Entity</th>
                  <th className="px-4 py-3">Reason</th>
                  <th className="px-4 py-3">Payload</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {isLoading ? (
                  <tr>
                    <td className="px-4 py-8 text-center text-slate-400" colSpan={6}>
                      Loading audit logs…
                    </td>
                  </tr>
                ) : logs.length === 0 ? (
                  <tr>
                    <td className="px-4 py-8 text-center text-slate-400" colSpan={6}>
                      No audit logs found.
                    </td>
                  </tr>
                ) : (
                  logs.map((log) => (
                    <tr key={log.id} className="align-top transition hover:bg-slate-800/50">
                      <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                        {formatDate(log.created_at ?? log.source_timestamp)}
                      </td>
                      <td className="px-4 py-3 text-white">{log.actor ?? "system"}</td>
                      <td className="px-4 py-3">
                        <span className="rounded-full border border-system/40 bg-system/10 px-2 py-1 text-xs text-system">
                          {log.event_type ?? "UNKNOWN"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-300">
                        <div>{log.entity_type ?? "—"}</div>
                        <div className="text-xs text-slate-500">{log.entity_id ?? "—"}</div>
                      </td>
                      <td className="max-w-sm px-4 py-3 text-slate-300">{log.reason ?? "—"}</td>
                      <td className="max-w-md px-4 py-3">
                        <button
                          type="button"
                          onClick={() => setSelectedLog(log)}
                          className="rounded-lg border bg-slate-950 px-3 py-2 text-left font-mono text-xs text-slate-300 transition hover:border-system hover:text-white"
                        >
                          {summarizePayload(log.payload)}
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
      {selectedLog ? <JsonPayloadModal log={selectedLog} onClose={() => setSelectedLog(null)} /> : null}
    </main>
  );
}
