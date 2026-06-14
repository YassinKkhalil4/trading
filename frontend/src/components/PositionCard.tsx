export function PositionCard({
  symbol,
  quantity,
  pnl,
}: {
  symbol: string;
  quantity: number;
  pnl: number;
}) {
  const positive = pnl >= 0;
  return (
    <div className="rounded-xl border bg-slate-900 p-4 shadow-sm">
      <div className="text-xs uppercase tracking-wide text-slate-500">Position</div>
      <div className="mt-2 flex items-end justify-between">
        <div>
          <div className="text-2xl font-semibold text-white">{symbol}</div>
          <div className="text-sm text-slate-400">{quantity.toLocaleString()} shares</div>
        </div>
        <div className={positive ? "text-profit" : "text-risk"}>
          {positive ? "+" : ""}
          {pnl.toFixed(2)}%
        </div>
      </div>
    </div>
  );
}
