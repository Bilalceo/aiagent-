"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getTelephonyCalls } from "@/lib/admin";
import { getUser } from "@/lib/auth";
import { maskPhone } from "@/components/ui";
import { useLanguage } from "@/lib/i18n";
import type { TelephonyCall } from "@/lib/types";

const PROVIDERS = ["", "mock", "twilio"];
const STATUSES = ["", "received", "processed", "failed"];
const DIRECTIONS = ["", "inbound", "outbound"];
const PAGE_SIZE = 25;

function fmtDate(v: string | null): string {
  return v ? v.replace("T", " ").slice(0, 19) : "-";
}

export default function TelephonyCallsPage() {
  const { t, tStatus } = useLanguage();
  const role = getUser()?.role;
  const canView = role === "super_admin" || role === "admin";

  const [rows, setRows] = useState<TelephonyCall[]>([]);
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [direction, setDirection] = useState("");
  const [callSessionId, setCallSessionId] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  function load() {
    if (!canView) return;
    setLoading(true);
    setError(null);
    const csid = callSessionId.trim() === "" ? undefined : Number(callSessionId);
    getTelephonyCalls({
      provider: provider || undefined,
      status: status || undefined,
      direction: direction || undefined,
      call_session_id: Number.isFinite(csid) ? csid : undefined,
      limit: PAGE_SIZE,
      offset,
    })
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : t("error")))
      .finally(() => setLoading(false));
  }

  useEffect(load, [provider, status, direction, callSessionId, offset]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => setOffset(0), [provider, status, direction, callSessionId]);

  if (!canView) {
    return (
      <div className="space-y-2">
        <h1 className="text-xl font-semibold">{t("tel_title")}</h1>
        <p className="text-sm text-red-600">{t("tel_forbidden")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("tel_title")}</h1>
      <p className="text-xs text-slate-400">{t("tel_sub")}</p>
      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-1">
          {t("th_provider")}
          <select className="rounded border px-2 py-1" value={provider} onChange={(e) => setProvider(e.target.value)}>
            {PROVIDERS.map((p) => <option key={p} value={p}>{p || t("filter_all")}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1">
          {t("th_status")}
          <select className="rounded border px-2 py-1" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? tStatus(s) : t("filter_all")}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1">
          {t("th_direction")}
          <select className="rounded border px-2 py-1" value={direction} onChange={(e) => setDirection(e.target.value)}>
            {DIRECTIONS.map((d) => <option key={d} value={d}>{d ? tStatus(d) : t("filter_all")}</option>)}
          </select>
        </label>
        <input
          className="w-40 rounded border px-2 py-1"
          placeholder={t("ph_call_session_id")}
          inputMode="numeric"
          value={callSessionId}
          onChange={(e) => setCallSessionId(e.target.value.replace(/[^0-9]/g, ""))}
        />
      </div>

      {loading ? (
        <p className="text-slate-500">{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">{t("tel_empty")}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-slate-500">
              <th className="py-2">{t("th_id")}</th>
              <th>{t("th_provider")}</th>
              <th>{t("th_provider_call_id")}</th>
              <th>{t("th_status")}</th>
              <th>{t("th_direction")}</th>
              <th>{t("th_from")}</th>
              <th>{t("th_to")}</th>
              <th>{t("th_call")}</th>
              <th>{t("th_started")}</th>
              <th>{t("th_created")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b align-top hover:bg-gray-50">
                <td className="py-2">
                  <Link href={`/admin/telephony-calls/${r.id}`} className="text-blue-600">#{r.id}</Link>
                </td>
                <td>{r.provider}</td>
                <td>{r.provider_call_id ?? "-"}</td>
                <td>{tStatus(r.status)}</td>
                <td>{tStatus(r.direction)}</td>
                <td className="font-mono">{maskPhone(r.from_number)}</td>
                <td className="font-mono">{maskPhone(r.to_number)}</td>
                <td>
                  {r.call_session_id != null ? (
                    <Link href={`/admin/calls/${r.call_session_id}`} className="text-blue-600">#{r.call_session_id}</Link>
                  ) : "-"}
                </td>
                <td>{fmtDate(r.started_at)}</td>
                <td>{fmtDate(r.created_at)}</td>
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
