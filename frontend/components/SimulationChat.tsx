"use client";

import { useState } from "react";
import { sendMessage, startCall } from "@/lib/api";
import { useLanguage } from "@/lib/i18n";
import type { ChatTurn } from "@/lib/types";

export function SimulationChat() {
  const { t } = useLanguage();
  const [callId, setCallId] = useState<number | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ensureCall(): Promise<number> {
    if (callId !== null) return callId;
    const { call_id } = await startCall("+998901112233");
    setCallId(call_id);
    return call_id;
  }

  async function onSend() {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    setTurns((prev) => [...prev, { role: "user", text: trimmed }]);
    setText("");
    try {
      const id = await ensureCall();
      const res = await sendMessage(id, trimmed);
      setTurns((prev) => [
        ...prev,
        { role: "assistant", text: res.reply, action: res.action, transferred: res.transferred },
      ]);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border bg-white">
      <div className="h-96 space-y-3 overflow-y-auto p-4">
        {turns.length === 0 && <p className="text-sm text-slate-400">{t("sim_hint")}</p>}
        {turns.map((turn, i) => (
          <div key={i} className={turn.role === "user" ? "text-right" : "text-left"}>
            <span
              className={
                "inline-block max-w-[80%] rounded-lg px-3 py-2 text-sm " +
                (turn.role === "user" ? "bg-blue-600 text-white" : "bg-gray-100")
              }
            >
              {turn.text}
              {turn.action && turn.action !== "allow" && (
                <span className="ml-2 rounded bg-amber-200 px-1 text-xs text-amber-900">
                  {turn.action === "emergency" ? t("sim_badge_emergency") : t("sim_badge_operator")}
                </span>
              )}
            </span>
          </div>
        ))}
      </div>
      {error && <div className="px-4 text-sm text-red-600">{error}</div>}
      <div className="flex gap-2 border-t p-3">
        <input
          className="flex-1 rounded border px-3 py-2 text-sm"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSend()}
          placeholder={t("sim_input_placeholder")}
        />
        <button
          className="rounded bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50"
          onClick={onSend}
          disabled={busy}
        >
          {t("sim_send")}
        </button>
      </div>
    </div>
  );
}
