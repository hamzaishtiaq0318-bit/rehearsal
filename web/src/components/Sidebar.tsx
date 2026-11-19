import { Moon, PanelLeftClose, SquarePen, ScrollText, Sun } from "lucide-react";

import type { ConversationRow, Status } from "../api";
import { Logo } from "./Logo";
import { dayLabel } from "./words";

interface Props {
  status: Status | null;
  conversations: ConversationRow[];
  selected: string | null;
  view: "chat" | "audit";
  theme: "light" | "dark";
  open: boolean;
  onClose: () => void;
  onToggleTheme: () => void;
}

const STATE_TEXT: Record<string, string> = {
  rehearsing: "Working",
  ready: "Needs your review",
  applied: "Applied",
  rejected: "Cancelled",
  stale: "Held off",
  failed: "Rolled back",
  undone: "Undone",
};

function answered(c: ConversationRow): boolean {
  return c.state === "ready" && !c.running && c.changes === 0;
}

function dotTone(c: ConversationRow): string {
  if (c.running || c.state === "rehearsing") return "working";
  if (answered(c)) return "quiet";
  if (c.state === "ready") return c.risk === "high" ? "high" : "review";
  if (c.state === "applied") return "good";
  return "quiet";
}

export function Sidebar({ status, conversations, selected, view, theme, open, onClose, onToggleTheme }: Props) {
  const groups = new Map<string, ConversationRow[]>();
  for (const c of conversations) {
    const label = dayLabel(c.created_at);
    groups.set(label, [...(groups.get(label) ?? []), c]);
  }
  const ready = status?.models.filter((m) => m.state === "ready").length ?? 0;

  return (
    <aside className="sidebar" data-open={open} aria-label="Conversations">
      <div className="sidebar-top">
        <a className="brand" href="#/" onClick={onClose}>
          <Logo />
          <span>Rehearsal</span>
        </a>
        <button type="button" className="icon-button only-mobile" onClick={onClose} aria-label="Close the menu">
          <PanelLeftClose size={18} aria-hidden="true" />
        </button>
      </div>

      <a className="new-chat" href="#/" onClick={onClose}>
        <SquarePen size={17} aria-hidden="true" />
        New conversation
      </a>

      <nav className="history" aria-label="Previous conversations">
        {conversations.length === 0 && (
          <p className="history-empty">Your conversations with the agent will show up here.</p>
        )}
        {[...groups.entries()].map(([label, items]) => (
          <div key={label} className="history-group">
            <p className="history-label">{label}</p>
            <ul>
              {items.map((c) => (
                <li key={c.id}>
                  <a href={`#/c/${c.id}`} className="history-item" aria-current={c.id === selected ? "page" : undefined}
                    onClick={onClose}>
                    <span className="dot" data-tone={dotTone(c)} aria-hidden="true" />
                    <span className="history-text">
                      <span className="history-title">{c.title}</span>
                      <span className="history-state">{answered(c) ? "Answered" : STATE_TEXT[c.state] ?? c.state}</span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <a className="foot-link" href="#/audit" aria-current={view === "audit" ? "page" : undefined} onClick={onClose}>
          <ScrollText size={17} aria-hidden="true" />
          Audit log
        </a>
        <div className="foot-row">
          {status && (
            <span className="model-status" title="Free AI models ready right now">
              <span className="dot" data-tone={ready > 0 ? "good" : "review"} aria-hidden="true" />
              {ready} free models ready
            </span>
          )}
          <button type="button" className="icon-button" onClick={onToggleTheme}
            aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
            {theme === "dark" ? <Sun size={17} aria-hidden="true" /> : <Moon size={17} aria-hidden="true" />}
          </button>
        </div>
      </div>
    </aside>
  );
}
