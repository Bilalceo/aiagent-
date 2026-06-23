"use client";

import { useEffect, useState } from "react";
import { getManagerStats, getManagerActionItems } from "@/lib/manager";
import type { ManagerStats, ManagerActionItem } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";
import { PageHeader, MetricCard, Card, CardBody, CardHeader, LoadingState, ErrorState } from "@/components/ui";

export default function ManagerReports() {
  const { t } = useLanguage();
  const [stats, setStats] = useState<ManagerStats | null>(null);
  const [actionCount, setActionCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getManagerStats(), getManagerActionItems()])
      .then(([s, items]: [ManagerStats, ManagerActionItem[]]) => {
        setStats(s);
        setActionCount(items.length);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;
  if (!stats) return null;

  return (
    <div className="space-y-6">
      <PageHeader title={t("mgr_reports_title")} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <MetricCard label={t("mgr_kpi_total_calls")} value={stats.total_calls} />
        <MetricCard label={t("mgr_kpi_ai_resolved")} value={stats.ai_resolved} tone="success" />
        <MetricCard label={t("mgr_kpi_transfers")} value={stats.operator_transfers} />
        <MetricCard label={t("mgr_rep_action_items")} value={actionCount} tone={actionCount > 0 ? "warning" : "neutral"} />
        <MetricCard label={t("m_kb_items")} value={stats.kb_items} />
      </div>

      {/* Date-scoped reports (today/week/month) need the appointment backend. */}
      <Card>
        <CardHeader title={t("mgr_gap_appt_title")} />
        <CardBody>
          <p className="text-sm text-slate-500">{t("mgr_rep_note")}</p>
        </CardBody>
      </Card>
    </div>
  );
}
