import { CircleAlert, Info, PanelLeftOpen, ShieldAlert, Wrench } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import type { ConversationRow, StartMode, Status, TicketRow } from "./api";
import { api, errorMessage, hasToken } from "./api";
import { AuditView } from "./components/AuditView";
import type { ComposerSubmit } from "./components/Composer";
import { Composer } from "./components/Composer";
import { Conversation } from "./components/Conversation";
import { Logo } from "./components/Logo";
import { Sidebar } from "./components/Sidebar";
import { greeting } from "./components/words";

type Route = { view: "new" } | { view: "chat"; id: string } | { view: "audit" };

function parseRoute(hash: string): Route {
  if (hash.startsWith("#/audit")) return { view: "audit" };
  const match = /^#\/c\/([a-f0-9]{6,32})$/.exec(hash);
  return match ? { view: "chat", id: match[1]! } : { view: "new" };
}

function useRoute(): [Route, (hash: string) => void] {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return [route, (hash) => (window.location.hash = hash)];
}

type Theme = "light" | "dark";

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem("rehearsal-theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage blocked: follow the system */
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

interface StartProps {
  status: Status | null;
  tickets: TicketRow[];
  canSend: boolean;
  busy: boolean;
  onStart: (body: { ticket_id?: number; message?: string; mode: StartMode }) => void;
}

function NewChat({ status, tickets, canSend, busy, onStart }: StartProps) {
  const open = tickets.filter((t) => t.status !== "closed");
  const attack = status?.simulation_ticket ?? 1006;
  const gallery = (status?.simulations ?? []).filter((s) => s.ticket_id !== attack);
  const staged = new Set(gallery.map((s) => s.ticket_id));
  const others = open.filter((t) => t.id !== attack && t.id !== 1002 && !staged.has(t.id));
  // With no model key on the server, each ticket's recorded real model run stands in for the live agent.
  const live = (status?.models.length ?? 0) > 0;
  const modeFor = (id: number): StartMode | null =>
    live ? "agent" : status?.recordings.includes(id) ? "replay" : null;
  return (
    <div className="new-chat-view">
      <div className="hello">
        <span className="hello-mark" aria-hidden="true"><Logo /></span>
        <h1>{greeting()}. What should the agent work on?</h1>
        <p>
          The agent works through tools Synapse generated from {status?.target ?? "the app"}. Everything it does runs
          on a private copy first, and I will ask you before anything touches the live store.
        </p>
      </div>

      <div className="starters" aria-label="Try one of these">
        <button type="button" className="starter" disabled={!canSend || busy}
          onClick={() => onStart({ ticket_id: attack, mode: "simulated" })}>
          <span className="starter-icon risk"><ShieldAlert size={18} aria-hidden="true" /></span>
          <span className="starter-title">Watch Rehearsal catch an attack</span>
          <span className="starter-text">Ticket {attack} hides an instruction to refund twelve strangers. A scripted
            agent falls for it.</span>
        </button>
        <button type="button" className="starter" disabled={!canSend || busy || !modeFor(1002)}
          onClick={() => onStart({ ticket_id: 1002, mode: modeFor(1002) ?? "agent" })}>
          <span className="starter-icon"><Wrench size={18} aria-hidden="true" /></span>
          <span className="starter-title">Let the AI agent fix a torn jacket</span>
          <span className="starter-text">
            {live ? "A real free model handles ticket 1002." : "A real model's recorded run handles ticket 1002."} You
            approve the refund it proposes.
          </span>
        </button>
      </div>

      {gallery.length > 0 && (
        <div className="more-tickets">
          <p>More attacks to try</p>
          <div className="ticket-chips">
            {gallery.map((s) => (
              <button key={s.ticket_id} type="button" className="ticket-chip attack-chip" disabled={!canSend || busy}
                onClick={() => onStart({ ticket_id: s.ticket_id, mode: "simulated" })}>
                <ShieldAlert size={15} aria-hidden="true" /> {s.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {others.length > 0 && (
        <div className="more-tickets">
          <p>Or pick another open ticket</p>
          <div className="ticket-chips">
            {others.map((t) => (
              <button key={t.id} type="button" className="ticket-chip" disabled={!canSend || busy || !modeFor(t.id)}
                onClick={() => onStart({ ticket_id: t.id, mode: modeFor(t.id) ?? "agent" })}>
                <span className="mono">{t.id}</span> {t.subject}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="composer-dock">
        <Composer status={status} ticketIds={tickets.map((t) => t.id)} canSend={canSend} busy={busy} autoFocus
          onSubmit={(v: ComposerSubmit) => onStart({ ticket_id: v.ticketId ?? undefined, message: v.message, mode: v.mode })} />
      </div>
    </div>
  );
}

export function App() {
  const [route, navigate] = useRoute();
  const [status, setStatus] = useState<Status | null>(null);
  const [tickets, setTickets] = useState<TicketRow[]>([]);
  const [conversations, setConversations] = useState<ConversationRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const refresh = useCallback(() => {
    Promise.all([api.status(), api.tickets(), api.conversations()])
      .then(([s, t, c]) => {
        setStatus(s);
        setTickets(t);
        setConversations(c);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);

  const anyRunning = conversations.some((c) => c.running);
  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, anyRunning ? 4_000 : 12_000);
    return () => window.clearInterval(timer);
  }, [refresh, anyRunning]);

  useEffect(() => {
    const current = route.view === "chat" ? conversations.find((c) => c.id === route.id) : null;
    document.title = `${route.view === "audit" ? "Audit log" : current?.title ?? "New conversation"} | Rehearsal`;
  }, [route, conversations]);

  // Any page change (a link, Back, Forward or a typed address) closes the phone menu.
  useEffect(() => setMenuOpen(false), [route]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  async function start(body: { ticket_id?: number; message?: string; mode: StartMode }) {
    setStarting(true);
    setError(null);
    try {
      const { id } = await api.start(body);
      navigate(`#/c/${id}`);
      refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setStarting(false);
    }
  }

  async function pauseAll() {
    let reviewer = "Reviewer";
    try {
      reviewer = localStorage.getItem("rehearsal-reviewer") || reviewer;
    } catch {
      /* storage blocked: the default name is used */
    }
    try {
      await api.pause(reviewer);
      refresh();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem("rehearsal-theme", next);
    } catch {
      /* lasts for this page view only */
    }
  }

  const canSend = hasToken() || Boolean(status?.public_demo);

  return (
    <div className="app" data-menu-open={menuOpen}>
      <a className="skip-link" href="#content">Skip to content</a>
      <Sidebar status={status} conversations={conversations} selected={route.view === "chat" ? route.id : null}
        view={route.view === "audit" ? "audit" : "chat"} theme={theme} open={menuOpen}
        onClose={() => setMenuOpen(false)} onToggleTheme={toggleTheme}
        onPause={hasToken() && !status?.static_demo ? pauseAll : undefined} />
      <button type="button" className="scrim" aria-hidden="true" tabIndex={-1} onClick={() => setMenuOpen(false)} />

      <div className="stage">
        <header className="mobile-bar">
          <button type="button" className="icon-button" onClick={() => setMenuOpen(true)} aria-label="Open the menu"
            aria-expanded={menuOpen}>
            <PanelLeftOpen size={18} aria-hidden="true" />
          </button>
          <a className="brand" href="#/"><Logo /><span>Rehearsal</span></a>
        </header>

        {status?.static_demo && (
          <p className="banner" role="note">
            <Info size={17} aria-hidden="true" />
            <span>
              Browser demo of real engine runs, replayed here. <a href="/docs">Run it yourself</a> for the live
              agent.
            </span>
          </p>
        )}
        {status && !canSend && (
          <p className="banner" role="note">
            <CircleAlert size={17} aria-hidden="true" />
            Read only. Open the link printed in the terminal when Rehearsal started to talk to the agent.
          </p>
        )}
        {error && (
          <p className="banner" role="alert">
            <CircleAlert size={17} aria-hidden="true" />
            {error}
          </p>
        )}

        <main id="content" className="content" tabIndex={-1}>
          {route.view === "audit" ? (
            <div className="page">
              <AuditView publicDemo={Boolean(status?.public_demo)} onReset={() => { refresh(); navigate("#/"); }} />
            </div>
          ) : route.view === "chat" ? (
            <div className="chat-view">
              <Conversation key={route.id} id={route.id} status={status} onChanged={refresh} />
              <div className="composer-dock">
                <Composer status={status} ticketIds={tickets.map((t) => t.id)} canSend={canSend} busy={starting}
                  placeholder="Ask about another ticket, or anything about the store"
                  onSubmit={(v) => start({ ticket_id: v.ticketId ?? undefined, message: v.message, mode: v.mode })} />
              </div>
            </div>
          ) : (
            <NewChat status={status} tickets={tickets} canSend={canSend} busy={starting} onStart={start} />
          )}
        </main>
      </div>
    </div>
  );
}
