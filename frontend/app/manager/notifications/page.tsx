"use client";

import { useEffect, useState } from "react";
import { getManagerActionItems, getManagerRecentCalls } from "@/lib/manager";
import type { ManagerActionItem, ManagerCall } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";
import {
  PageHeader,
  Card,
  CardBody,
  Badge,
  LoadingState,
  ErrorState,
  EmptyState,
} from "@/components/ui";

type Priority = "urgent" | "high" | "medium" | "low";
type Notif = {
  id: string;
  typeKey: string;
  priority: Priority;
  detail: string;
  at: string | null;
};

function priorityTone(p: Priority): "danger" | "warning" | "info" | "neutral" {
  return p === "urgent" ? "danger" : p === "high" ? "warning" : p === "medium" ? "info" : "neutral";
}

// Derive a manager notification feed from existing callbacks + calls (M1a MVP;
// a dedicated notification entity is a future task).
function deriveNotifications(items: ManagerActionItem[], calls: ManagerCall[]): Notif[] {
  const out: Notif[] = [];
  for (const it of items) {
    const overdue = it.due_at && new Date(it.due_at).getTime() < Date.now();
    out.push({
      id: `cb-${it.id}`,
      typeKey: it.reason === "operator_request" ? "ntype_operator_required"
        : overdue ? "ntype_callback_overdue" : "ntype_callback_required",
      priority: (it.priority as Priority) || "medium",
      detail: it.phone_masked ?? "-",
      at: it.created_at,
    });
  }
  for (const c of calls) {
    if (c.status === "transferred" || c.status === "emergency") {
      out.push({
        id: `call-${c.id}`,
        typeKey: "ntype_emergency",
        priority: "urgent",
        detail: c.from_masked ?? "-",
        at: c.started_at,
      });
    }
  }
  return out;
}

export default function ManagerNotifications() {
  const { t } = useLanguage();
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const [read, setRead] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getManagerActionItems(), getManagerRecentCalls()])
      .then(([items, calls]) => setNotifs(deriveNotifications(items, calls)))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;

  const unread = notifs.filter((n) => !read.has(n.id)).length;

  return (
    <div className="space-y-4">
      <PageHeader
        title={t("mgr_notif_title")}
        subtitle={`${unread} ${t("mgr_unread")}`}
        actions={
          notifs.length > 0 ? (
            <button
              className="rounded border border-slate-300 px-3 py-1 text-sm text-slate-700 hover:bg-slate-100"
              onClick={() => setRead(new Set(notifs.map((n) => n.id)))}
            >
              {t("mgr_mark_all_read")}
            </button>
          ) : null
        }
      />

      {notifs.length === 0 ? (
        <EmptyState title={t("mgr_notif_empty")} />
      ) : (
        <div className="space-y-2">
          {notifs.map((n) => {
            const isRead = read.has(n.id);
            return (
              <Card key={n.id} className={isRead ? "opacity-60" : ""}>
                <CardBody className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <Badge tone={priorityTone(n.priority)}>{t(n.typeKey)}</Badge>
                    <span className="font-mono text-sm text-slate-700">{n.detail}</span>
                    <span className="text-xs text-slate-400">
                      {n.at ? n.at.replace("T", " ").slice(0, 16) : "-"}
                    </span>
                  </div>
                  {!isRead && (
                    <button
                      className="text-xs text-blue-600 hover:underline"
                      onClick={() => setRead((prev) => new Set(prev).add(n.id))}
                    >
                      {t("mgr_mark_read")}
                    </button>
                  )}
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
