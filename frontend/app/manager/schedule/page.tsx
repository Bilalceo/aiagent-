"use client";

import { useState } from "react";
import { useLanguage } from "@/lib/i18n";
import { PageHeader, Card, CardBody, CardHeader, EmptyState } from "@/components/ui";

function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

function fmtDate(d: Date): string {
  // Local y-m-d (not UTC) so the date label matches the clinic timezone.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export default function ManagerSchedule() {
  const { t } = useLanguage();
  const [view, setView] = useState<"day" | "week">("day");
  const [date, setDate] = useState<Date>(new Date());

  const step = view === "day" ? 1 : 7;
  const rangeLabel =
    view === "day" ? fmtDate(date) : `${fmtDate(date)} - ${fmtDate(addDays(date, 6))}`;

  return (
    <div className="space-y-4">
      <PageHeader title={t("mgr_sched_title")} />

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <div className="inline-flex overflow-hidden rounded border border-slate-300">
          <button
            onClick={() => setView("day")}
            className={`px-3 py-1 ${view === "day" ? "bg-blue-600 text-white" : "bg-white text-slate-600 hover:bg-slate-100"}`}
          >
            {t("mgr_view_day")}
          </button>
          <button
            onClick={() => setView("week")}
            className={`px-3 py-1 ${view === "week" ? "bg-blue-600 text-white" : "bg-white text-slate-600 hover:bg-slate-100"}`}
          >
            {t("mgr_view_week")}
          </button>
        </div>
        <button className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100" onClick={() => setDate(addDays(date, -step))}>
          {t("mgr_prev")}
        </button>
        <button className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100" onClick={() => setDate(new Date())}>
          {t("mgr_today")}
        </button>
        <button className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100" onClick={() => setDate(addDays(date, step))}>
          {t("mgr_next")}
        </button>
        <span className="ml-2 font-mono text-slate-700">{rangeLabel}</span>
      </div>

      <EmptyState title={t("mgr_empty_day")} />

      {/* Appointment module not built yet - explicit backend gap, no fake data. */}
      <Card>
        <CardHeader title={t("mgr_gap_appt_title")} />
        <CardBody>
          <p className="text-sm text-slate-500">{t("mgr_gap_appt")}</p>
        </CardBody>
      </Card>
    </div>
  );
}
