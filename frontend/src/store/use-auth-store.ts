import { create } from "zustand";
import { persist } from "zustand/middleware";

export type AuthSession = {
  token: string | null;
  username: string | null;
  role: string | null;
  expiresAt: string | null;
};

type AuthState = AuthSession & {
  setSession: (session: { token: string; username?: string | null; role?: string | null; expires_at?: string | null; expiresAt?: string | null }) => void;
  clearSession: () => void;
};

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      username: null,
      role: null,
      expiresAt: null,
      setSession: (session) =>
        set({
          token: session.token,
          username: session.username ?? null,
          role: session.role ?? null,
          expiresAt: session.expiresAt ?? session.expires_at ?? null,
        }),
      clearSession: () => set({ token: null, username: null, role: null, expiresAt: null }),
    }),
    {
      name: "admin-auth-session",
      onRehydrateStorage: () => (state) => {
        if (typeof window === "undefined" || !state?.token) return;
        window.localStorage.setItem("admin_auth_token", state.token);
      },
    },
  ),
);

export function getAuthToken() {
  if (typeof window === "undefined") return null;
  return useAuthStore.getState().token ?? window.localStorage.getItem("admin_auth_token");
}

export function persistAuthSession(session: { token: string; username?: string | null; role?: string | null; expires_at?: string | null; expiresAt?: string | null }) {
  useAuthStore.getState().setSession(session);
  if (typeof window !== "undefined") window.localStorage.setItem("admin_auth_token", session.token);
}

export function clearAuthSession() {
  useAuthStore.getState().clearSession();
  if (typeof window !== "undefined") window.localStorage.removeItem("admin_auth_token");
}
