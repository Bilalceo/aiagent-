import { authHeader } from "./auth";
import type { ManagerActionItem, ManagerCall, ManagerStats } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api/v1";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store", headers: authHeader() });
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

export function getManagerStats(): Promise<ManagerStats> {
  return getJSON<ManagerStats>("/manager/stats");
}

export function getManagerActionItems(): Promise<ManagerActionItem[]> {
  return getJSON<ManagerActionItem[]>("/manager/action-items");
}

export function getManagerRecentCalls(): Promise<ManagerCall[]> {
  return getJSON<ManagerCall[]>("/manager/recent-calls");
}
