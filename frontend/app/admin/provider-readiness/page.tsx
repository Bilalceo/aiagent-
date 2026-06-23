"use client";

import { useEffect, useState } from "react";
import { getReadiness } from "@/lib/admin";
import type { VoiceReadiness } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";
import {
  PageHeader,
  Card,
  CardBody,
  CardHeader,
  Badge,
  StatusBadge,
  BoolPill,
  LoadingState,
  ErrorState,
} from "@/components/ui";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-slate-100 py-2 last:border-0">
      <span className="text-sm text-slate-600">{label}</span>
      <span className="text-sm">{children}</span>
    </div>
  );
}

export default function ProviderReadinessPage() {
  const { t } = useLanguage();
  const [data, setData] = useState<VoiceReadiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getReadiness()
      .then(setData)
      .catch(() => setError(t("admin_required")))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  const s = data.summary;
  const redactionWarn = data.warnings.find((w) => w.toLowerCase().includes("redaction only applies"));

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("rd_title")}
        subtitle={t("rd_subtitle")}
        actions={<StatusBadge status={data.ready ? "ready" : "not_ready"} />}
      />

      {redactionWarn ? (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <span className="font-semibold">{t("rd_redaction_label")}</span> {redactionWarn}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title={`${t("rd_errors")} (${data.errors.length})`} subtitle={t("rd_errors_sub")} />
          <CardBody>
            {data.errors.length === 0 ? (
              <p className="text-sm text-emerald-700">{t("rd_no_errors")}</p>
            ) : (
              <ul className="space-y-2">
                {data.errors.map((e, i) => (
                  <li key={i} className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                    {e}
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title={`${t("rd_warnings")} (${data.warnings.length})`} subtitle={t("rd_warnings_sub")} />
          <CardBody>
            {data.warnings.length === 0 ? (
              <p className="text-sm text-slate-500">{t("rd_no_warnings")}</p>
            ) : (
              <ul className="space-y-2">
                {data.warnings.map((w, i) => (
                  <li key={i} className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                    {w}
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title={t("rd_pipeline")} />
          <CardBody>
            <Row label={t("r_twilio_media")}>
              <BoolPill value={s.twilio_media_streams_enabled} trueLabel={t("enabled")} falseLabel={t("disabled")} />
            </Row>
            <Row label={t("r_streaming_stt")}>
              <BoolPill value={s.streaming_stt_enabled} trueLabel={t("enabled")} falseLabel={t("disabled")} />
            </Row>
            <Row label={t("r_streaming_tts")}>
              <BoolPill value={s.streaming_tts_enabled} trueLabel={t("enabled")} falseLabel={t("disabled")} />
            </Row>
            <Row label={t("r_ai_turns")}>
              <BoolPill value={s.ai_turns_enabled} trueLabel={t("enabled")} falseLabel={t("disabled")} />
            </Row>
            <Row label={t("r_barge")}>
              <BoolPill value={s.barge_in_enabled} trueLabel={t("enabled")} falseLabel={t("disabled")} />
            </Row>
            <Row label={t("r_latency_metrics")}>
              <BoolPill value={s.metrics_enabled} trueLabel={t("enabled")} falseLabel={t("disabled")} />
            </Row>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title={t("rd_providers")} subtitle={t("rd_providers_sub")} />
          <CardBody>
            <Row label={t("f_stt_provider")}>
              <Badge tone={s.streaming_stt_provider === "deepgram" ? "info" : "neutral"}>
                {s.streaming_stt_provider}
              </Badge>
            </Row>
            <Row label={t("f_tts_provider")}>
              <Badge tone={s.streaming_tts_provider === "deepgram" ? "info" : "neutral"}>
                {s.streaming_tts_provider}
              </Badge>
            </Row>
            <Row label={t("r_dg_key_present")}>
              <BoolPill value={s.deepgram_api_key_present} trueLabel={t("key_present")} falseLabel={t("key_hidden")} />
            </Row>
            <Row label={t("r_stt_model")}>
              <span className="font-mono text-xs text-slate-600">
                {s.deepgram_stt.model} ({s.deepgram_stt.encoding}/{s.deepgram_stt.sample_rate})
              </span>
            </Row>
            <Row label={t("r_tts_model")}>
              <span className="font-mono text-xs text-slate-600">
                {s.deepgram_tts.model} ({s.deepgram_tts.encoding}/{s.deepgram_tts.sample_rate}/{s.deepgram_tts.container})
              </span>
            </Row>
            <Row label={t("r_stt_twilio")}>
              <BoolPill value={s.stt_twilio_compatible} />
            </Row>
            <Row label={t("r_tts_twilio")}>
              <BoolPill value={s.tts_twilio_compatible} />
            </Row>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title={t("rd_smoke_gate")} />
          <CardBody>
            <Row label={t("f_smoke_mode")}>
              <Badge tone={s.smoke_mode_enabled ? "warning" : "neutral"}>
                {s.smoke_mode_enabled ? t("enabled") : t("disabled")}
              </Badge>
            </Row>
            <Row label={t("r_require_token")}>
              <BoolPill value={s.require_smoke_token} />
            </Row>
            <Row label={t("r_token_present")}>
              <BoolPill value={s.smoke_token_present} trueLabel={t("key_present")} falseLabel={t("key_hidden")} />
            </Row>
            <Row label={t("r_allowed_numbers")}>
              <span className="text-slate-700">{s.allowed_caller_numbers_count}</span>
            </Row>
            <Row label={t("r_max_duration")}>
              <span className="text-slate-700">{s.max_call_duration_seconds}</span>
            </Row>
            <Row label={t("r_max_turns")}>
              <span className="text-slate-700">{s.max_turns}</span>
            </Row>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title={t("rd_privacy")} />
          <CardBody>
            <Row label={t("r_redact")}>
              <BoolPill value={s.redact_transcripts} />
            </Row>
            <Row label={t("r_no_patient")}>
              <BoolPill value={s.no_patient_data_notice} />
            </Row>
            <p className="mt-3 text-xs text-slate-400">{t("rd_privacy_note")}</p>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
