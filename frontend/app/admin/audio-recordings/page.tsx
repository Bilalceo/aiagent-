"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { deleteAudioRecording, getAudioRecordings } from "@/lib/admin";
import { getUser } from "@/lib/auth";
import { useLanguage } from "@/lib/i18n";
import type { AudioRecording } from "@/lib/types";

const DIRECTIONS = ["", "inbound", "outbound"];
const KINDS = ["", "user_audio", "ai_tts", "full_call", "system"];
const PAGE_SIZE = 25;

function fmtDate(v: string | null): string {
  return v ? v.replace("T", " ").slice(0, 19) : "-";
}

export default function AudioRecordingsPage() {
  const { t, tStatus } = useLanguage();
  const role = getUser()?.role;
  const canView = role === "super_admin" || role === "admin";

  const [rows, setRows] = useState<AudioRecording[]>([]);
  const [callId, setCallId] = useState("");

  useEffect(() => {
    if (typeof window === "undefined") return;
    const fromUrl = new URLSearchParams(window.location.search).get("call_id");
    if (fromUrl && /^[0-9]+$/.test(fromUrl)) setCallId(fromUrl);
  }, []);

  const [direction, setDirection] = useState("");
  const [kind, setKind] = useState("");
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  function load() {
    if (!canView) return;
    setLoading(true);
    setError(null);
    const callIdNum = callId.trim() === "" ? undefined : Number(callId);
    getAudioRecordings({
      call_id: Number.isFinite(callIdNum) ? callIdNum : undefined,
      direction: direction || undefined,
      kind: kind || undefined,
      include_deleted: includeDeleted,
      limit: PAGE_SIZE,
      offset,
    })
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : t("error")))
      .finally(() => setLoading(false));
  }

  useEffect(load, [callId, direction, kind, includeDeleted, offset]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => setOffset(0), [callId, direction, kind, includeDeleted]);

  async function onDelete(rec: AudioRecording) {
    if (!window.confirm(`#${rec.id}: ${t("audio_confirm_delete")}`)) return;
    setError(null);
    setMessage(null);
    try {
      await deleteAudioRecording(rec.id);
      setMessage(`#${rec.id} ${t("audio_deleted_msg")}`);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("action_failed"));
    }
  }

  if (!canView) {
    return (
      <div className="space-y-2">
        <h1 className="text-xl font-semibold">{t("audio_title")}</h1>
        <p className="text-sm text-red-600">{t("audio_forbidden")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("audio_title")}</h1>
      <p className="text-xs text-slate-400">{t("audio_sub")}</p>
      {message && <p className="text-sm text-green-700">{message}</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <input
          className="w-32 rounded border px-2 py-1"
          placeholder={t("ph_call_id")}
          inputMode="numeric"
          value={callId}
          onChange={(e) => setCallId(e.target.value.replace(/[^0-9]/g, ""))}
        />
        <label className="flex items-center gap-1">
          {t("th_direction")}
          <select className="rounded border px-2 py-1" value={direction} onChange={(e) => setDirection(e.target.value)}>
            {DIRECTIONS.map((d) => <option key={d} value={d}>{d ? tStatus(d) : t("filter_all")}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1">
          {t("th_kind")}
          <select className="rounded border px-2 py-1" value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => <option key={k} value={k}>{k ? tStatus(k) : t("filter_all")}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={includeDeleted} onChange={(e) => setIncludeDeleted(e.target.checked)} />
          {t("chk_include_deleted")}
        </label>
      </div>

      {loading ? (
        <p className="text-slate-500">{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">{t("audio_empty")}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-slate-500">
              <th className="py-2">{t("th_id")}</th>
              <th>{t("th_call")}</th>
              <th>{t("th_direction")}</th>
              <th>{t("th_kind")}</th>
              <th>{t("th_content_type")}</th>
              <th>{t("th_size")}</th>
              <th>{t("th_duration")}</th>
              <th>{t("th_lang")}</th>
              <th>{t("th_conf")}</th>
              <th>{t("th_voice")}</th>
              <th>{t("th_expires")}</th>
              <th>{t("th_created")}</th>
              <th>{t("th_deleted")}</th>
              <th>{t("th_actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b align-top hover:bg-gray-50">
                <td className="py-2">
                  <Link href={`/admin/audio-recordings/${r.id}`} className="text-blue-600">#{r.id}</Link>
                </td>
                <td>
                  <Link href={`/admin/calls/${r.call_session_id}`} className="text-blue-600">#{r.call_session_id}</Link>
                </td>
                <td>{tStatus(r.direction)}</td>
                <td>{tStatus(r.kind)}</td>
                <td>{r.content_type}</td>
                <td>{r.size_bytes}</td>
                <td>{r.duration_ms ?? "-"}</td>
                <td>{r.transcript_language ?? "-"}</td>
                <td>{r.transcript_confidence != null ? r.transcript_confidence.toFixed(2) : "-"}</td>
                <td>{r.tts_voice ?? "-"}</td>
                <td>{fmtDate(r.expires_at)}</td>
                <td>{fmtDate(r.created_at)}</td>
                <td>{r.is_deleted ? t("yes") : t("no")}</td>
                <td className="whitespace-nowrap">
                  {!r.is_deleted && (
                    <button
                      className="rounded border border-red-300 px-2 py-0.5 text-red-700"
                      onClick={() => onDelete(r)}
                    >
                      {t("act_delete")}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="flex items-center gap-3 text-sm">
        <button
          className="rounded border px-2 py-1 disabled:opacity-40"
          disabled={offset === 0 || loading}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          {t("pg_prev")}
        </button>
        <span className="text-slate-500">
          {t("pg_rows")} {rows.length === 0 ? 0 : offset + 1}-{offset + rows.length}
        </span>
        <button
          className="rounded border px-2 py-1 disabled:opacity-40"
          disabled={rows.length < PAGE_SIZE || loading}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          {t("pg_next")}
        </button>
      </div>
    </div>
  );
}
