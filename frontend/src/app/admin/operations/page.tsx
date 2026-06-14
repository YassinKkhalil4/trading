"use client";

import {
  activateSymbol,
  runAlphaScanner,
  runFillReconciliation,
  type ActivateSymbolRequest,
  type AlphaScannerRunRequest,
} from "@/lib/api";
import { MANUAL_OPERATIONS, runManualOperation, type ManualOperation } from "@/lib/admin-api";
import { useMutation } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";

const AUDIT_EVENT_LABEL = "MANUAL_OPERATION_RUN";
const LAST_SELECTED_RESULT_LABEL = "Last {selected?.label} result";

type OperationResult = {
  label: string;
  data: unknown;
};

function parseSymbols(value: string) {
  const symbols = value
    .split(/[\s,]+/)
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean);
  return symbols.length ? symbols : undefined;
}

export default function AdminOperationsPage() {
  const [selected, setSelected] = useState<ManualOperation | null>(null);
  const [operationResult, setOperationResult] = useState<OperationResult | null>(null);
  const [scannerStrategyId, setScannerStrategyId] = useState("vwap_reclaim");
  const [scannerSymbols, setScannerSymbols] = useState("");
  const [symbolInput, setSymbolInput] = useState("SPY");
  const [symbolName, setSymbolName] = useState("");
  const [symbolSector, setSymbolSector] = useState("");
  const [symbolReason, setSymbolReason] = useState("Activated from the admin operations dashboard.");

  const operation = useMutation({
    mutationFn: runManualOperation,
    onSuccess: (data, item) => {
      setSelected(item);
      setOperationResult({ label: item.label, data });
    },
  });
  const alphaScanner = useMutation({
    mutationFn: runAlphaScanner,
    onSuccess: (data) => setOperationResult({ label: "Alpha Scanner Run", data }),
  });
  const symbolActivation = useMutation({
    mutationFn: activateSymbol,
    onSuccess: (data) => setOperationResult({ label: "Activate Symbol", data }),
  });
  const reconciliation = useMutation({
    mutationFn: runFillReconciliation,
    onSuccess: (data) => setOperationResult({ label: "Fill Reconciliation", data }),
  });

  const currentError = useMemo(
    () => operation.error ?? alphaScanner.error ?? symbolActivation.error ?? reconciliation.error,
    [operation.error, alphaScanner.error, symbolActivation.error, reconciliation.error],
  );

  function submitAlphaScanner(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const request: AlphaScannerRunRequest = {
      strategy_id: scannerStrategyId.trim(),
      symbols: parseSymbols(scannerSymbols),
    };
    alphaScanner.mutate(request);
  }

  function submitSymbolActivation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const request: ActivateSymbolRequest = {
      symbol: symbolInput.trim().toUpperCase(),
      name: symbolName.trim() || undefined,
      sector: symbolSector.trim() || undefined,
      reason: symbolReason.trim() || "Activated from the admin operations dashboard.",
    };
    symbolActivation.mutate(request);
  }

  return (
    <main className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="rounded-2xl border bg-slate-900 p-6">
          <div className="text-sm uppercase tracking-[0.3em] text-system">Admin</div>
          <h1 className="mt-2 text-3xl font-semibold text-white">Manual Operations</h1>
          <p className="mt-2 max-w-3xl text-sm text-slate-400">
            Trigger audited backend operations from the dashboard instead of manual cURL calls. Scanner runs, symbol
            universe updates, and fill reconciliation are role-gated by the API. Audit event: {AUDIT_EVENT_LABEL}.
          </p>
        </header>

        <section className="grid gap-4 lg:grid-cols-3">
          <form onSubmit={submitAlphaScanner} className="rounded-2xl border bg-slate-900 p-4">
            <div className="text-xs uppercase text-slate-500">Alpha Engine</div>
            <h2 className="mt-2 text-xl font-semibold text-white">Run Alpha Scanner</h2>
            <label className="mt-4 block text-sm text-slate-300">Strategy ID</label>
            <input value={scannerStrategyId} onChange={(event) => setScannerStrategyId(event.target.value)} required className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
            <label className="mt-4 block text-sm text-slate-300">Symbols (optional, comma or space separated)</label>
            <input value={scannerSymbols} onChange={(event) => setScannerSymbols(event.target.value)} placeholder="SPY, NVDA, AAPL" className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
            <button type="submit" disabled={alphaScanner.isPending} className="mt-4 rounded-lg bg-system px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">
              {alphaScanner.isPending ? "Queueing..." : "Run Scanner"}
            </button>
          </form>

          <form onSubmit={submitSymbolActivation} className="rounded-2xl border bg-slate-900 p-4">
            <div className="text-xs uppercase text-slate-500">Symbol Universe</div>
            <h2 className="mt-2 text-xl font-semibold text-white">Activate Symbol</h2>
            <label className="mt-4 block text-sm text-slate-300">Symbol</label>
            <input value={symbolInput} onChange={(event) => setSymbolInput(event.target.value)} required className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <input value={symbolName} onChange={(event) => setSymbolName(event.target.value)} placeholder="Name (optional)" className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
              <input value={symbolSector} onChange={(event) => setSymbolSector(event.target.value)} placeholder="Sector (optional)" className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
            </div>
            <label className="mt-4 block text-sm text-slate-300">Audit reason</label>
            <textarea value={symbolReason} onChange={(event) => setSymbolReason(event.target.value)} rows={3} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
            <button type="submit" disabled={symbolActivation.isPending} className="mt-4 rounded-lg bg-system px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">
              {symbolActivation.isPending ? "Activating..." : "Activate Symbol"}
            </button>
          </form>

          <article className="rounded-2xl border bg-slate-900 p-4">
            <div className="text-xs uppercase text-slate-500">Reconciliation</div>
            <h2 className="mt-2 text-xl font-semibold text-white">Run Fill Reconciliation</h2>
            <p className="mt-2 text-sm text-slate-400">Sync broker fills, orders, positions, and mismatch status once.</p>
            <button type="button" onClick={() => reconciliation.mutate()} disabled={reconciliation.isPending} className="mt-4 rounded-lg bg-system px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">
              {reconciliation.isPending ? "Running..." : "Run Reconciliation"}
            </button>
          </article>
        </section>

        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {MANUAL_OPERATIONS.map((item) => (
            <article key={item.id} className="rounded-2xl border bg-slate-900 p-4">
              <div className="text-xs uppercase text-slate-500">{item.minimumRole}+ · {item.id}</div>
              <h2 className="mt-2 text-xl font-semibold text-white">{item.label}</h2>
              <p className="mt-2 min-h-16 text-sm text-slate-400">{item.description}</p>
              <button type="button" onClick={() => operation.mutate(item)} disabled={operation.isPending && selected?.id === item.id} className="mt-4 rounded-lg bg-system px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">Run</button>
            </article>
          ))}
        </section>
        {currentError ? <div className="rounded-2xl border border-risk bg-risk/10 p-4 text-risk">{(currentError as Error).message}</div> : null}
        {operationResult ? <pre aria-label={LAST_SELECTED_RESULT_LABEL} className="overflow-auto rounded-2xl border bg-slate-900 p-4 text-sm text-slate-200">Last {selected?.label ?? operationResult.label} result: {JSON.stringify(operationResult.data, null, 2)}</pre> : null}
      </div>
    </main>
  );
}
