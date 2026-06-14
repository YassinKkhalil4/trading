"use client";

import { useDashboardStore } from "@/store/use-dashboard-store";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

type TradingEvent = {
  type: string;
  payload?: {
    symbol?: string;
    [key: string]: unknown;
  };
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function websocketUrl() {
  const url = new URL(API_BASE);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/api/v1/stream";
  url.search = "";
  return url.toString();
}

export function TradingEventBridge() {
  const queryClient = useQueryClient();
  const activeSymbol = useDashboardStore((state) => state.activeSymbol);

  useEffect(() => {
    const socket = new WebSocket(websocketUrl());
    socket.onmessage = (message) => {
      const event = JSON.parse(message.data) as TradingEvent;
      if (event.type === "MARKET_DATA_CANDLE" && event.payload?.symbol) {
        queryClient.invalidateQueries({ queryKey: ["candles", event.payload.symbol] });
      }
      if (["ORDER_STATUS", "FILL", "SIGNAL_UPDATE"].includes(event.type)) {
        queryClient.invalidateQueries();
      }
    };
    return () => socket.close();
  }, [activeSymbol, queryClient]);

  return null;
}
