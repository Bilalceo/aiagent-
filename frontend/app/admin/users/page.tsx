"use client";

import { useEffect, useState } from "react";
import { getUser } from "@/lib/auth";
import {
  activateUser,
  createUser,
  deactivateUser,
  listUsers,
  resetPassword,
  resetTwoFactor,
  updateUser,
} from "@/lib/users";
import { useLanguage } from "@/lib/i18n";
import type { ManagedUser, Role } from "@/lib/types";

const ROLES: Role[] = ["super_admin", "admin", "operator"];

export default function UsersPage() {
  const { t } = useLanguage();
  const me = getUser();
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [role, setRole] = useState<Role>("operator");
  const [password, setPassword] = useState("");

  function load() {
    setLoading(true);
    listUsers()
      .then(setUsers)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (me?.role === "super_admin") load();
    else setLoading(false);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (me?.role !== "super_admin") {
    return <p className="text-sm text-red-600">{t("users_forbidden")}</p>;
  }

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("action_failed"));
    }
  }

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    await act(async () => {
      await createUser({ email, full_name: fullName, role, password });
      setEmail("");
      setFullName("");
      setPassword("");
      setRole("operator");
    });
  }

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">{t("users_title")}</h1>
      {error && <p className="text-sm text-red-600">{error}</p>}

      <form onSubmit={onCreate} className="flex flex-wrap items-end gap-2 rounded-lg border bg-white p-3 text-sm">
        <Field label={t("th_email")}><input className="rounded border px-2 py-1" value={email} onChange={(e) => setEmail(e.target.value)} required /></Field>
        <Field label={t("th_full_name")}><input className="rounded border px-2 py-1" value={fullName} onChange={(e) => setFullName(e.target.value)} /></Field>
        <Field label={t("th_role")}>
          <select className="rounded border px-2 py-1" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => <option key={r} value={r}>{t(`role_${r}`)}</option>)}
          </select>
        </Field>
        <Field label={t("u_temp_password")}><input className="rounded border px-2 py-1" value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
        <button type="submit" className="rounded bg-blue-600 px-3 py-1.5 text-white">{t("u_create")}</button>
      </form>

      {loading ? (
        <p className="text-slate-500">{t("loading")}</p>
      ) : users.length === 0 ? (
        <p className="text-sm text-slate-400">{t("users_empty")}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-slate-500">
              <th className="py-2">{t("th_email")}</th>
              <th>{t("th_full_name")}</th>
              <th>{t("th_role")}</th>
              <th>{t("th_active")}</th>
              <th>2FA</th>
              <th>{t("th_last_login")}</th>
              <th>{t("th_actions")}</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-b align-middle hover:bg-gray-50">
                <td className="py-2">{u.email}</td>
                <td>{u.full_name ?? "-"}</td>
                <td>
                  <select
                    className="rounded border px-1 py-0.5"
                    value={u.role}
                    onChange={(e) => act(() => updateUser(u.id, { role: e.target.value as Role }))}
                  >
                    {ROLES.map((r) => <option key={r} value={r}>{t(`role_${r}`)}</option>)}
                  </select>
                </td>
                <td>{u.is_active ? t("yes") : t("no")}</td>
                <td>{u.two_factor_enabled ? t("yes") : t("no")}</td>
                <td>{u.last_login_at?.replace("T", " ").slice(0, 19) ?? "-"}</td>
                <td className="space-x-1">
                  {u.is_active ? (
                    <button className="rounded border px-2 py-0.5" onClick={() => act(() => deactivateUser(u.id))}>{t("act_deactivate")}</button>
                  ) : (
                    <button className="rounded border px-2 py-0.5" onClick={() => act(() => activateUser(u.id))}>{t("act_activate")}</button>
                  )}
                  <button
                    className="rounded border px-2 py-0.5"
                    onClick={() => {
                      const pw = window.prompt(`${u.email}: ${t("u_prompt_pw")}`);
                      if (pw) act(() => resetPassword(u.id, pw));
                    }}
                  >
                    {t("u_reset_pw")}
                  </button>
                  <button
                    className="rounded border px-2 py-0.5"
                    onClick={() => {
                      if (window.confirm(`${u.email}: ${t("u_confirm_2fa")}`)) act(() => resetTwoFactor(u.id));
                    }}
                  >
                    {t("u_reset_2fa")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-slate-500">{label}</span>
      {children}
    </label>
  );
}
