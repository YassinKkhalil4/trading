"use client";

import { getStrategies, type Strategy } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { FixedSizeList } from "react-window";

type Ranking = {
  symbol: string;
  score: number;
  grade: string;
  setup: string;
};

const rows: Ranking[] = Array.from({ length: 1000 }, (_, index) => ({
  symbol: ["AAPL", "NVDA", "MSFT", "AMD"][index % 4],
  score: 100 - (index % 73),
  grade: index % 5 === 0 ? "A" : "B",
  setup: "VWAP_RECLAIM",
}));

function tradingContext(strategy?: Strategy) {
  if (!strategy) return "Trading context unavailable until the strategy registry loads.";
  const maxDrawdown =
    strategy.max_drawdown_limit === null || strategy.max_drawdown_limit === undefined
      ? "not capped"
      : `${strategy.max_drawdown_limit}% max drawdown`;
  return `Requires at least ${strategy.minimum_backtest_trades} backtest trades with ${maxDrawdown}.`;
}

const helper = createColumnHelper<Ranking>();
const columns = [
  helper.accessor("symbol", { header: "Symbol" }),
  helper.accessor("score", { header: "Score" }),
  helper.accessor("grade", { header: "Grade" }),
  helper.accessor("setup", { header: "Setup" }),
];

export function AlphaRankingsGrid() {
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  const strategiesById = new Map(
    (strategies.data?.strategies ?? []).map((strategy) => [strategy.strategy_id, strategy]),
  );
  const table = useReactTable({ data: rows, columns, getCoreRowModel: getCoreRowModel() });
  const tableRows = table.getRowModel().rows;
  return (
    <div className="rounded-2xl border bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Alpha Rankings</h2>
        <span className="text-xs text-slate-500">Hover setup for trading context</span>
      </div>
      <div className="grid grid-cols-4 border-b pb-2 text-xs uppercase text-slate-500">
        {table.getHeaderGroups()[0].headers.map((header) => (
          <div key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</div>
        ))}
      </div>
      <FixedSizeList height={320} itemCount={tableRows.length} itemSize={36} width="100%">
        {({ index, style }) => {
          const row = tableRows[index];
          const strategy = strategiesById.get(row.original.setup);
          return (
            <div style={style} className="grid grid-cols-4 items-center border-b text-sm">
              {row.getVisibleCells().map((cell) => (
                <div
                  key={cell.id}
                  title={cell.column.id === "setup" ? tradingContext(strategy) : undefined}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </div>
              ))}
            </div>
          );
        }}
      </FixedSizeList>
    </div>
  );
}
