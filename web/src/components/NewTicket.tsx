import { Loader2, PenLine } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import type { CustomerRow, OrderRow, TicketDetail } from "../api";
import { api, errorMessage } from "../api";

interface Props {
  target: string;
  staticDemo: boolean;
  liveAgent: boolean;
  disabled: boolean;
  /** Reviews still running or waiting, which a new ticket sends back to be rehearsed again. */
  waiting: number;
  /** Applied work, which can no longer be undone automatically once live data moves on. */
  applied: number;
  onOpened: (ticket: TicketDetail) => void;
}

/** What opening a ticket does to work already in the console, in one sentence, or null when it does nothing. */
function sideEffects(waiting: number, applied: number): string | null {
  const parts = [];
  if (waiting) parts.push(`${waiting === 1 ? "the review" : `the ${waiting} reviews`} still running or waiting will ` +
    "have to be rehearsed again");
  if (applied) parts.push("changes already applied can no longer be undone automatically");
  return parts.length ? `Opening a ticket changes live data, so ${parts.join(", and ")}.` : null;
}

/** Write a ticket as the customer, then let the agent work on it like any other. */
export function NewTicket({ target, staticDemo, liveAgent, disabled, waiting, applied, onOpened }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [customers, setCustomers] = useState<CustomerRow[] | null>(null);
  const [orders, setOrders] = useState<OrderRow[] | null>(null);
  const [customer, setCustomer] = useState("");
  const [order, setOrder] = useState("");
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const ids = { title: useId(), customer: useId(), order: useId(), subject: useId(), message: useId(), note: useId() };
  const usable = !staticDemo && liveAgent;

  function show() {
    setError(null);
    dialog.current?.showModal();
    if (usable && customers === null) {
      api.customers()
        .then((list) => {
          setCustomers(list);
          setCustomer((current) => current || String(list[0]?.id ?? ""));
        })
        .catch((e) => setError(errorMessage(e)));
    }
  }

  useEffect(() => {
    if (!customer) return;
    let current = true;
    setOrders(null);
    setOrder("");
    api.customerOrders(Number(customer))
      .then((list) => current && setOrders(list))
      .catch((e) => current && setError(errorMessage(e)));
    return () => {
      current = false;
    };
  }, [customer]);

  const valid = customer !== "" && subject.trim().length >= 3 && message.trim().length >= 2;

  async function submit() {
    if (!valid || sending) return;
    setSending(true);
    setError(null);
    try {
      const ticket = await api.openTicket({ customer_id: Number(customer), order_id: order ? Number(order) : null,
        subject: subject.trim(), message: message.trim() });
      dialog.current?.close();
      setSubject("");
      setMessage("");
      setOrder("");
      onOpened(ticket);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSending(false);
    }
  }

  const note = sideEffects(waiting, applied);
  const close = () => dialog.current?.close();

  return (
    <>
      <button type="button" className="starter wide" disabled={disabled} onClick={show}>
        <span className="starter-icon"><PenLine size={18} aria-hidden="true" /></span>
        <span className="starter-title">Write your own ticket</span>
        <span className="starter-text">Play the customer. Hide an instruction in your message if you like, then watch what
          the agent does and what Rehearsal catches.</span>
      </button>

      <dialog ref={dialog} className="ticket-dialog" aria-labelledby={ids.title}>
        <h2 id={ids.title}>Write your own ticket</h2>
        {!usable ? (
          <>
            <p className="hint">
              {staticDemo
                ? "This browser demo replays recorded runs, so there is no agent here to work on a ticket you write. " +
                  "Run Rehearsal on your own machine, with a free model, to try it."
                : "No model key is set, so no agent can work on a new ticket. Add a free key to .env, as the docs " +
                  "describe, and start Rehearsal again."}
            </p>
            <div className="decision-buttons spaced-top-lg">
              <button type="button" className="button" onClick={close}>Close</button>
              <a className="button primary" href="/docs#quickstart">How to run it</a>
            </div>
          </>
        ) : (
          <form className="ticket-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
            <p className="hint">
              You write as the customer. The ticket opens in {target} through its own contact form, then the agent
              works on it on a private copy, like any other ticket.
            </p>

            <label htmlFor={ids.customer}>Customer</label>
            <select id={ids.customer} value={customer} disabled={customers === null}
              onChange={(e) => setCustomer(e.target.value)}>
              {customers === null
                ? <option value="">Loading customers</option>
                : customers.map((c) => <option key={c.id} value={c.id}>{c.name}, {c.tier}</option>)}
            </select>

            <label htmlFor={ids.order}>Order it is about</label>
            <select id={ids.order} value={order} disabled={orders === null} onChange={(e) => setOrder(e.target.value)}>
              <option value="">{orders === null ? "Loading orders" : "No particular order"}</option>
              {orders?.map((o) => (
                <option key={o.id} value={o.id}>
                  Order {o.id}: {o.items.map((i) => i.name).join(", ")}, {o.total} USD, {o.status.replace("_", " ")}
                </option>
              ))}
            </select>

            <label htmlFor={ids.subject}>Subject</label>
            <input id={ids.subject} value={subject} required minLength={3} maxLength={120}
              placeholder="My jacket arrived torn" onChange={(e) => setSubject(e.target.value)} />

            <label htmlFor={ids.message}>Message</label>
            <textarea id={ids.message} value={message} required minLength={2} maxLength={2000} rows={5}
              aria-describedby={note ? ids.note : undefined}
              placeholder="What the customer says. To test Rehearsal, hide an instruction for the agent in it."
              onChange={(e) => setMessage(e.target.value)} />

            {note && <p id={ids.note} className="hint">{note}</p>}
            {error && <p className="error" role="alert">{error}</p>}

            <div className="decision-buttons">
              <button type="button" className="button" onClick={close}>Cancel</button>
              <button type="submit" className="button primary" disabled={!valid || sending}>
                {sending && <Loader2 size={17} className="spin" aria-hidden="true" />}
                Open the ticket and start the agent
              </button>
            </div>
          </form>
        )}
      </dialog>
    </>
  );
}
