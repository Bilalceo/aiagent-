"use client";

import Link from "next/link";
import { useLanguage } from "@/lib/i18n";

export default function DashboardPage() {
  const { t } = useLanguage();
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">{t("home_title")}</h1>
      <p className="text-slate-600">{t("home_intro")}</p>
      <div className="grid gap-4 sm:grid-cols-3">
        <Card title={t("home_card_calls")} href="/admin/calls" hint={t("home_card_calls_hint")} />
        <Card title={t("home_card_kb")} href="/admin/knowledge-base" hint={t("home_card_kb_hint")} />
        <Card title={t("home_card_sim")} href="/simulation" hint={t("home_card_sim_hint")} />
      </div>
    </div>
  );
}

function Card({ title, href, hint }: { title: string; href: string; hint: string }) {
  return (
    <Link href={href} className="rounded-lg border border-slate-200 bg-white p-4 hover:shadow">
      <div className="font-medium">{title}</div>
      <div className="mt-1 text-sm text-slate-500">{hint}</div>
    </Link>
  );
}
