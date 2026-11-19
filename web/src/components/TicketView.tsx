import { ArrowLeft, Bot, History, Loader2, Siren } from "lucide-react";
import { useEffect, useState } from "react";

import type { RehearsalDetail, Status, TicketDetail } from "../api";
import { api, errorMessage, hasToken } from "../api";
import { when } from "./labels";
import { RehearsalPanel } from "./RehearsalPanel";

interface Props {
  ticketId: number;
  status: Status | null;
  rehearsalId: string | null;
  running: boolean;
  onBack: () => void;
  onChanged: () => void;
}

export function TicketView({ ticketId, status, rehearsalId, running, onBack, onChanged }: Props) {
  const [ticket, setTicket] = useState<TicketDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<string | null>(rehearsalId);
  const [starting, setStarting] = useState<RehearsalDetail["mode"] | null>(null);

  useEffect(() => setCurrent(rehearsalId), [rehearsalId, ticketId]);

  useEffect(() => {
    let alive = true;
    setError(null);
    api
      .ticket(ticketId)
      .then((t) => alive && setTicket(t))
      .catch((e) => alive && setError(errorMessage(e)));
    return () => {
      alive = false;
    };
  }, [ticketId, current, running]);

  async function start(mode: RehearsalDetail["mode"]) {
    setStarting(mode);
    setError(null);
    try {
      const { id } = await api.start(ticketId, mode);
      setCurrent(id);
      onChanged();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setStarting(null);
    }
  }

  const canStart = (hasToken() || Boolean(status?.public_demo)) && !running && starting === null;
  const hasRecording = status?.recordings.includes(ticketId) ?? false;
  const isSimulationTicket = status?.simulation_ticket === ticketId;
  const shown = ticket?.id === ticketId ? ticket : null;

  return (
    <div className="main-inner">
      <div>
        <button type="button" className="button quiet" onClick={onBack}>
          <ArrowLeft size={17} aria-hidden="true" /> All tickets
        </button>
      </div>

      <article className="card" aria-labelledby="ticket-title">
        {shown ? (
          <>
            <p className="eyebrow mono">Ticket {shown.id}</p>
            <h1 id="ticket-title" className="ticket-title">
              {shown.subject}
            </h1>
            <p className="facts">
              <span>
                {shown.customer.name}, {shown.customer.tier} customer
              </span>
              {shown.order_id && <span className="mono">Order {shown.order_id}</span>}
              <span>Priority {shown.priority}</span>
              <span>Status {shown.status}</span>
            </p>
            <div className="thread">
              {shown.messages.map((m) => (
                <div key={m.id} className="message">
                  <header>
                    <span>{m.author === "customer" ? shown.customer.name : "Kestrel support"}</span>
                    <time dateTime={m.sent_at}>{when(m.sent_at)}</time>
                  </header>
                  <p className="message-body">{m.body}</p>
                </div>
              ))}
            </div>
          </>
        ) : (
          <p className="hint" role="status">
            {error ?? "Loading ticket"}
          </p>
        )}
      </article>

      <section className="card" aria-labelledby="rehearse-title">
        <h2 id="rehearse-title">Rehearse this ticket</h2>
        <p className="hint spaced-top">
          The agent works on a shadow copy of {status?.target ?? "the app"}. Nothing reaches live data until a person
          approves the exact changes.
        </p>
        <div className="actions">
          <button type="button" className="button primary" disabled={!canStart} onClick={() => start("agent")}>
            {starting === "agent" ? (
              <Loader2 size={17} className="spin" aria-hidden="true" />
            ) : (
              <Bot size={17} aria-hidden="true" />
            )}
            {starting === "agent" ? "Starting" : "Rehearse with the AI agent"}
          </button>
          {hasRecording && (
            <button type="button" className="button" disabled={!canStart} onClick={() => start("replay")}>
              <History size={17} aria-hidden="true" /> Replay a recorded run
            </button>
          )}
          {isSimulationTicket && (
            <button type="button" className="button danger" disabled={!canStart} onClick={() => start("simulated")}>
              <Siren size={17} aria-hidden="true" /> Simulate a compromised agent
            </button>
          )}
        </div>
        {isSimulationTicket && (
          <p className="hint spaced-top">
            This ticket hides an instruction aimed at AI agents. The simulation is a scripted stand in that obeys it,
            so you can see what Rehearsal catches. Everything Rehearsal does in response is real.
          </p>
        )}
        {running && <p className="hint spaced-top">A rehearsal for this ticket is already running.</p>}
        {!hasToken() && !status?.public_demo && (
          <p className="hint spaced-top">
            Read only. Open the console with the link printed when Rehearsal started to run rehearsals.
          </p>
        )}
        {error && shown && (
          <p className="error spaced-top" role="alert">
            {error}
          </p>
        )}
      </section>

      {current && <RehearsalPanel key={current} id={current} status={status} onChanged={onChanged} />}
    </div>
  );
}
