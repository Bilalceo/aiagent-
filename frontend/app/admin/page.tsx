"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getStats, getReadiness } from "@/lib/admin";
import type { AdminStats, VoiceReadiness } from "@/lib/types";
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
  StatusBadge,
  Badge,
  BoolPill,
  SafetyBanner,
  MaskedPhone,
  LoadingState,
  ErrorState,
  EmptyState,
} from "@/components/ui";

// Metrics the backend does not yet aggregate (see U1 audit gap B1). Shown as an
// explicit "backend gap" panel instead of inventing fake values.
const GAP_KEYS = [
  "gap_avg_ai_latency",
  "gap_avg_tts",
  "gap_emergency",
  "gap_provider_errors",
  "gap_barge",
  "gap_missed",
];

function fmtTime(s: string | null): string {
  return s ? s.replace("T", " ").slice(0, 19) : "-";
}

export default function AdminOverview() {
  const { t } = useLanguage();
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [readiness, setReadiness] = useState<VoiceReadiness | null>(null);
  const [statsErr, setStatsErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.allSettled([getStats(), getReadiness()])
      .then(([s, r]) => {
        if (s.status === "fulfilled") setStats(s.value);
        else setStatsErr(s.reason?.message || "stats");
        if (r.status === "fulfilled") setReadiness(r.value);
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingState />;

  const sum = readiness?.summary;

  return (
    <div className="space-y-6">
      <PageHeader title={t("ov_title")} subtitle={t("ov_subtitle")} />

      <SafetyBanner>{t("safety_notice")}</SafetyBanner>

      {statsErr ? (
        <ErrorState message={t("metrics_admin_only")} />
      ) : stats ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <MetricCard label={t("m_total_calls")} value={stats.total_calls} />
          <MetricCard label={t("m_ai_resolved")} value={stats.ai_resolved} tone="success" />
          <MetricCard
            label={t("m_operator_transfers")}
            value={stats.operator_transfers}
            tone={stats.operator_transfers > 0 ? "warning" : "neutral"}
          />
          <MetricCard
            label={t("m_callbacks_required")}
            value={stats.callbacks_required}
            tone={stats.callbacks_required > 0 ? "warning" : "neutral"}
          />
          <MetricCard label={t("m_kb_items")} value={stats.kb_items} />
        </div>
      ) : null}

      {sum ? (
        <Card>
          <CardHeader
            title={t("voice_status_title")}
            subtitle={t("voice_status_sub")}
            actions={
              <Link href="/admin/provider-readiness" className="text-xs text-blue-600 hover:underline">
                {t("details")}
              </Link>
            }
          />
          <CardBody>
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
              <div>
                <div className="text-xs text-slate-500">{t("f_readiness")}</div>
                <div className="mt-1">
                  <StatusBadge status={readiness?.ready ? "ready" : "not_ready"} />
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t("f_stt_provider")}</div>
                <div className="mt-1">
                  <Badge tone={sum.streaming_stt_provider === "deepgram" ? "info" : "neutral"}>
                    {sum.streaming_stt_provider}
                  </Badge>
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t("f_tts_provider")}</div>
                <div className="mt-1">
                  <Badge tone={sum.streaming_tts_provider === "deepgram" ? "info" : "neutral"}>
                    {sum.streaming_tts_provider}
                  </Badge>
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t("f_twilio_stt")}</div>
                <div className="mt-1">
                  <BoolPill value={sum.stt_twilio_compatible} />
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t("f_twilio_tts")}</div>
                <div className="mt-1">
                  <BoolPill value={sum.tts_twilio_compatible} />
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t("f_smoke_mode")}</div>
                <div className="mt-1">
                  <Badge tone={sum.smoke_mode_enabled ? "warning" : "neutral"}>
                    {sum.smoke_mode_enabled ? t("enabled") : t("disabled")}
                  </Badge>
                </div>
              </div>
            </div>
          </CardBody>
        </Card>
      ) : null}

      <div>
        <SectionHeader title={t("recent_calls")} hint={t("phones_masked")} />
        {!stats || stats.recent_calls.length === 0 ? (
          <EmptyState title={t("empty_calls_title")} hint={t("empty_calls_hint")} />
        ) : (
          <Table
            head={
              <>
                <TH>{t("th_id")}</TH>
                <TH>{t("th_from")}</TH>
                <TH>{t("th_language")}</TH>
                <TH>{t("th_status")}</TH>
                <TH>{t("th_started")}</TH>
              </>
            }
          >
            {stats.recent_calls.map((c) => (
              <TR key={c.id}>
                <TD>
                  <Link href={`/admin/calls/${c.id}`} className="text-blue-600 hover:underline">
                    {c.id}
                  </Link>
                </TD>
                <TD>
                  <MaskedPhone value={c.from_number} />
                </TD>
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

      <Card>
        <CardHeader title={t("gap_title")} subtitle={t("gap_sub")} />
        <CardBody>
          <div className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
            {GAP_KEYS.map((k) => (
              <div key={k} className="flex items-center justify-between rounded border border-slate-200 px-3 py-2">
                <span className="text-slate-600">{t(k)}</span>
                <Badge tone="neutral">{t("gap_requires")}</Badge>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-400">{t("gap_footer")}</p>
        </CardBody>
      </Card>
    </div>
  );
}
