export function PositionCard({
  symbol,
  quantity,
  averagePrice,
  brokerQuantity,
  reconciliationStatus,
}: {
  symbol: string;
  quantity: number;
  averagePrice?: number | null;
  brokerQuantity?: number | null;
  reconciliationStatus?: string;
}) {
  const inSync = !reconciliationStatus || reconciliationStatus === "SYNCED";
  const quantityLabel = Math.abs(quantity) === 1 ? "share" : "shares";

  return (
    <div className="rounded-xl border bg-slate-900 p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs uppercase tracking-wide text-slate-500">Position</div>
        <span className={`rounded-full px-2 py-1 text-xs ${inSync ? "bg-emerald-950 text-emerald-200" : "bg-amber-950 text-amber-200"}`}>
          {reconciliationStatus ?? "SYNCED"}
        </span>
      </div>
      <div className="mt-2 flex items-end justify-between">
        <div>
          <div className="text-2xl font-semibold text-white">{symbol}</div>
          <div className="text-sm text-slate-400">{quantity.toLocaleString()} {quantityLabel}</div>
        </div>
        <div className="text-right text-sm text-slate-300">
          <div>{averagePrice != null ? `$${averagePrice.toFixed(2)}` : "—"}</div>
          <div className="text-xs text-slate-500">avg price</div>
        </div>
      </div>
      {brokerQuantity != null ? (
        <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-400">
          Broker quantity: <span className="text-slate-200">{brokerQuantity.toLocaleString()}</span>
        </div>
      ) : null}
    </div>
  );
}
