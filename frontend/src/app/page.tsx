import { ActionFeed } from "@/components/ActionFeed";
import { AlphaRankingsGrid } from "@/components/AlphaRankingsGrid";
import { CandleChart } from "@/components/CandleChart";
import { LiveReadinessMonitor } from "@/components/LiveReadinessMonitor";
import { PositionCard } from "@/components/PositionCard";
import { RegimeGauge } from "@/components/RegimeGauge";
import { TradingEventBridge } from "@/components/TradingEventBridge";

export default function DashboardPage() {
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
            <PositionCard symbol="AAPL" quantity={1200} pnl={1.8} />
            <PositionCard symbol="NVDA" quantity={300} pnl={-0.4} />
            <RegimeGauge regime="BULL_TREND" confidence={82} />
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
