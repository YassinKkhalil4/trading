"use client";

import { MANUAL_OPERATIONS, runManualOperation, type ManualOperation } from "@/lib/admin-api";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

export default function AdminOperationsPage() {
  const [selected, setSelected] = useState<ManualOperation | null>(null);
  const operation = useMutation({ mutationFn: runManualOperation, onSuccess: (_data, item) => setSelected(item) });
  return (
    <main className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="rounded-2xl border bg-slate-900 p-6"><div className="text-sm uppercase tracking-[0.3em] text-system">Admin</div><h1 className="mt-2 text-3xl font-semibold text-white">Manual Operations</h1><p className="mt-2 max-w-3xl text-sm text-slate-400">Streamlit-only buttons have been ported to audited API calls in the Next.js console. Each action is role-gated server-side and records MANUAL_OPERATION_RUN or a domain-specific audit event with the authenticated actor.</p></header>
        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{MANUAL_OPERATIONS.map((item) => <article key={item.id} className="rounded-2xl border bg-slate-900 p-4"><div className="text-xs uppercase text-slate-500">{item.minimumRole}+ · {item.id}</div><h2 className="mt-2 text-xl font-semibold text-white">{item.label}</h2><p className="mt-2 min-h-16 text-sm text-slate-400">{item.description}</p><button type="button" onClick={() => operation.mutate(item)} className="mt-4 rounded-lg bg-system px-4 py-2 font-semibold text-slate-950">Run</button></article>)}</section>
        {operation.error ? <div className="rounded-2xl border border-risk bg-risk/10 p-4 text-risk">{(operation.error as Error).message}</div> : null}
        {operation.data ? <pre className="overflow-auto rounded-2xl border bg-slate-900 p-4 text-sm text-slate-200">Last {selected?.label} result: {JSON.stringify(operation.data, null, 2)}</pre> : null}
      </div>
    </main>
  );
}
