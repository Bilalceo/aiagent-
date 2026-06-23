import { authHeader } from "./auth";
import type {
  AdminCall,
  AdminCallDetail,
  AdminStats,
  AudioRecording,
  AudioRecordingDetail,
  AudioRecordingFilters,
  AuditLogEntry,
  TelephonyCall,
  TelephonyCallFilters,
  TelephonyStream,
  TelephonyStreamFilters,
  VoiceReadiness,
  CallbackTask,
  KnowledgeItem,
  KnowledgeItemInput,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api/v1";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store", headers: authHeader() });
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

export function getStats(): Promise<AdminStats> {
  return getJSON<AdminStats>("/admin/stats");
}

export function getAuditLogs(
  filters: { event_type?: string; actor_user_id?: number; limit?: number; offset?: number } = {},
): Promise<AuditLogEntry[]> {
  const q = new URLSearchParams();
  if (filters.event_type) q.set("event_type", filters.event_type);
  if (filters.actor_user_id != null) q.set("actor_user_id", String(filters.actor_user_id));
  if (filters.limit != null) q.set("limit", String(filters.limit));
  if (filters.offset != null) q.set("offset", String(filters.offset));
  const qs = q.toString();
  return getJSON<AuditLogEntry[]>(`/admin/audit-logs${qs ? `?${qs}` : ""}`);
}

export function getCalls(filters: { status?: string; language?: string } = {}): Promise<AdminCall[]> {
  const q = new URLSearchParams();
  if (filters.status) q.set("status", filters.status);
  if (filters.language) q.set("language", filters.language);
  const qs = q.toString();
  return getJSON<AdminCall[]>(`/admin/calls${qs ? `?${qs}` : ""}`);
}

export function getCall(id: number | string): Promise<AdminCallDetail> {
  return getJSON<AdminCallDetail>(`/admin/calls/${id}`);
}

export function getAudioRecordings(filters: AudioRecordingFilters = {}): Promise<AudioRecording[]> {
  const q = new URLSearchParams();
  if (filters.call_id != null) q.set("call_id", String(filters.call_id));
  if (filters.direction) q.set("direction", filters.direction);
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.include_deleted) q.set("include_deleted", "true");
  if (filters.limit != null) q.set("limit", String(filters.limit));
  if (filters.offset != null) q.set("offset", String(filters.offset));
  const qs = q.toString();
  return getJSON<AudioRecording[]>(`/admin/audio-recordings${qs ? `?${qs}` : ""}`);
}

export function getAudioRecording(id: number | string): Promise<AudioRecordingDetail> {
  return getJSON<AudioRecordingDetail>(`/admin/audio-recordings/${id}`);
}

export function deleteAudioRecording(id: number): Promise<AudioRecording> {
  return writeJSON<AudioRecording>(`/admin/audio-recordings/${id}/delete`, "POST");
}

export function getTelephonyCalls(filters: TelephonyCallFilters = {}): Promise<TelephonyCall[]> {
  const q = new URLSearchParams();
  if (filters.provider) q.set("provider", filters.provider);
  if (filters.status) q.set("status", filters.status);
  if (filters.direction) q.set("direction", filters.direction);
  if (filters.call_session_id != null) q.set("call_session_id", String(filters.call_session_id));
  if (filters.limit != null) q.set("limit", String(filters.limit));
  if (filters.offset != null) q.set("offset", String(filters.offset));
  const qs = q.toString();
  return getJSON<TelephonyCall[]>(`/admin/telephony-calls${qs ? `?${qs}` : ""}`);
}

export function getTelephonyCall(id: number | string): Promise<TelephonyCall> {
  return getJSON<TelephonyCall>(`/admin/telephony-calls/${id}`);
}

// Voice provider readiness (safe config only - no keys/tokens ever returned).
export function getReadiness(): Promise<VoiceReadiness> {
  return getJSON<VoiceReadiness>("/admin/voice-provider-readiness");
}

