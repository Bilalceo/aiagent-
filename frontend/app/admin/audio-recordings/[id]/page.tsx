"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { deleteAudioRecording, getAudioRecording } from "@/lib/admin";
import { getUser } from "@/lib/auth";
import { useLanguage } from "@/lib/i18n";
import type { AudioRecordingDetail } from "@/lib/types";

function shorten(v: string, head = 10, tail = 6): string {
  if (v.length <= head + tail + 3) return v;
  return `${v.slice(0, head)}...${v.slice(-tail)}`;
}

function fmtDate(v: string | null): string {
  return v ? v.replace("T", " ").slice(0, 19) : "-";
}

export default function AudioRecordingDetailPage() {
  const params = useParams<{ id: string }>();
  const { t, tStatus } = useLanguage();
  const role = getUser()?.role;
  const canView = role === "super_admin" || role === "admin";

  const [data, setData] = useState<AudioRecordingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  function load() {
    if (!canView) return;
    setLoading(true);
    setError(null);
    getAudioRecording(params.id)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : t("error")))
      .finally(() => setLoading(false));
  }

  useEffect(load, [params.id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function onDelete() {
    if (!data || data.is_deleted) return;
    if (!window.confirm(`#${data.id}: ${t("audio_confirm_delete")}`)) return;
    setError(null);
    setMessage(null);
    try {
      await deleteAudioRecording(data.id);
      setMessage(`#${data.id} ${t("audio_deleted_msg")}`);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("action_failed"));
    }
  }

  if (!canView) return <p className="text-sm text-red-600">{t("audio_forbidden")}</p>;
  if (loading) return <p className="text-slate-500">{t("loading")}</p>;
  if (error) return <p className="text-red-600">{t("error")}: {error}</p>;
  if (!data) return null;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/admin/audio-recordings" className="text-sm text-blue-600">
          &larr; {t("audio_detail_back")}
        </Link>
        <h1 className="mt-1 text-xl font-semibold">
          {t("audio_recording")} #{data.id} {data.is_deleted && <span className="text-sm text-red-600">{t("deleted_paren")}</span>}
        </h1>
      </div>

      {message && <p className="text-sm text-green-700">{message}</p>}

      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Field label={t("th_call")} value={<Link href={`/admin/calls/${data.call_session_id}`} className="text-blue-600">#{data.call_session_id}</Link>} />
        <Field label={t("th_direction")} value={tStatus(data.direction)} />
        <Field label={t("th_kind")} value={tStatus(data.kind)} />
        <Field label={t("th_content_type")} value={data.content_type} />
        <Field label={t("th_size_bytes")} value={String(data.size_bytes)} />
        <Field label={t("th_duration_ms")} value={data.duration_ms != null ? String(data.duration_ms) : "-"} />
        <Field label={t("th_storage_provider")} value={data.storage_provider} />
        <Field label={t("th_storage_key")} value={shorten(data.storage_key)} />
        <Field label={t("th_checksum")} value={shorten(data.checksum_sha256, 12, 6)} />
        <Field label={t("th_transcript_lang")} value={data.transcript_language ?? "-"} />
        <Field
          label={t("th_transcript_conf")}
          value={data.transcript_confidence != null ? data.transcript_confidence.toFixed(2) : "-"}
        />
        <Field label={t("th_voice")} value={data.tts_voice ?? "-"} />
        <Field label={t("th_expires")} value={fmtDate(data.expires_at)} />
        <Field label={t("th_created")} value={fmtDate(data.created_at)} />
        <Field label={t("th_updated")} value={fmtDate(data.updated_at)} />
      </div>

      {data.transcript_text && <Block label={t("audio_transcript_text")} value={data.transcript_text} />}
      {data.tts_text && <Block label={t("audio_tts_text")} value={data.tts_text} />}

      <div className="rounded border p-3 text-sm">
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">{t("audio_label")}</div>
        {data.signed_url ? (
          <div className="space-y-2">
            <a href={data.signed_url} className="text-blue-600 underline" target="_blank" rel="noreferrer">
              {t("audio_open_url")}
            </a>
            <audio controls src={data.signed_url} className="w-full" />
          </div>
        ) : (
          <p className="text-slate-500">{t("audio_no_url")}</p>
        )}
      </div>

      {!data.is_deleted && (
        <button
          className="rounded border border-red-300 px-3 py-1 text-sm text-red-700 hover:bg-red-50"
          onClick={onDelete}
        >
          {t("audio_soft_delete")}
        </button>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 break-all">{value}</div>
    </div>
  );
}

function Block({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border p-3 text-sm">
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <p className="whitespace-pre-wrap break-words">{value}</p>
    </div>
  );
}
