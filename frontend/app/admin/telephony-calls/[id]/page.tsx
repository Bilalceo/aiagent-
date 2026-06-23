"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { getTelephonyCall } from "@/lib/admin";
import { getUser } from "@/lib/auth";
import { maskPhone } from "@/components/ui";
import { useLanguage } from "@/lib/i18n";
import type { TelephonyCall } from "@/lib/types";

function fmtDate(v: string | null): string {
  return v ? v.replace("T", " ").slice(0, 19) : "-";
}

export default function TelephonyCallDetailPage() {
  const params = useParams<{ id: string }>();
  const { t, tStatus } = useLanguage();
  const role = getUser()?.role;
  const canView = role === "super_admin" || role === "admin";

  const [data, setData] = useState<TelephonyCall | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!canView) return;
    getTelephonyCall(params.id)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : t("error")))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.id, canView]);

  if (!canView) return <p className="text-sm text-red-600">{t("tel_forbidden")}</p>;
  if (loading) return <p className="text-slate-500">{t("loading")}</p>;
  if (error) return <p className="text-red-600">{t("error")}: {error}</p>;
  if (!data) return null;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/admin/telephony-calls" className="text-sm text-blue-600">
          &larr; {t("tel_detail_back")}
        </Link>
        <h1 className="mt-1 text-xl font-semibold">{t("tel_call")} #{data.id}</h1>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Field label={t("th_provider")} value={data.provider} />
        <Field label={t("th_provider_call_id")} value={data.provider_call_id ?? "-"} />
        <Field label={t("th_status")} value={tStatus(data.status)} />
        <Field label={t("th_direction")} value={tStatus(data.direction)} />
        <Field label={t("th_from")} value={maskPhone(data.from_number)} />
        <Field label={t("th_to")} value={maskPhone(data.to_number)} />
        <Field
          label={t("th_call_session")}
          value={
            data.call_session_id != null ? (
              <Link href={`/admin/calls/${data.call_session_id}`} className="text-blue-600">
                #{data.call_session_id}
              </Link>
            ) : "-"
          }
        />
        <Field label={t("th_started")} value={fmtDate(data.started_at)} />
        <Field label={t("th_ended")} value={fmtDate(data.ended_at)} />
        <Field label={t("th_created")} value={fmtDate(data.created_at)} />
        <Field label={t("th_updated")} value={fmtDate(data.updated_at)} />
      </div>

      {data.call_session_id != null && (
        <div className="flex flex-wrap gap-3 text-sm">
          <Link
            href={`/admin/calls/${data.call_session_id}`}
            className="rounded border px-3 py-1 text-blue-600 hover:bg-gray-50"
          >
            {t("act_view_call")}
          </Link>
          <Link
            href={`/admin/audio-recordings?call_id=${data.call_session_id}`}
            className="rounded border px-3 py-1 text-blue-600 hover:bg-gray-50"
          >
            {t("act_view_audio")}
          </Link>
        </div>
      )}

      <details className="rounded border p-3 text-sm" open>
        <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-400">
          {t("safe_raw_metadata")}
        </summary>
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words text-xs text-slate-700">
          {data.raw_metadata ? JSON.stringify(data.raw_metadata, null, 2) : t("none_paren")}
        </pre>
      </details>
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
