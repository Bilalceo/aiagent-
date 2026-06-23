"use client";

import { useEffect, useState } from "react";
import {
  confirm2fa,
  disable2fa,
  enroll2fa,
  me,
  regenerateRecoveryCodes,
} from "@/lib/auth";
import { useLanguage } from "@/lib/i18n";

export default function SecurityPage() {
  const { t } = useLanguage();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [secret, setSecret] = useState("");
  const [otpUri, setOtpUri] = useState("");
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    me().then((u) => setEnabled(u.two_factor_enabled)).catch(() => setEnabled(null));
  }, []);

  function run(fn: () => Promise<void>) {
    return async () => {
      setBusy(true);
      setError(null);
      try {
        await fn();
      } catch (e) {
        setError(e instanceof Error ? e.message : t("failed_generic"));
      } finally {
        setBusy(false);
      }
    };
  }

  const onEnroll = run(async () => {
    const r = await enroll2fa();
    setSecret(r.secret);
    setOtpUri(r.otpauth_uri);
    setRecovery(null);
  });

  const onConfirm = run(async () => {
    const r = await confirm2fa(code.trim());
    setRecovery(r.recovery_codes);
    setSecret("");
    setOtpUri("");
    setCode("");
    setEnabled(true);
  });

  const onDisable = run(async () => {
    await disable2fa(code.trim());
    setCode("");
    setRecovery(null);
    setEnabled(false);
  });

  const onRegenerate = run(async () => {
    const r = await regenerateRecoveryCodes(code.trim());
    setRecovery(r.recovery_codes);
    setCode("");
  });

  if (enabled === null) return <p className="text-slate-500">{t("loading")}</p>;

  return (
    <div className="max-w-lg space-y-5">
      <h1 className="text-xl font-semibold">{t("sec_title")}</h1>
      <p className="text-sm text-slate-600">
        {t("sec_status")}: <span className="font-medium">{enabled ? t("sec_enabled") : t("sec_disabled")}</span>
      </p>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {recovery && (
        <div className="rounded-lg border bg-green-50 p-3 text-sm">
          <div className="mb-1 font-semibold">{t("sec_recovery_title")}</div>
          <ul className="grid grid-cols-2 gap-x-4 font-mono text-xs">
            {recovery.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {!enabled && !secret && (
        <button onClick={onEnroll} disabled={busy} className="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50">
          {t("sec_start")}
        </button>
      )}

      {!enabled && secret && (
        <div className="space-y-3 rounded-lg border bg-white p-4 text-sm">
          <div>
            <div className="text-slate-600">{t("sec_add_secret")}</div>
            <div className="mt-1 break-all font-mono">{secret}</div>
            <div className="mt-1 break-all text-xs text-slate-500">{otpUri}</div>
          </div>
          <input
            className="w-full rounded border px-3 py-2"
            placeholder={t("sec_6digit")}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button onClick={onConfirm} disabled={busy} className="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-50">
            {t("sec_confirm_enable")}
          </button>
        </div>
      )}

      {enabled && (
        <div className="space-y-3 rounded-lg border bg-white p-4 text-sm">
          <input
            className="w-full rounded border px-3 py-2"
            placeholder={t("sec_current_or_recovery")}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <div className="flex gap-2">
            <button onClick={onRegenerate} disabled={busy} className="rounded border px-3 py-2 hover:bg-gray-100">
              {t("sec_regenerate")}
            </button>
            <button onClick={onDisable} disabled={busy} className="rounded border border-red-300 px-3 py-2 text-red-700 hover:bg-red-50">
              {t("sec_disable")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
