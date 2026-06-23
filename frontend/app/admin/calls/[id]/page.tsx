"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { getCall } from "@/lib/admin";
import { maskPhone } from "@/components/ui";
import { useLanguage } from "@/lib/i18n";
import type { AdminCallDetail } from "@/lib/types";

export default function CallDetailPage() {
  const params = useParams<{ id: string }>();
  const { t, tStatus } = useLanguage();
  const [data, setData] = useState<AdminCallDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getCall(params.id)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [params.id]);

  if (loading) return <p className="text-slate-500">{t("loading")}</p>;
  if (error) return <p className="text-red-600">{t("error")}: {error}</p>;
  if (!data) return null;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/admin/calls" className="text-sm text-blue-600">&larr; {t("detail_back")}</Link>
        <h1 className="mt-1 text-xl font-semibold">{t("detail_call")} #{data.id}</h1>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Field label={t("th_from")} value={maskPhone(data.from_number)} />
        <Field label={t("th_language")} value={data.language ?? "-"} />
        <Field label={t("th_status")} value={tStatus(data.status)} />
        <Field label={t("th_duration")} value={data.duration_seconds != null ? `${data.duration_seconds}s` : "-"} />
      </div>

      {(data.transfer || data.reason_codes.length > 0) && (
        <div className="rounded-lg border bg-amber-50 p-3 text-sm">
          {data.transfer && (
            <div>
              <span className="font-semibold">{t("transfer_label")}:</span> {tStatus(data.transfer.reason)} ({t("lbl_priority")}: {tStatus(data.transfer.priority)}, {tStatus(data.transfer.status)})
            </div>
          )}
          {data.reason_codes.length > 0 && (
            <div><span className="font-semibold">{t("safety_codes")}:</span> {data.reason_codes.join(", ")}</div>
          )}
        </div>
      )}

      {data.callback && (
        <div className="rounded-lg border bg-orange-50 p-3 text-sm">
          <div className="font-semibold">{t("callback_task")} #{data.callback.id}</div>
          <div>{t("lbl_reason")}: {tStatus(data.callback.reason)} | {t("lbl_priority")}: {tStatus(data.callback.priority)} | {t("th_status")}: {tStatus(data.callback.status)}</div>
          <div>{t("lbl_phone")}: {maskPhone(data.callback.patient_phone)} | {t("lbl_due")}: {data.callback.due_at?.replace("T", " ").slice(0, 19) ?? "-"}</div>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-sm font-semibold text-slate-700">{t("transcript")}</h2>
        <div className="space-y-2">
          {data.transcripts.map((item) => (
            <div key={item.id} className={item.role === "user" ? "text-right" : "text-left"}>
              <span
                className={
                  "inline-block max-w-[80%] rounded-lg px-3 py-2 text-sm " +
                  (item.role === "user" ? "bg-blue-600 text-white" : "bg-gray-100")
                }
              >
                <span className="mr-2 text-[10px] uppercase opacity-60">{tStatus(item.role)}</span>
                {item.text}
              </span>
            </div>
          ))}
        </div>
      </div>

      {data.sources.length > 0 && (
        <div>
          <h2 className="mb-2 text-sm font-semibold text-slate-700">{t("kb_sources_used")}</h2>
          <ul className="list-inside list-disc text-sm text-slate-600">
            {data.sources.map((s) => (
              <li key={s.id}>#{s.id} {s.title}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="text-slate-800">{value}</div>
      <div className="mt-1 text-xs text-slate-500">{label}</div>
    </div>
  );
}
