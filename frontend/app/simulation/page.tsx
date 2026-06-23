"use client";

import { SimulationChat } from "@/components/SimulationChat";
import { useLanguage } from "@/lib/i18n";

export default function SimulationPage() {
  const { t } = useLanguage();
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">{t("sim_title")}</h1>
      <p className="text-slate-600">{t("sim_subtitle")}</p>
      <SimulationChat />
    </div>
  );
}
