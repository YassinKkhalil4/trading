"use client";

import { getActionFeedEvents } from "@/lib/api";
import { useActionFeedStore, type ActionFeedSeverity } from "@/store/use-action-feed-store";
import { useEffect, useRef, useState } from "react";

const severityClass: Record<ActionFeedSeverity, string> = {
  CRITICAL: "bg-rose-950 text-rose-200 border-rose-800",
  WARN: "bg-amber-950 text-amber-200 border-amber-800",
  INFO: "bg-slate-900 text-slate-400 border-slate-800",
};

export function ActionFeed() {
  const events = useActionFeedStore((state) => state.events);
  const setEvents = useActionFeedStore((state) => state.setEvents);
  const [autoScroll, setAutoScroll] = useState(true);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getActionFeedEvents().then((payload) => setEvents(payload.events)).catch(() => undefined);
  }, [setEvents]);

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [autoScroll, events.length]);

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col rounded-2xl border bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-white">Action Feed</h2>
          <span className="text-xs text-slate-500">Operational heartbeat · Last 200</span>
        </div>
        <label className="flex items-center gap-2 text-xs text-slate-400">
          <input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} className="h-4 w-4 accent-sky-500" />
          Auto-scroll
        </label>
      </div>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {events.length === 0 ? <div className="rounded-lg border border-slate-800 bg-slate-950 p-3 text-sm text-slate-500">Waiting for trading events…</div> : null}
        {events.map((event) => (
          <div key={event.id} className={`rounded-lg border p-3 ${severityClass[event.severity]}`}>
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="font-semibold">{event.severity}{event.entity_id ? ` · ${event.entity_id}` : ""}</span>
              <time className="text-slate-500">{new Date(event.timestamp).toLocaleTimeString()}</time>
            </div>
            <div className="mt-1 text-sm">{event.message}</div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
