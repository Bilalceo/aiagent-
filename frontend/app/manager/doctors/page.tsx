"use client";

import { useLanguage } from "@/lib/i18n";
import { PageHeader, Card, CardBody, CardHeader, EmptyState } from "@/components/ui";

export default function ManagerDoctors() {
  const { t } = useLanguage();
  return (
    <div className="space-y-4">
      <PageHeader title={t("mgr_doctors_title")} />
      <EmptyState title={t("mgr_doctors_title")} hint={t("mgr_gap_doctors")} />

      {/* Doctor entity not built yet - explicit backend gap, no fake doctors. */}
      <Card>
        <CardHeader title={t("mgr_doctors_title")} />
        <CardBody>
          <p className="text-sm text-slate-500">{t("mgr_gap_doctors")}</p>
        </CardBody>
      </Card>
    </div>
  );
}
