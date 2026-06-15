"use client";

import { useActionFeedEvents, useExecutionFills } from "@/lib/queries";
import { useActionFeedStore, type ActionFeedSeverity } from "@/store/use-action-feed-store";
import { useEffect, useRef, useState } from "react";


function formatPrice(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function calculateSlippageBps(fillPrice?: number | null, expectedPrice?: number | null, side?: string | null, storedSlippageBps?: number | null) {
  if (typeof expectedPrice === "number" && expectedPrice > 0 && typeof fillPrice === "number") {
    const priceDelta = side?.toLowerCase() === "sell" ? expectedPrice - fillPrice : fillPrice - expectedPrice;
    return (priceDelta / expectedPrice) * 10_000;
  }
  return typeof storedSlippageBps === "number" ? storedSlippageBps : null;
}

export function RecentFillsTable() {
  const { data, error, isLoading } = useExecutionFills(8);
  const fills = data?.fills ?? [];
  const maxSlippageBps = data?.max_slippage_bps ?? 25;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-white">Recent Fills</h2>
          <span className="text-xs text-slate-500">Execution quality · Max {maxSlippageBps.toFixed(1)} bps</span>
        </div>
        {isLoading ? <span className="text-xs text-slate-500">Refreshing…</span> : null}
      </div>
      {error ? <div className="rounded-lg border border-rose-800 bg-rose-950 p-3 text-sm text-rose-200">Unable to load fills: {error.message}</div> : null}
      {!error ? (
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-slate-800 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="pb-2 pr-4 font-medium">Symbol</th>
                <th className="pb-2 pr-4 font-medium">Side</th>
                <th className="pb-2 pr-4 text-right font-medium">Fill Price</th>
                <th className="pb-2 text-right font-medium">Slippage Bps</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {fills.length === 0 ? (
                <tr><td colSpan={4} className="py-3 text-slate-500">No recent fills.</td></tr>
              ) : fills.map((fill) => {
                const slippageBps = calculateSlippageBps(fill.price, fill.expected_price, fill.side, fill.slippage_bps);
                const breach = slippageBps !== null && Math.abs(slippageBps) > maxSlippageBps;
                return (
                  <tr key={fill.id} className="text-slate-300">
                    <td className="py-2 pr-4 font-medium text-white">{fill.symbol}</td>
                    <td className="py-2 pr-4 uppercase text-slate-400">{fill.side ?? "—"}</td>
                    <td className="py-2 pr-4 text-right font-mono">${formatPrice(fill.price)}</td>
                    <td className={`py-2 text-right font-mono ${breach ? "text-rose-300" : "text-emerald-300"}`}>{slippageBps === null ? "—" : slippageBps.toFixed(2)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

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
  const { data } = useActionFeedEvents(200);

  useEffect(() => {
    if (data?.events) setEvents(data.events);
  }, [data?.events, setEvents]);

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
