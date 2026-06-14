"use client";

import { useDashboardStore, type ActionFeedSeverity } from "@/store/use-dashboard-store";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

type TradingEvent = {
  type: string;
  payload?: {
    symbol?: string;
    message?: string;
    reason?: string;
    status?: string;
    order_id?: string;
    id?: string;
    [key: string]: unknown;
  };
  timestamp?: string;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const FEED_EVENT_TYPES = new Set(["ORDER_SUBMITTED", "FILL_RECEIVED", "EXECUTION_ERROR"]);

function websocketUrl() {
  const url = new URL(API_BASE);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/api/v1/stream";
  url.search = "";
  return url.toString();
}

function eventSeverity(event: TradingEvent): ActionFeedSeverity {
  if (event.type === "EXECUTION_ERROR") return "ERROR";
  if (event.type === "FILL_RECEIVED") return "SUCCESS";
  if (String(event.payload?.status ?? "").toUpperCase().includes("WARN")) return "WARNING";
  return "INFO";
}

function eventMessage(event: TradingEvent) {
  if (event.payload?.message) return event.payload.message;
  if (event.payload?.reason) return event.payload.reason;
  if (event.type === "ORDER_SUBMITTED") return `Order submitted${event.payload?.symbol ? ` for ${event.payload.symbol}` : ""}.`;
  if (event.type === "FILL_RECEIVED") return `Fill received${event.payload?.symbol ? ` for ${event.payload.symbol}` : ""}.`;
  if (event.type === "EXECUTION_ERROR") return "Execution error received from trading engine.";
  return "Trading event received.";
}

export function TradingEventBridge() {
  const queryClient = useQueryClient();
  const pushActionFeedEvent = useDashboardStore((state) => state.pushActionFeedEvent);

  useEffect(() => {
    const socket = new WebSocket(websocketUrl());
    socket.onmessage = (message) => {
      let event: TradingEvent;
      try {
        event = JSON.parse(message.data) as TradingEvent;
      } catch {
        return;
      }

      if (event.type === "MARKET_DATA_CANDLE" && event.payload?.symbol) {
        queryClient.invalidateQueries({ queryKey: ["candles", event.payload.symbol] });
      }

      if (FEED_EVENT_TYPES.has(event.type)) {
        pushActionFeedEvent({
          type: event.type,
          severity: eventSeverity(event),
          message: eventMessage(event),
          timestamp: event.timestamp,
          symbol: event.payload?.symbol,
          payload: event.payload,
        });
      }

      if (["FILL_RECEIVED", "ORDER_CANCELLED"].includes(event.type)) {
        queryClient.invalidateQueries({ queryKey: ["execution", "orders"] });
        queryClient.invalidateQueries({ queryKey: ["execution", "positions"] });
      }

      if (["ORDER_STATUS", "FILL", "SIGNAL_UPDATE"].includes(event.type)) {
        queryClient.invalidateQueries();
      }
    };
    return () => socket.close();
  }, [pushActionFeedEvent, queryClient]);

  return null;
}
