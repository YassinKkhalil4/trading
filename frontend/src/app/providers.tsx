"use client";

import { refreshSession } from "@/lib/api";
import { useAuthStore } from "@/store/use-auth-store";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 5_000, refetchOnWindowFocus: false },
        },
      }),
  );

  const expiresAt = useAuthStore((state) => state.expiresAt);
  const token = useAuthStore((state) => state.token);

  useEffect(() => {
    if (!token || !expiresAt) return;
    const refreshInMs = Math.max(new Date(expiresAt).getTime() - Date.now() - 60_000, 5_000);
    const timeout = window.setTimeout(() => {
      refreshSession().catch(() => undefined);
    }, refreshInMs);
    return () => window.clearTimeout(timeout);
  }, [expiresAt, token]);

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
