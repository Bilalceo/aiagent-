"use client";

import { useEffect, useState } from "react";
import { getManagerStats, getManagerActionItems, getManagerRecentCalls } from "@/lib/manager";
import type { ManagerStats, ManagerActionItem, ManagerCall } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";
import {
  PageHeader,
  SectionHeader,
  Card,
  CardBody,
  CardHeader,
  MetricCard,
  Table,
  TH,
  TD,
  TR,
  Badge,
  StatusBadge,
  SafetyBanner,
  LoadingState,
  ErrorState,
  EmptyState,
} from "@/components/ui";

function fmtTime(s: string | null): string {
  return s ? s.replace("T", " ").slice(0, 16) : "-";
}

function priorityTone(p: string): "danger" | "warning" | "neutral" {
  if (p === "urgent") return "danger";
  if (p === "high") return "warning";
  return "neutral";
}

export default function ManagerHome() {
  const { t, tStatus } = useLanguage();
  const [stats, setStats] = useState<ManagerStats | null>(null);
  const [items, setItems] = useState<ManagerActionItem[]>([]);
  const [calls, setCalls] = useState<ManagerCall[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getManagerStats(), getManagerActionItems(), getManagerRecentCalls()])
      .then(([s, a, c]) => {
        setStats(s);
        setItems(a);
        setCalls(c);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;

  return (
    <div className="space-y-6">
      <PageHeader title={t("mgr_home_title")} subtitle={t("mgr_home_sub")} />

      <SafetyBanner>{t("mgr_safety")}</SafetyBanner>

      {stats ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MetricCard label={t("mgr_kpi_total_calls")} value={stats.total_calls} />
          <MetricCard label={t("mgr_kpi_ai_resolved")} value={stats.ai_resolved} tone="success" />
          <MetricCard
            label={t("mgr_kpi_transfers")}
            value={stats.operator_transfers}
            tone={stats.operator_transfers > 0 ? "warning" : "neutral"}
          />
          <MetricCard
            label={t("mgr_kpi_action")}
            value={stats.callbacks_required}
            tone={stats.callbacks_required > 0 ? "warning" : "neutral"}
          />
        </div>
      ) : null}

      {/* Action items needing manager/operator attention (real callbacks). */}
      <div>
        <SectionHeader title={t("mgr_sec_action_items")} />
        {items.length === 0 ? (
          <EmptyState title={t("mgr_empty_action")} />
        ) : (
          <Table
            head={
              <>
                <TH>{t("th_reason")}</TH>
                <TH>{t("th_priority")}</TH>
                <TH>{t("th_status")}</TH>
                <TH>{t("th_phone")}</TH>
                <TH>{t("th_due")}</TH>
              </>
            }
          >
            {items.map((it) => (
              <TR key={it.id}>
                <TD>{tStatus(it.reason)}</TD>
                <TD>
                  <Badge tone={priorityTone(it.priority)}>{tStatus(it.priority)}</Badge>
                </TD>
                <TD>
                  <StatusBadge status={it.status} />
                </TD>
                <TD className="font-mono">{it.phone_masked ?? "-"}</TD>
                <TD className="text-slate-500">{fmtTime(it.due_at)}</TD>
              </TR>
            ))}
          </Table>
        )}
      </div>

      {/* Recent calls (masked). */}
      <div>
        <SectionHeader title={t("mgr_sec_recent_calls")} />
        {calls.length === 0 ? (
          <EmptyState title={t("mgr_empty_calls")} />
        ) : (
          <Table
            head={
              <>
                <TH>{t("th_from")}</TH>
                <TH>{t("th_language")}</TH>
                <TH>{t("th_status")}</TH>
                <TH>{t("th_started")}</TH>
              </>
            }
          >
            {calls.map((c) => (
              <TR key={c.id}>
                <TD className="font-mono">{c.from_masked ?? "-"}</TD>
                <TD>{c.language ?? "-"}</TD>
                <TD>
                  <StatusBadge status={c.status} />
                </TD>
                <TD className="text-slate-500">{fmtTime(c.started_at)}</TD>
              </TR>
            ))}
          </Table>
        )}
      </div>

      {/* Appointment module not built yet - explicit, no fake data. */}
      <Card>
        <CardHeader title={t("mgr_gap_appt_title")} />
        <CardBody>
          <p className="text-sm text-slate-500">{t("mgr_gap_appt")}</p>
        </CardBody>
      </Card>
    </div>
  );
}