export function getTelephonyStreams(filters: TelephonyStreamFilters = {}): Promise<TelephonyStream[]> {
  const q = new URLSearchParams();
  if (filters.call_sid) q.set("call_sid", filters.call_sid);
  if (filters.status) q.set("status", filters.status);
  if (filters.limit != null) q.set("limit", String(filters.limit));
  if (filters.offset != null) q.set("offset", String(filters.offset));
  const qs = q.toString();
  return getJSON<TelephonyStream[]>(`/admin/telephony-streams${qs ? `?${qs}` : ""}`);
}

export function getCallbacks(
  filters: { status?: string; priority?: string; reason?: string; assigned_to_me?: boolean } = {},
): Promise<CallbackTask[]> {
  const q = new URLSearchParams();
  if (filters.status) q.set("status", filters.status);
  if (filters.priority) q.set("priority", filters.priority);
  if (filters.reason) q.set("reason", filters.reason);
  if (filters.assigned_to_me) q.set("assigned_to_me", "true");
  const qs = q.toString();
  return getJSON<CallbackTask[]>(`/admin/callbacks${qs ? `?${qs}` : ""}`);
}

export function assignCallback(id: number): Promise<CallbackTask> {
  return writeJSON<CallbackTask>(`/admin/callbacks/${id}/assign`, "POST");
}

export function completeCallback(id: number): Promise<CallbackTask> {
  return writeJSON<CallbackTask>(`/admin/callbacks/${id}/complete`, "POST");
}

export function cancelCallback(id: number): Promise<CallbackTask> {
  return writeJSON<CallbackTask>(`/admin/callbacks/${id}/cancel`, "POST");
}

export function rescheduleCallback(id: number, due_at: string): Promise<CallbackTask> {
  return writeJSON<CallbackTask>(`/admin/callbacks/${id}/reschedule`, "POST", { due_at });
}

export function updateCallbackNotes(id: number, resolution_notes: string): Promise<CallbackTask> {
  return writeJSON<CallbackTask>(`/admin/callbacks/${id}/notes`, "PATCH", { resolution_notes });
}

export function getKnowledgeItems(
  filters: { category?: string; active_only?: boolean } = {},
): Promise<KnowledgeItem[]> {
  const q = new URLSearchParams();
  if (filters.category) q.set("category", filters.category);
  if (filters.active_only) q.set("active_only", "true");
  const qs = q.toString();
  return getJSON<KnowledgeItem[]>(`/admin/knowledge-items${qs ? `?${qs}` : ""}`);
}

export async function seedKnowledge(): Promise<{ inserted: number }> {
  const res = await fetch(`${API_BASE}/admin/knowledge/seed`, {
    method: "POST",
    headers: authHeader(),
  });
  if (!res.ok) throw new Error(`Seed failed: ${res.status}`);
  return res.json();
}

async function writeJSON<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

export function createKnowledgeItem(payload: KnowledgeItemInput): Promise<KnowledgeItem> {
  return writeJSON<KnowledgeItem>("/admin/knowledge-items", "POST", payload);
}

export function updateKnowledgeItem(id: number, payload: Partial<KnowledgeItemInput>): Promise<KnowledgeItem> {
  return writeJSON<KnowledgeItem>(`/admin/knowledge-items/${id}`, "PATCH", payload);
}

export function activateKnowledgeItem(id: number): Promise<KnowledgeItem> {
  return writeJSON<KnowledgeItem>(`/admin/knowledge-items/${id}/activate`, "POST");
}

export function deactivateKnowledgeItem(id: number): Promise<KnowledgeItem> {
  return writeJSON<KnowledgeItem>(`/admin/knowledge-items/${id}/deactivate`, "POST");
}

export function deleteKnowledgeItem(id: number): Promise<{ status: string; id: number }> {
  return writeJSON<{ status: string; id: number }>(`/admin/knowledge-items/${id}`, "DELETE");
}
