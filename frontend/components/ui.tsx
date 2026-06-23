"use client";

// U1 shared UI kit - professional clinic dashboard primitives (Tailwind).
// No emojis. Neutral slate palette with restrained status colors. User-facing
// default text comes from the i18n layer (Uzbek/Russian only).
import React from "react";
import { useLanguage } from "@/lib/i18n";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

const TONES: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-700 border-slate-200",
  info: "bg-blue-50 text-blue-700 border-blue-200",
  success: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warning: "bg-amber-50 text-amber-700 border-amber-200",
  danger: "bg-red-50 text-red-700 border-red-200",
};

// --- Card -------------------------------------------------------------------
export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-slate-200 bg-white shadow-sm ${className}`}>{children}</div>;
}

export function CardBody({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`p-4 ${className}`}>{children}</div>;
}

export function CardHeader({
  title,
  subtitle,
  actions,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between border-b border-slate-200 px-4 py-3">
      <div>
        <div className="text-sm font-semibold text-slate-800">{title}</div>
        {subtitle ? <div className="mt-0.5 text-xs text-slate-500">{subtitle}</div> : null}
      </div>
      {actions ? <div className="flex gap-2">{actions}</div> : null}
    </div>
  );
}

// --- Headers ----------------------------------------------------------------
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-slate-500">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex gap-2">{actions}</div> : null}
    </div>
  );
}

export function SectionHeader({ title, hint }: { title: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
      {hint ? <span className="text-xs text-slate-400">{hint}</span> : null}
    </div>
  );
}

// --- Badges -----------------------------------------------------------------
export function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: Tone }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  );
}

const STATUS_TONES: Record<string, Tone> = {
  ok: "success", ready: "success", online: "success", active: "success",
  completed: "success", confirmed: "success", allow: "success", answered: "success",
  warning: "warning", pending: "warning", callback_required: "warning",
  needs_operator: "warning", transfer: "warning", transferred: "warning", new: "info",
  error: "danger", failed: "danger", emergency: "danger", not_ready: "danger",
  degraded: "danger", cancelled: "danger", missed: "danger",
};

export function StatusBadge({ status }: { status?: string | null }) {
  const { tStatus } = useLanguage();
  const key = (status || "").toLowerCase();
  return <Badge tone={STATUS_TONES[key] || "neutral"}>{tStatus(status)}</Badge>;
}

export function BoolPill({
  value,
  trueLabel,
  falseLabel,
  goodWhenTrue = true,
}: {
  value: boolean;
  trueLabel?: string;
  falseLabel?: string;
  goodWhenTrue?: boolean;
}) {
  const { t } = useLanguage();
  const good = goodWhenTrue ? value : !value;
  const yes = trueLabel ?? t("yes");
  const no = falseLabel ?? t("no");
  return <Badge tone={good ? "success" : "warning"}>{value ? yes : no}</Badge>;
}

// --- MetricCard -------------------------------------------------------------
export function MetricCard({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: Tone;
}) {
  const valueColor =
    tone === "danger" ? "text-red-700" : tone === "warning" ? "text-amber-700" : "text-slate-900";
  return (
    <Card>
      <CardBody>
        <div className={`text-2xl font-semibold ${valueColor}`}>{value}</div>
        <div className="mt-1 text-xs font-medium text-slate-500">{label}</div>
        {hint ? <div className="mt-1 text-[11px] text-slate-400">{hint}</div> : null}
      </CardBody>
    </Card>
  );
}

// --- Table ------------------------------------------------------------------
export function Table({ head, children }: { head: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            {head}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function TH({ children, className = "" }: { children?: React.ReactNode; className?: string }) {
  return <th className={`px-3 py-2 font-medium ${className}`}>{children}</th>;
}

export function TD({ children, className = "" }: { children?: React.ReactNode; className?: string }) {
  return <td className={`px-3 py-2 align-top ${className}`}>{children}</td>;
}

export function TR({ children }: { children: React.ReactNode }) {
  return <tr className="border-b border-slate-100 last:border-0 hover:bg-slate-50">{children}</tr>;
}

// --- States -----------------------------------------------------------------
export function LoadingState({ label }: { label?: string }) {
  const { t } = useLanguage();
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
      {label ?? t("loading")}
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  const { t } = useLanguage();
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
      {t("error")}: {message}
    </div>
  );
}

export function EmptyState({ title, hint }: { title?: string; hint?: React.ReactNode }) {
  const { t } = useLanguage();
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center">
      <div className="text-sm font-medium text-slate-600">{title ?? t("no_data")}</div>
      {hint ? <div className="mt-1 text-xs text-slate-400">{hint}</div> : null}
    </div>
  );
}

// --- SafetyBanner -----------------------------------------------------------
export function SafetyBanner({ children }: { children: React.ReactNode }) {
  const { t } = useLanguage();
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
      <span className="font-semibold">{t("safety_label")}</span> {children}
    </div>
  );
}

// --- MaskedPhone ------------------------------------------------------------
export function maskPhone(raw?: string | null): string {
  const s = (raw || "").trim();
  if (!s) return "-";
  if (s.length <= 4) return "*".repeat(s.length);
  return s.slice(0, 3) + "*".repeat(Math.max(2, s.length - 5)) + s.slice(-2);
}

export function MaskedPhone({ value }: { value?: string | null }) {
  return <span className="font-mono text-slate-700">{maskPhone(value)}</span>;
}

// --- SimpleTabs -------------------------------------------------------------
export function SimpleTabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="flex gap-1 border-b border-slate-200">
      {tabs.map((tab) => (
        <button
          key={tab.key}
          onClick={() => onChange(tab.key)}
          className={`-mb-px border-b-2 px-3 py-2 text-sm ${
            active === tab.key
              ? "border-blue-600 font-medium text-blue-700"
              : "border-transparent text-slate-500 hover:text-slate-700"
          }`}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}
