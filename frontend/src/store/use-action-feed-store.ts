import { create } from "zustand";

export type ActionFeedSeverity = "INFO" | "WARN" | "CRITICAL";

export type ActionFeedEvent = {
  id: string;
  timestamp: string;
  severity: ActionFeedSeverity;
  entity_id?: string | null;
  message: string;
  type?: string;
  payload?: Record<string, unknown>;
};

type ActionFeedState = {
  events: ActionFeedEvent[];
  appendEvent: (event: Omit<ActionFeedEvent, "id" | "timestamp"> & { id?: string; timestamp?: string }) => void;
  setEvents: (events: ActionFeedEvent[]) => void;
  clearEvents: () => void;
};

export const useActionFeedStore = create<ActionFeedState>()((set) => ({
  events: [],
  appendEvent: (event) =>
    set((state) => ({
      events: [
        ...state.events,
        {
          ...event,
          id: event.id ?? `${event.entity_id ?? event.type ?? "event"}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          timestamp: event.timestamp ?? new Date().toISOString(),
        },
      ].slice(-200),
    })),
  setEvents: (events) => set({ events: events.slice(-200) }),
  clearEvents: () => set({ events: [] }),
}));
