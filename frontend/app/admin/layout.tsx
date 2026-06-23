"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { getToken, logout, me } from "@/lib/auth";
import { useLanguage } from "@/lib/i18n";
import type { AuthUser, Role } from "@/lib/types";

type RoleReq = "operator" | "admin" | "super_admin";
type NavItem = { href: string; labelKey: string; min: RoleReq };
type NavGroup = { titleKey: string; items: NavItem[] };

// Grouped navigation. `min` is the lowest role allowed (rank-based below). All
// existing pages are preserved; only grouping + the new Provider Readiness link
// are added. Role gating matches the existing backend route guards. Labels are
// translation keys (UZ/RU) resolved at render time.
const GROUPS: NavGroup[] = [
  {
    titleKey: "grp_overview",
    items: [{ href: "/admin", labelKey: "nav_overview", min: "operator" }],
  },
  {
    titleKey: "grp_calls",
    items: [
      { href: "/admin/calls", labelKey: "nav_call_history", min: "operator" },
      { href: "/admin/telephony-calls", labelKey: "nav_telephony", min: "admin" },
    ],
  },
  {
    titleKey: "grp_voice",
    items: [
      { href: "/admin/provider-readiness", labelKey: "nav_readiness", min: "admin" },
      { href: "/admin/audio-recordings", labelKey: "nav_audio", min: "admin" },
    ],
  },
  {
    titleKey: "grp_content",
    items: [{ href: "/admin/knowledge-base", labelKey: "nav_kb", min: "operator" }],
  },
  {
    titleKey: "grp_ops",
    items: [
      { href: "/admin/callbacks", labelKey: "nav_callbacks", min: "operator" },
      { href: "/admin/audit-logs", labelKey: "nav_audit", min: "admin" },
    ],
  },
  {
    titleKey: "grp_security",
    items: [
      { href: "/admin/security", labelKey: "nav_security", min: "operator" },
      { href: "/admin/users", labelKey: "nav_users", min: "super_admin" },
    ],
  },
];

const RANK: Record<RoleReq, number> = { operator: 1, admin: 2, super_admin: 3 };
function roleRank(role: Role): number {
  return RANK[role as RoleReq] ?? 0;
}

export default function AdminLayout({ children }: { children: React.ReactNode }) {
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

  const rank = roleRank(user.role);
  const groups = GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => rank >= RANK[i.min]),
  })).filter((g) => g.items.length > 0);

  function isActive(href: string): boolean {
    if (href === "/admin") return pathname === "/admin";
    return pathname === href || pathname.startsWith(href + "/");
  }

  return (
    <div className="flex gap-6">
      <aside className="w-56 shrink-0">
        <div className="mb-4 rounded-lg border border-slate-200 bg-white p-3">
          <div className="text-sm font-semibold text-slate-900">{t("shell_title")}</div>
          <div className="text-xs text-slate-500">{t("shell_sub")}</div>
        </div>
        <nav className="space-y-4 text-sm">
          {groups.map((g) => (
            <div key={g.titleKey}>
              <div className="mb-1 px-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                {t(g.titleKey)}
              </div>
              <div className="flex flex-col gap-0.5">
                {g.items.map((n) => (
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
              </div>
            </div>
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
