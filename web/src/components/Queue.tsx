import type { TicketRow } from "../api";
import { STATE_LABEL, rehearsalTone } from "./labels";

interface Props {
  tickets: TicketRow[] | null;
  selected: number | null;
  onSelect: (id: number) => void;
}

function Row({ t, selected, onSelect }: { t: TicketRow; selected: boolean; onSelect: (id: number) => void }) {
  return (
    <li>
      <button type="button" className="ticket-link" aria-current={selected} onClick={() => onSelect(t.id)}>
        <span className="ticket-top">
          <span className="priority-dot" data-priority={t.priority} aria-hidden="true" />
          <span className="mono num">#{t.id}</span>
          <span className="visually-hidden">priority {t.priority},</span>
          <span>{t.status}</span>
        </span>
        <span className="ticket-subject">{t.subject}</span>
        <span className="ticket-meta">
          <span>{t.customer}</span>
          {t.rehearsal && (
            <span className="pill" data-tone={rehearsalTone(t.rehearsal)}>
              {STATE_LABEL[t.rehearsal.state] ?? t.rehearsal.state}
            </span>
          )}
        </span>
      </button>
    </li>
  );
}

export function Queue({ tickets, selected, onSelect }: Props) {
  if (tickets === null) {
    return <p className="hint" role="status">Loading tickets</p>;
  }
  const active = tickets.filter((t) => t.status !== "closed");
  const closed = tickets.filter((t) => t.status === "closed");
  return (
    <>
      <div className="queue-head">
        <h2 id="queue-title">Support queue</h2>
        <span className="hint num">{active.length} open</span>
      </div>
      <ul aria-labelledby="queue-title">
        {active.map((t) => (
          <Row key={t.id} t={t} selected={t.id === selected} onSelect={onSelect} />
        ))}
      </ul>
      {closed.length > 0 && (
        <details className="closed-toggle">
          <summary className="hint">Recently closed ({closed.length})</summary>
          <ul>
            {closed.map((t) => (
              <Row key={t.id} t={t} selected={t.id === selected} onSelect={onSelect} />
            ))}
          </ul>
        </details>
      )}
    </>
  );
}
