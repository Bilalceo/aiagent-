"use client";

import { useEffect, useState } from "react";
import { getAuditLogs } from "@/lib/admin";
import { getUser } from "@/lib/auth";
import { useLanguage } from "@/lib/i18n";
import type { AuditLogEntry } from "@/lib/types";

const LIMITS = [25, 50, 100];

export default function AuditLogsPage() {
  const { t } = useLanguage();
  const role = getUser()?.role;
  const canView = role === "super_admin" || role === "admin";

  const [rows, setRows] = useState<AuditLogEntry[]>([]);
  const [eventType, setEventType] = useState("");
  const [actorId, setActorId] = useState("");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    getAuditLogs({
      event_type: eventType || undefined,
      actor_user_id: actorId ? Number(actorId) : undefined,
      limit,
      offset,
    })
      .then(setRows)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(load, [eventType, actorId, limit, offset]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!canView) {
    return <p className="text-sm text-red-600">{t("audit_forbidden")}</p>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("audit_title")}</h1>

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <input
          className="rounded border px-2 py-1"
          placeholder={t("ph_event_type")}
          value={eventType}
          onChange={(e) => { setOffset(0); setEventType(e.target.value); }}
        />
        <input
          className="w-40 rounded border px-2 py-1"
          placeholder={t("ph_actor_id")}
          value={actorId}
          onChange={(e) => { setOffset(0); setActorId(e.target.value.replace(/[^0-9]/g, "")); }}
        />
        <label className="flex items-center gap-1">
          {t("audit_limit")}
          <select className="rounded border px-2 py-1" value={limit} onChange={(e) => { setOffset(0); setLimit(Number(e.target.value)); }}>
            {LIMITS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>
        <div className="ml-auto flex items-center gap-2">
          <button
            className="rounded border px-2 py-1 disabled:opacity-50"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
          >
            {t("pg_prev")}
          </button>
          <span className="text-slate-500">{t("audit_offset")} {offset}</span>
          <button
            className="rounded border px-2 py-1 disabled:opacity-50"
            disabled={rows.length < limit}
            onClick={() => setOffset(offset + limit)}
          >
            {t("pg_next")}
          </button>
        </div>
      </div>

      {loading ? (
        <p className="text-slate-500">{t("loading")}</p>
      ) : error ? (
        <p className="text-red-600">{t("error")}: {error}</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">{t("audit_empty")}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-slate-500">
              <th className="py-2">{t("th_id")}</th>
              <th>{t("th_event")}</th>
              <th>{t("th_actor")}</th>
              <th>{t("th_when")}</th>
              <th>{t("th_metadata")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b align-top hover:bg-gray-50">
                <td className="py-2">{r.id}</td>
                <td className="font-medium">{r.event_type}</td>
                <td>{r.actor_user_id ?? "-"}</td>
                <td>{r.created_at?.replace("T", " ").slice(0, 19) ?? "-"}</td>
                <td>
                  {r.metadata ? (
                    <pre className="max-w-md overflow-x-auto whitespace-pre-wrap rounded bg-gray-50 p-2 text-xs text-slate-700">
                      {JSON.stringify(r.metadata, null, 2)}
                    </pre>
                  ) : (
                    <span className="text-slate-400">-</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
