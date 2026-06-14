"use client";

import { clearAdminUserLockout, listAdminUsers, setAdminUserActive, setAdminUserRole, upsertAdminUser, type AdminRole } from "@/lib/admin-api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

const roles: AdminRole[] = ["viewer", "trader", "admin"];
const DEFAULT_REASON = "Managed from Next.js Admin Users console after Streamlit sunset.";

export default function AdminUsersPage() {
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<AdminRole>("viewer");
  const [reason, setReason] = useState(DEFAULT_REASON);
  const users = useQuery({ queryKey: ["admin-users"], queryFn: () => listAdminUsers(100) });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  const upsert = useMutation({ mutationFn: upsertAdminUser, onSuccess: invalidate });
  const changeRole = useMutation({ mutationFn: setAdminUserRole, onSuccess: invalidate });
  const changeActive = useMutation({ mutationFn: setAdminUserActive, onSuccess: invalidate });
  const unlock = useMutation({ mutationFn: clearAdminUserLockout, onSuccess: invalidate });
  const rows = users.data?.admin_users ?? [];

  return (
    <main className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="rounded-2xl border bg-slate-900 p-6">
          <div className="text-sm uppercase tracking-[0.3em] text-system">Admin</div>
          <h1 className="mt-2 text-3xl font-semibold text-white">Admin Users</h1>
          <p className="mt-2 max-w-3xl text-sm text-slate-400">Create, update, activate, deactivate, unlock, and role-manage API-backed admin users. The API hashes passwords, revokes sessions after sensitive changes, blocks self-demotion/deactivation, and writes audit events.</p>
        </header>
        <section className="grid gap-3 rounded-2xl border bg-slate-900 p-4 md:grid-cols-5">
          <input className="rounded-lg border bg-slate-950 px-3 py-2" placeholder="Username" value={username} onChange={(event) => setUsername(event.target.value)} />
          <input className="rounded-lg border bg-slate-950 px-3 py-2" placeholder="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
          <select className="rounded-lg border bg-slate-950 px-3 py-2" value={role} onChange={(event) => setRole(event.target.value as AdminRole)}>{roles.map((item) => <option key={item}>{item}</option>)}</select>
          <input className="rounded-lg border bg-slate-950 px-3 py-2" value={reason} onChange={(event) => setReason(event.target.value)} />
          <button className="rounded-lg bg-system px-4 py-2 font-semibold text-slate-950" type="button" onClick={() => upsert.mutate({ username, password, role, reason })}>Save User</button>
        </section>
        <section className="overflow-hidden rounded-2xl border bg-slate-900">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-950/60 text-xs uppercase text-slate-500"><tr><th className="px-4 py-3">Username</th><th className="px-4 py-3">Role</th><th className="px-4 py-3">Active</th><th className="px-4 py-3">Lockout</th><th className="px-4 py-3">Actions</th></tr></thead>
            <tbody className="divide-y divide-slate-800">{rows.map((user) => <tr key={user.id}><td className="px-4 py-3 text-white">{user.username}</td><td className="px-4 py-3"><select className="rounded border bg-slate-950 px-2 py-1" value={user.role} onChange={(event) => changeRole.mutate({ username: user.username, role: event.target.value as AdminRole, reason })}>{roles.map((item) => <option key={item}>{item}</option>)}</select></td><td className="px-4 py-3">{user.is_active ? "Active" : "Inactive"}</td><td className="px-4 py-3">{user.locked_until ?? "—"}</td><td className="space-x-2 px-4 py-3"><button className="rounded border px-3 py-1" onClick={() => changeActive.mutate({ username: user.username, is_active: !user.is_active, reason })}>{user.is_active ? "Deactivate Symbol".replace("Symbol", "User") : "Activate User"}</button><button className="rounded border px-3 py-1" onClick={() => unlock.mutate({ username: user.username, reason })}>Unlock</button></td></tr>)}</tbody>
          </table>
        </section>
      </div>
    </main>
  );
}
