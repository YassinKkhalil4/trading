"use client";

import type { OpportunityDecision } from "@/lib/api";
import { useOpportunityDecisions } from "@/lib/queries";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { FixedSizeList } from "react-window";

type AlphaCandidateRow = {
  id: string;
  symbol: string;
  strategyId: string;
  grade: string;
  expectancyR: number | null;
  status: "Approved" | "Blocked by Risk";
  reason: string;
  updatedAt?: string;
};

function readPayloadValue(payload: OpportunityDecision["payload"], key: string) {
  return payload && typeof payload === "object" && key in payload ? payload[key] : undefined;
}

function asString(value: unknown) {
  return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}

function asNumber(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function statusFor(decision: OpportunityDecision): AlphaCandidateRow["status"] {
  const outcome = decision.outcome.toLowerCase();
  const riskStatus = asString(readPayloadValue(decision.payload, "risk_status"))?.toLowerCase();
  const approved = readPayloadValue(decision.payload, "approved");
  if (approved === false || outcome.includes("reject") || outcome.includes("block") || riskStatus === "blocked") {
    return "Blocked by Risk";
  }
  return "Approved";
}

function toCandidateRow(decision: OpportunityDecision): AlphaCandidateRow {
  const payload = decision.payload;
  return {
    id: decision.id,
    symbol: asString(readPayloadValue(payload, "symbol")) ?? "—",
    strategyId: decision.strategy_id ?? asString(readPayloadValue(payload, "strategy_id")) ?? "—",
    grade: asString(readPayloadValue(payload, "grade")) ?? asString(readPayloadValue(payload, "opportunity_grade")) ?? "—",
    expectancyR: asNumber(readPayloadValue(payload, "expected_r") ?? readPayloadValue(payload, "expectancy_r")),
    status: statusFor(decision),
    reason: decision.reason,
    updatedAt: decision.source_timestamp ?? decision.created_at,
  };
}

function formatExpectancy(value: number | null) {
  return value == null ? "—" : `${value.toFixed(2)}R`;
}

const helper = createColumnHelper<AlphaCandidateRow>();
const columns = [
  helper.accessor("symbol", { header: "Symbol", cell: (info) => <span className="font-mono font-semibold text-slate-100">{info.getValue()}</span> }),
  helper.accessor("strategyId", { header: "Strategy ID" }),
  helper.accessor("grade", { header: "Opportunity Grade", cell: (info) => <span className="rounded bg-sky-500/15 px-1.5 py-0.5 font-semibold text-sky-200">{info.getValue()}</span> }),
  helper.accessor("expectancyR", { header: "Expectancy (R)", cell: (info) => formatExpectancy(info.getValue()) }),
  helper.accessor("status", {
    header: "Status",
    cell: (info) => {
      const approved = info.getValue() === "Approved";
      return <span className={approved ? "text-emerald-300" : "text-amber-300"}>{info.getValue()}</span>;
    },
  }),
];

export function AlphaRankingsGrid() {
  const [sorting, setSorting] = useState<SortingState>([{ id: "expectancyR", desc: true }]);
  const decisions = useOpportunityDecisions(300);
  const rows = useMemo(() => (decisions.data?.decisions ?? []).map(toCandidateRow), [decisions.data?.decisions]);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  const tableRows = table.getRowModel().rows;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Alpha Candidate Grid</h2>
          <p className="text-xs text-slate-500">Live scanner and risk-gate decisions before signal fire.</p>
        </div>
        <span className="text-xs text-slate-500">{decisions.isFetching ? "Refreshing…" : `${tableRows.length} candidates`}</span>
      </div>
      {decisions.isError ? <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">Unable to load decisions.</div> : null}
      <div className="grid grid-cols-[0.8fr_1.5fr_1.1fr_1fr_1.2fr] border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wide text-slate-500">
        {table.getHeaderGroups()[0].headers.map((header) => (
          <button key={header.id} type="button" className="pr-3 text-left hover:text-slate-300" onClick={header.column.getToggleSortingHandler()}>
            {flexRender(header.column.columnDef.header, header.getContext())}
            <span className="ml-1">{{ asc: "▲", desc: "▼" }[header.column.getIsSorted() as string] ?? ""}</span>
          </button>
        ))}
      </div>
      <FixedSizeList height={320} itemCount={tableRows.length} itemSize={30} width="100%">
        {({ index, style }) => {
          const row = tableRows[index];
          return (
            <div style={style} className="grid grid-cols-[0.8fr_1.5fr_1.1fr_1fr_1.2fr] items-center border-b border-slate-800/70 text-xs" title={row.original.reason}>
              {row.getVisibleCells().map((cell) => <div key={cell.id} className="truncate pr-3">{flexRender(cell.column.columnDef.cell, cell.getContext())}</div>)}
            </div>
          );
        }}
      </FixedSizeList>
    </div>
  );
}
