"use client";

import { ActionFeed } from "@/components/ActionFeed";
import { LiveReadinessMonitor } from "@/components/LiveReadinessMonitor";
import { TradingEventBridge } from "@/components/TradingEventBridge";
import { useExecutionOrders, useExecutionPositions } from "@/lib/queries";

function value(row: Record<string, unknown>, key: string) {
  const item = row[key];
  if (item === null || item === undefined) return "—";
  if (typeof item === "number") return item.toLocaleString();
  return String(item);
}

function DataTable({ title, rows, columns }: { title: string; rows: Record<string, unknown>[]; columns: string[] }) {
  return (
    <section className="rounded-2xl border bg-slate-900 p-4">
      <h2 className="mb-3 text-lg font-semibold text-white">{title}</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase text-slate-500">
            <tr>{columns.map((column) => <th key={column} className="border-b border-slate-800 px-2 py-2">{column.replaceAll("_", " ")}</th>)}</tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td className="px-2 py-4 text-slate-500" colSpan={columns.length}>No rows available.</td></tr>
            ) : rows.map((row, index) => (
              <tr key={String(row.id ?? `${title}-${index}`)} className="border-b border-slate-800/70 text-slate-300">
                {columns.map((column) => <td key={column} className="px-2 py-2">{value(row, column)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export default function TradesPage() {
  const orders = useExecutionOrders(100);
  const positions = useExecutionPositions(100);
  const orderRows = orders.data?.orders ?? [];
  const positionRows = positions.data?.positions ?? [];

  return (
    <main className="min-h-screen bg-slate-950 p-4">
      <TradingEventBridge />
      <div className="grid grid-cols-[1fr_360px] gap-4">
        <section className="space-y-4">
          <LiveReadinessMonitor />
          <DataTable title="Orders" rows={orderRows} columns={["symbol", "side", "quantity", "status", "created_at"]} />
          <DataTable title="Positions" rows={positionRows} columns={["symbol", "quantity", "average_entry_price", "market_value", "updated_at"]} />
        </section>
        <ActionFeed />
      </div>
    </main>
  );
}
