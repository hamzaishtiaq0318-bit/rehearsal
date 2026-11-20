import { ArrowUp, Loader2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import type { StartMode, Status } from "../api";
import { MODES } from "./words";

type Mode = StartMode;

export interface ComposerSubmit {
  message: string;
  ticketId: number | null;
  mode: Mode;
}

interface Props {
  status: Status | null;
  ticketIds: number[];
  canSend: boolean;
  busy: boolean;
  placeholder?: string;
  initial?: string;
  autoFocus?: boolean;
  onSubmit: (value: ComposerSubmit) => void;
}

/** Finds a ticket the person mentioned, such as "1006" or "ticket #1002". */
export function findTicket(text: string, ticketIds: number[]): number | null {
  for (const match of text.matchAll(/\d{3,8}/g)) {
    const id = Number(match[0]);
    if (ticketIds.includes(id)) return id;
  }
  return null;
}

export function Composer({ status, ticketIds, canSend, busy, placeholder, initial = "", autoFocus, onSubmit }: Props) {
  const [text, setText] = useState(initial);
  const [mode, setMode] = useState<Mode>("agent");
  const area = useRef<HTMLTextAreaElement>(null);
  const ids = { input: useId(), modes: useId(), hint: useId() };

  const ticket = findTicket(text, ticketIds);
  const liveAgent = (status?.models.length ?? 0) > 0;
  const available: Record<Mode, boolean> = {
    agent: liveAgent,
    replay: ticket !== null && (status?.recordings.includes(ticket) ?? false),
    simulated: ticket !== null && (ticket === status?.simulation_ticket ||
      Boolean(status?.simulations?.some((s) => s.ticket_id === ticket))),
  };
  // Without a model key on the server, a recorded run stands in for the live agent.
  const effectiveMode: Mode | null = available[mode]
    ? mode
    : (["agent", "replay", "simulated"] as const).find((m) => available[m]) ?? null;

  useEffect(() => setText(initial), [initial]);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
    el.style.overflowY = el.scrollHeight > 220 ? "auto" : "hidden";
  }, [text]);

  useEffect(() => {
    if (autoFocus) area.current?.focus();
  }, [autoFocus]);

  const trimmed = text.trim();
  const needsTicket = (Boolean(status?.public_demo) || !liveAgent) && ticket === null;
  const ready = canSend && !busy && trimmed.length >= 2 && !needsTicket && effectiveMode !== null;

  function submit() {
    if (!ready || effectiveMode === null) return;
    onSubmit({ message: trimmed, ticketId: ticket, mode: effectiveMode });
  }

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label htmlFor={ids.input} className="visually-hidden">
        Message the agent
      </label>
      <textarea
        id={ids.input}
        ref={area}
        rows={1}
        value={text}
        maxLength={500}
        placeholder={placeholder ?? "Ask the agent to handle a ticket, for example: please resolve ticket 1002"}
        aria-describedby={ids.hint}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="composer-bar">
        <div className="mode-picker" role="radiogroup" aria-label="How to run it" id={ids.modes}>
          {(Object.keys(MODES) as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={effectiveMode === m}
              disabled={!available[m]}
              title={available[m] ? MODES[m].hint : m === "agent" ? "" : "Mention a ticket that supports this first"}
              className="mode-chip"
              aria-label={MODES[m].label}
              onClick={() => setMode(m)}
            >
              <span className="label-long">{MODES[m].label}</span>
              <span className="label-short" aria-hidden="true">{MODES[m].short}</span>
            </button>
          ))}
        </div>
        <button type="submit" className="send" disabled={!ready} aria-label="Send">
          {busy ? <Loader2 size={18} className="spin" aria-hidden="true" /> : <ArrowUp size={18} aria-hidden="true" />}
        </button>
      </div>
      <p id={ids.hint} className="composer-hint">
        {!canSend
          ? "Read only. Open the link printed when Rehearsal started to send requests."
          : needsTicket
            ? status?.static_demo
              ? "In this browser demo, pick a run above or mention its ticket, for example: please resolve ticket 1002."
              : status?.public_demo
              ? "In the public demo, mention a ticket number, for example: please resolve ticket 1002."
              : "No model key is set, so mention a ticket with a recorded run, for example: please resolve ticket 1002."
            : ticket && effectiveMode
            ? `Ticket ${ticket} attached. ${MODES[effectiveMode].hint}`
            : ticket
            ? `Ticket ${ticket} has no recorded run, and no model key is set on this server.`
            : "Nothing touches live data until you approve. Enter to send, Shift and Enter for a new line."}
      </p>
    </form>
  );
}
