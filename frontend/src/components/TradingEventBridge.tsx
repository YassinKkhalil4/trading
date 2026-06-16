"use client";

import { getActionFeedEvents } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import { useActionFeedStore, type ActionFeedSeverity } from "@/store/use-action-feed-store";
import { useDashboardStore } from "@/store/use-dashboard-store";
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
const FEED_EVENT_TYPES = new Set(["ORDER_SUBMITTED", "FILL_RECEIVED", "EXECUTION_ERROR", "ORDER_CANCELLED", "ORDER_STATUS", "FILL", "SIGNAL_UPDATE", "STREAM_BRIDGE_ERROR"]);
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000] as const;

function websocketUrl() {
  const url = new URL(API_BASE);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/api/v1/stream";
  url.search = "";
  return url.toString();
}

function mergeActionFeedEvents(events: ReturnType<typeof useActionFeedStore.getState>["events"]) {
  const currentEvents = useActionFeedStore.getState().events;
  const deduped = new Map<string, (typeof currentEvents)[number]>();
  for (const event of [...currentEvents, ...events]) {
    deduped.set(event.id, event);
  }

  useActionFeedStore
    .getState()
    .setEvents(Array.from(deduped.values()).sort((left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime()));
}

async function recoverState(queryClient: ReturnType<typeof useQueryClient>) {
  await Promise.all([
    queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] === "candles" }),
    queryClient.invalidateQueries({ queryKey: queryKeys.executionPositions }),
    queryClient.invalidateQueries({ queryKey: queryKeys.executionOrders }),
  ]);

  const response = await getActionFeedEvents(50);
  mergeActionFeedEvents(response.events);
}

function eventSeverity(event: TradingEvent): ActionFeedSeverity {
  if (event.type === "EXECUTION_ERROR" || event.type === "STREAM_BRIDGE_ERROR") return "CRITICAL";
  if (String(event.payload?.status ?? "").toUpperCase().includes("WARN")) return "WARN";
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
  const appendActionFeedEvent = useActionFeedStore((state) => state.appendEvent);
  const pushStrategyMarker = useDashboardStore((state) => state.pushStrategyMarker);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectAttempt = 0;
    let shouldReconnect = true;
    let hasConnected = false;

    const clearReconnectTimer = () => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const handleMessage = (message: MessageEvent) => {
      let event: TradingEvent;
      try {
        event = JSON.parse(message.data) as TradingEvent;
      } catch {
        return;
      }

      if (event.type === "MARKET_DATA_CANDLE" && event.payload?.symbol) {
        queryClient.invalidateQueries({ queryKey: ["candles", event.payload.symbol] });
      }

      const side = String(event.payload?.side ?? event.payload?.direction ?? event.payload?.action ?? "").toLowerCase();
      const price = Number(event.payload?.price ?? event.payload?.entry_price ?? event.payload?.limit_price);
      if (event.payload?.symbol && ["SIGNAL", "SIGNAL_UPDATE", "SIGNAL_CREATED"].includes(event.type) && ["buy", "long", "sell", "short"].includes(side) && Number.isFinite(price)) {
        pushStrategyMarker({
          id: String(event.payload.id ?? `${event.type}-${event.payload.symbol}-${event.timestamp ?? Date.now()}`),
          symbol: event.payload.symbol.toUpperCase(),
          side: side === "sell" || side === "short" ? "sell" : "buy",
          price,
          timestamp: String(event.payload.timestamp ?? event.payload.source_timestamp ?? event.timestamp ?? new Date().toISOString()),
          strategyId: typeof event.payload.strategy_id === "string" ? event.payload.strategy_id : undefined,
        });
      }

      if (FEED_EVENT_TYPES.has(event.type)) {
        appendActionFeedEvent({
          type: event.type,
          severity: eventSeverity(event),
          message: eventMessage(event),
          timestamp: event.timestamp,
          entity_id: String(event.payload?.order_id ?? event.payload?.id ?? event.payload?.symbol ?? event.type),
          payload: event.payload,
        });
      }

      if (["FILL_RECEIVED", "ORDER_CANCELLED"].includes(event.type)) {
        queryClient.invalidateQueries({ queryKey: queryKeys.executionOrders });
        queryClient.invalidateQueries({ queryKey: queryKeys.executionPositions });
      }

      if (["ORDER_STATUS", "FILL", "SIGNAL_UPDATE"].includes(event.type)) {
        queryClient.invalidateQueries();
      }
    };

    const connect = () => {
      clearReconnectTimer();
      socket = new WebSocket(websocketUrl());

      socket.onopen = () => {
        const wasReconnect = hasConnected;
        hasConnected = true;
        reconnectAttempt = 0;

        if (wasReconnect) {
          void recoverState(queryClient);
        }
      };

      socket.onmessage = handleMessage;

      socket.onerror = () => {
        socket?.close();
      };

      socket.onclose = () => {
        if (!shouldReconnect) return;

        const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
        reconnectAttempt += 1;
        reconnectTimer = setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      shouldReconnect = false;
      clearReconnectTimer();
      socket?.close();
    };
  }, [appendActionFeedEvent, pushStrategyMarker, queryClient]);

  return null;
}
