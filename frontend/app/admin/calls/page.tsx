"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getCalls } from "@/lib/admin";
import { maskPhone } from "@/components/ui";
import { useLanguage } from "@/lib/i18n";
import type { AdminCall } from "@/lib/types";

const STATUSES = ["", "in_progress", "completed", "transferred"];
const LANGUAGES = ["", "uz-UZ", "ru-RU"];

export default function CallsPage() {
  const { t, tStatus } = useLanguage();
  const [calls, setCalls] = useState<AdminCall[]>([]);
  const [status, setStatus] = useState("");
  const [language, setLanguage] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    getCalls({ status: status || undefined, language: language || undefined })
      .then(setCalls)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [status, language]);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("calls_title")}</h1>

      <div className="flex gap-3 text-sm">
        <label className="flex items-center gap-1">
          {t("th_status")}
          <select className="rounded border px-2 py-1" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s ? tStatus(s) : t("filter_all")}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          {t("th_language")}
          <select className="rounded border px-2 py-1" value={language} onChange={(e) => setLanguage(e.target.value)}>
            {LANGUAGES.map((l) => (
              <option key={l} value={l}>{l || t("filter_all")}</option>
            ))}
          </select>
        </label>
      </div>

      {loading ? (
        <p className="text-slate-500">{t("loading")}</p>
      ) : error ? (
        <p className="text-red-600">{t("error")}: {error}</p>
      ) : calls.length === 0 ? (
        <p className="text-sm text-slate-400">{t("empty_calls_filter")}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-slate-500">
              <th className="py-2">{t("th_id")}</th>
              <th>{t("th_from")}</th>
              <th>{t("th_language")}</th>
              <th>{t("th_status")}</th>
              <th>{t("th_duration")}</th>
              <th>{t("th_started")}</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((c) => (
              <tr key={c.id} className="border-b hover:bg-gray-50">
                <td className="py-2">
                  <Link href={`/admin/calls/${c.id}`} className="text-blue-600">{c.id}</Link>
                </td>
                <td className="font-mono">{maskPhone(c.from_number)}</td>
                <td>{c.language ?? "-"}</td>
                <td>{tStatus(c.status)}</td>
                <td>{c.duration_seconds != null ? `${c.duration_seconds}s` : "-"}</td>
                <td>{c.started_at?.replace("T", " ").slice(0, 19) ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
