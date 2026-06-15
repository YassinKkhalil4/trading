"use client";

import { ActionFeed } from "@/components/ActionFeed";
import { AlphaRankingsGrid } from "@/components/AlphaRankingsGrid";
import { CandleChart } from "@/components/CandleChart";
import { ExposureSummary } from "@/components/ExposureSummary";
import { LiveReadinessMonitor } from "@/components/LiveReadinessMonitor";
import { PositionCard } from "@/components/PositionCard";
import { RegimeGauge } from "@/components/RegimeGauge";
import { TradingEventBridge } from "@/components/TradingEventBridge";
import { useExecutionPositions } from "@/lib/queries";

export default function DashboardPage() {
  const { data, error, isLoading } = useExecutionPositions(100);
  const positions = data?.positions ?? [];

  return (
    <main className="min-h-screen bg-slate-950">
      <TradingEventBridge />
      <header className="sticky top-0 z-10 border-b bg-slate-950/95 px-6 py-3 backdrop-blur">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm uppercase tracking-[0.3em] text-system">
              Institutional Terminal
            </div>
            <h1 className="text-xl font-semibold text-white">Trading Operations</h1>
          </div>
          <div className="rounded-lg border bg-slate-900 px-3 py-2 text-sm text-slate-400">
            ⌘K Command Bar · Flatten All guarded by RBAC
          </div>
        </div>
      </header>
      <div className="grid grid-cols-[72px_1fr_360px] gap-4 p-4">
        <nav className="rounded-2xl border bg-slate-900 p-3 text-xs text-slate-400">
          <div className="mb-4 text-white">ATI</div>
          <div className="space-y-4">
            <div>Overview</div>
            <div>Trades</div>
            <div>Journal</div>
            <div>Admin</div>
          </div>
        </nav>
        <section className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            <ExposureSummary />
            <RegimeGauge regime="BULL_TREND" confidence={82} />
            <div className="rounded-xl border bg-slate-900 p-4 shadow-sm">
              <div className="text-xs uppercase tracking-wide text-slate-500">Positions</div>
              <div className="mt-2 text-2xl font-semibold text-white">{positions.length}</div>
              <div className="text-sm text-slate-400">Live execution book rows</div>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-3">
            {error ? <div className="rounded-xl border border-rose-800 bg-rose-950 p-4 text-sm text-rose-200">Unable to load positions: {error.message}</div> : null}
            {isLoading ? <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-500">Loading execution positions…</div> : null}
            {!isLoading && positions.length === 0 && !error ? <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-500">No open execution positions.</div> : null}
            {positions.map((position) => (
              <PositionCard
                key={`${position.environment_mode ?? "book"}-${position.symbol}`}
                symbol={position.symbol}
                quantity={position.quantity}
                averagePrice={position.average_price}
                brokerQuantity={position.broker_quantity}
                reconciliationStatus={position.reconciliation_status}
              />
            ))}
          </div>
          <CandleChart />
          <AlphaRankingsGrid />
        </section>
        <aside className="space-y-3">
          <LiveReadinessMonitor />
          <ActionFeed />
        </aside>
      </div>
    </main>
  );
}
