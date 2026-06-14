"use client";

import { useDashboardStore, type ActionFeedSeverity } from "@/store/use-dashboard-store";
import { useEffect, useRef } from "react";

const severityClass: Record<ActionFeedSeverity, string> = {
  SUCCESS: "border-emerald-500/40 text-emerald-200",
  INFO: "border-sky-500/40 text-sky-200",
  WARNING: "border-amber-500/40 text-amber-200",
  ERROR: "border-rose-500/40 text-rose-200",
};

export function ActionFeed() {
  const events = useDashboardStore((state) => state.actionFeed);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [events.length]);

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col rounded-2xl border bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-semibold text-white">Action Feed</h2>
        <span className="text-xs text-slate-500">Last 50</span>
      </div>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {events.length === 0 ? <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm text-slate-500">Waiting for execution events…</div> : null}
        {events.map((event) => (
          <div key={event.id} className={`rounded-lg border bg-slate-950/80 p-3 ${severityClass[event.severity]}`}>
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="font-semibold">{event.type}</span>
              <time className="text-slate-500">{new Date(event.timestamp).toLocaleTimeString()}</time>
            </div>
            <div className="mt-1 text-sm text-slate-300">{event.message}</div>
            {event.symbol ? <div className="mt-2 text-xs text-slate-500">Symbol: {event.symbol}</div> : null}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
