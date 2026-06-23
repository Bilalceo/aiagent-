"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  assignCallback,
  cancelCallback,
  completeCallback,
  getCallbacks,
  rescheduleCallback,
  updateCallbackNotes,
} from "@/lib/admin";
import { getUser } from "@/lib/auth";
import { maskPhone } from "@/components/ui";
import { useLanguage } from "@/lib/i18n";
import type { CallbackTask } from "@/lib/types";

const STATUSES = ["", "callback_required", "assigned", "completed", "cancelled"];
const PRIORITIES = ["", "urgent", "high", "normal"];
const TERMINAL = ["completed", "cancelled"];

export default function CallbacksPage() {
  const { t, tStatus } = useLanguage();
  const role = getUser()?.role;
  const canManage = role === "super_admin" || role === "admin";

  const [rows, setRows] = useState<CallbackTask[]>([]);
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [reason, setReason] = useState("");
  const [assignedToMe, setAssignedToMe] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    getCallbacks({
      status: status || undefined,
      priority: priority || undefined,
      reason: reason || undefined,
      assigned_to_me: assignedToMe,
    })
      .then(setRows)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(load, [status, priority, reason, assignedToMe]); // eslint-disable-line react-hooks/exhaustive-deps

  async function act(fn: () => Promise<unknown>, ok: string) {
    setError(null);
    setMessage(null);
    try {
      await fn();
      setMessage(ok);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("action_failed"));
    }
  }

  function isOverdue(task: CallbackTask): boolean {
    return !!task.due_at && !TERMINAL.includes(task.status) && new Date(task.due_at).getTime() < Date.now();
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("cb_title")}</h1>
      {message && <p className="text-sm text-green-700">{message}</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex flex-wrap gap-3 text-sm">
        <label className="flex items-center gap-1">
          {t("th_status")}
          <select className="rounded border px-2 py-1" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? tStatus(s) : t("filter_all")}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1">
          {t("th_priority")}
          <select className="rounded border px-2 py-1" value={priority} onChange={(e) => setPriority(e.target.value)}>
            {PRIORITIES.map((p) => <option key={p} value={p}>{p ? tStatus(p) : t("filter_all")}</option>)}
          </select>
        </label>
        <input className="rounded border px-2 py-1" placeholder={t("ph_reason")} value={reason} onChange={(e) => setReason(e.target.value)} />
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={assignedToMe} onChange={(e) => setAssignedToMe(e.target.checked)} />
          {t("chk_assigned_me")}
        </label>
      </div>

      {loading ? (
        <p className="text-slate-500">{t("loading")}</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">{t("cb_empty")}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-slate-500">
              <th className="py-2">{t("th_id")}</th>
              <th>{t("th_call")}</th>
              <th>{t("th_phone")}</th>
              <th>{t("th_reason")}</th>
              <th>{t("th_priority")}</th>
              <th>{t("th_status")}</th>
              <th>{t("th_due")}</th>
              <th>{t("th_assigned")}</th>
              <th>{t("th_actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((task) => {
              const terminal = TERMINAL.includes(task.status);
              return (
                <tr key={task.id} className="border-b align-top hover:bg-gray-50">
                  <td className="py-2">{task.id}</td>
                  <td>
                    <Link href={`/admin/calls/${task.call_session_id}`} className="text-blue-600">#{task.call_session_id}</Link>
                  </td>
                  <td className="font-mono">{maskPhone(task.patient_phone)}</td>
                  <td>{tStatus(task.reason)}</td>
                  <td>{tStatus(task.priority)}</td>
                  <td>{tStatus(task.status)}</td>
                  <td className={isOverdue(task) ? "font-medium text-red-600" : ""}>
                    {task.due_at?.replace("T", " ").slice(0, 16) ?? "-"}
                    {isOverdue(task) && ` (${t("overdue")})`}
                  </td>
                  <td>{task.assigned_to_user_id ?? "-"}</td>
                  <td className="space-x-1 whitespace-nowrap">
                    {task.status === "callback_required" && (
                      <button className="rounded border px-2 py-0.5" onClick={() => act(() => assignCallback(task.id), t("msg_assigned"))}>{t("act_assign_me")}</button>
                    )}
                    {task.status === "assigned" && (
                      <button className="rounded border px-2 py-0.5" onClick={() => act(() => completeCallback(task.id), t("msg_completed"))}>{t("act_complete")}</button>
                    )}
                    {!terminal && (
                      <button
                        className="rounded border px-2 py-0.5"
                        onClick={() => {
                          const notes = window.prompt(t("prompt_notes"), task.resolution_notes ?? "");
                          if (notes !== null) act(() => updateCallbackNotes(task.id, notes), t("msg_notes_saved"));
                        }}
                      >
                        {t("act_notes")}
                      </button>
                    )}
                    {canManage && !terminal && (
                      <button
                        className="rounded border px-2 py-0.5"
                        onClick={() => {
                          const v = window.prompt(t("prompt_due"), "");
                          if (v) act(() => rescheduleCallback(task.id, new Date(v).toISOString()), t("msg_rescheduled"));
                        }}
                      >
                        {t("act_reschedule")}
                      </button>
                    )}
                    {canManage && !terminal && (
                      <button
                        className="rounded border border-red-300 px-2 py-0.5 text-red-700"
                        onClick={() => {
                          if (window.confirm(t("confirm_cancel"))) act(() => cancelCallback(task.id), t("msg_cancelled"));
                        }}
                      >
                        {t("act_cancel")}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
