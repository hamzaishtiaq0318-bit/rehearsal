import { CircleAlert, Inbox, Moon, ScrollText, Sun } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import type { Status, TicketRow } from "./api";
import { api, errorMessage, hasToken } from "./api";
import { AuditView } from "./components/AuditView";
import { Logo } from "./components/Logo";
import { Queue } from "./components/Queue";
import { TicketView } from "./components/TicketView";

type Route = { view: "console"; ticket: number | null } | { view: "audit" };

function parseRoute(hash: string): Route {
  if (hash.startsWith("#/audit")) return { view: "audit" };
  const match = /^#\/ticket\/(\d{1,8})$/.exec(hash);
  return { view: "console", ticket: match ? Number(match[1]) : null };
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

function Welcome({ target }: { target: string }) {
  return (
    <div className="main-inner">
      <section className="empty card" aria-labelledby="welcome-title">
        <p className="eyebrow">Rehearsal console</p>
        <h1 id="welcome-title">Pick a ticket. Watch the agent rehearse. Decide what goes live.</h1>
        <p>
          Rehearsal sits between an AI agent and {target}. Every tool call runs on a shadow copy first, so you see the
          exact rows that would change before anything real does.
        </p>
        <ol className="how">
          <li>
            <strong>1. Rehearse</strong>
            The agent works through Synapse generated tools on a private copy of the app.
          </li>
          <li>
            <strong>2. Review</strong>
            See every changed row, the money involved and plain risk flags.
          </li>
          <li>
            <strong>3. Decide</strong>
            Approve to replay the exact calls on live data, or reject and nothing happens.
          </li>
          <li>
            <strong>4. Verify</strong>
            Rehearsal checks live changed exactly as rehearsed, and can undo it.
          </li>
        </ol>
      </section>
    </div>
  );
}

export function App() {
  const [route, navigate] = useRoute();
  const [status, setStatus] = useState<Status | null>(null);
  const [tickets, setTickets] = useState<TicketRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const refresh = useCallback(() => {
    Promise.all([api.status(), api.tickets()])
      .then(([s, t]) => {
        setStatus(s);
        setTickets(t);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const title = route.view === "audit" ? "Audit log" : route.ticket ? `Ticket ${route.ticket}` : "Support queue";
    document.title = `${title} | Rehearsal`;
  }, [route]);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem("rehearsal-theme", next);
    } catch {
      /* the choice then lasts for this page view only */
    }
  }

  const selected = route.view === "console" ? route.ticket : null;
  const row = tickets?.find((t) => t.id === selected) ?? null;
  const readyModels = status?.models.filter((m) => m.state === "ready").length ?? 0;

  return (
    <>
      <a className="skip-link" href="#content">
        Skip to content
      </a>
      <header className="topbar">
        <a className="brand" href="#/" aria-label="Rehearsal, support queue">
          <Logo />
          <span>Rehearsal</span>
        </a>
        {status && <span className="context">Protecting {status.target}</span>}
        <nav aria-label="Main">
          <a className="navlink" href="#/" aria-current={route.view === "console" ? "page" : undefined}>
            <Inbox size={17} aria-hidden="true" />
            <span>Queue</span>
          </a>
          <a className="navlink" href="#/audit" aria-current={route.view === "audit" ? "page" : undefined}>
            <ScrollText size={17} aria-hidden="true" />
            <span>Audit log</span>
          </a>
        </nav>
        {status && (
          <span className="pill" data-tone={readyModels > 0 ? "good" : "medium"} title="Free models ready to use"
            aria-label={`${readyModels} free models ready`}>
            <span className="num">{readyModels}</span>
            <span className="pill-label">models ready</span>
          </span>
        )}
        <button type="button" className="icon-button" onClick={toggleTheme}
          aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
          {theme === "dark" ? <Sun size={18} aria-hidden="true" /> : <Moon size={18} aria-hidden="true" />}
        </button>
      </header>

      {status && !hasToken() && !status.public_demo && (
        <p className="banner" role="note">
          <CircleAlert size={18} aria-hidden="true" />
          Read only view. Open the link printed in the terminal when Rehearsal started to run and approve rehearsals.
        </p>
      )}
      {error && (
        <p className="banner" role="alert">
          <CircleAlert size={18} aria-hidden="true" />
          {error}
        </p>
      )}

      {route.view === "audit" ? (
        <main id="content" className="main" tabIndex={-1}>
          <AuditView publicDemo={Boolean(status?.public_demo)} onReset={refresh} />
        </main>
      ) : (
        <div className="layout" data-has-selection={selected !== null}>
          <aside className="queue" aria-label="Support queue">
            <Queue tickets={tickets} selected={selected} onSelect={(id) => navigate(`#/ticket/${id}`)} />
          </aside>
          <main id="content" className="main" tabIndex={-1}>
            {selected === null ? (
              <Welcome target={status?.target ?? "your app"} />
            ) : (
              <TicketView
                ticketId={selected}
                status={status}
                rehearsalId={row?.rehearsal?.id ?? null}
                running={row?.rehearsal?.state === "rehearsing"}
                onBack={() => navigate("#/")}
                onChanged={refresh}
              />
            )}
          </main>
        </div>
      )}
    </>
  );
}
