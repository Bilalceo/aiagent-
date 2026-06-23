"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { getToken, logout, me } from "@/lib/auth";
import { useLanguage } from "@/lib/i18n";
import type { AuthUser } from "@/lib/types";

// Manager dashboard is a separate, non-technical surface. Accessible to
// manager / admin / super_admin only (operators are not shown manager pages).
const ALLOWED = new Set(["manager", "admin", "super_admin"]);

const NAV = [
  { href: "/manager", labelKey: "mgr_nav_home" },
  { href: "/manager/schedule", labelKey: "mgr_nav_schedule" },
  { href: "/manager/notifications", labelKey: "mgr_nav_notifications" },
  { href: "/manager/reports", labelKey: "mgr_nav_reports" },
  { href: "/manager/doctors", labelKey: "mgr_nav_doctors" },
];

export default function ManagerLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { t } = useLanguage();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    me()
      .then((u) => {
        if (u.force_password_change) {
          router.replace("/change-password");
          return;
        }
        setUser(u);
      })
      .catch(() => {
        logout();
        router.replace("/login");
      })
      .finally(() => setChecking(false));
  }, [router]);

  function onLogout() {
    logout();
    router.replace("/login");
  }

  if (checking) return <p className="text-sm text-slate-500">{t("checking_session")}</p>;
  if (!user) return null;

  // Unauthorized role: translated access-denied (no redirect loop).
  if (!ALLOWED.has(user.role)) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-sm text-red-700">
        {t("mgr_access_denied")}
      </div>
    );
  }

  function isActive(href: string): boolean {
    if (href === "/manager") return pathname === "/manager";
    return pathname === href || pathname.startsWith(href + "/");
  }

  return (
    <div className="flex gap-6">
      <aside className="w-56 shrink-0">
        <div className="mb-4 rounded-lg border border-slate-200 bg-white p-3">
          <div className="text-sm font-semibold text-slate-900">{t("shell_title")}</div>
          <div className="text-xs text-slate-500">{t("mgr_brand_sub")}</div>
        </div>
        <nav className="flex flex-col gap-0.5 text-sm">
          {NAV.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className={`rounded px-2 py-1.5 ${
                isActive(n.href)
                  ? "bg-blue-50 font-medium text-blue-700"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              {t(n.labelKey)}
            </Link>
          ))}
        </nav>
        <div className="mt-6 border-t border-slate-200 pt-3 text-xs text-slate-500">
          <div className="font-medium text-slate-700">{user.full_name || user.email}</div>
          <div className="mt-0.5">{t(`role_${user.role}`)}</div>
          <button
            onClick={onLogout}
            className="mt-2 rounded border border-slate-300 px-2 py-1 text-slate-700 hover:bg-slate-100"
          >
            {t("logout")}
          </button>
        </div>
      </aside>
      <section className="min-w-0 flex-1">{children}</section>
    </div>
  );
}
