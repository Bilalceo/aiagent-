"use client";

import Link from "next/link";
import { useLanguage, LanguageSwitcher } from "@/lib/i18n";

export default function SiteHeader() {
  const { t } = useLanguage();
  return (
    <header className="border-b border-slate-200 bg-white">
      <nav className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3 text-sm font-medium">
        <Link href="/" className="font-semibold text-slate-900">
          {t("brand")}
        </Link>
        <Link href="/simulation" className="text-slate-600 hover:text-slate-900">
          {t("nav_sim")}
        </Link>
        <Link href="/admin" className="text-slate-600 hover:text-slate-900">
          {t("nav_admin")}
        </Link>
        <span className="ml-auto">
          <LanguageSwitcher />
        </span>
      </nav>
    </header>
  );
}
