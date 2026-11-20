import {
  Ban,
  Check,
  ChevronDown,
  CircleAlert,
  Eye,
  Loader2,
  PenLine,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Undo2,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Change, Decision, RehearsalDetail, Status, TicketDetail } from "../api";
import { api, errorMessage, hasToken } from "../api";
import { BlastRadius } from "./BlastRadius";
import { when } from "./labels";
import { Logo } from "./Logo";
import { ActionItem, ReplyPreview, SaidDid } from "./Why";
import { groupSentence, outcomeText, stepSentence, VERDICT } from "./words";

type StepKind = "read" | "write" | "blocked" | "error";

interface Step {
  key: number;
  kind: StepKind;
  tool: string;
  args: Record<string, unknown>;
  pending: boolean;
  note?: string;
  flagged?: boolean;
}

function Expand({ open, id, children }: { open: boolean; id: string; children: React.ReactNode }) {
  return (
    <div className="expand" data-open={open} id={id}>
      <div inert={!open}>{children}</div>
    </div>
  );
}

function groupSteps(steps: Step[]): Step[][] {
  const groups: Step[][] = [];
  for (const step of steps) {
    const last = groups.at(-1);
    const head = last?.[0];
    if (last && head && !step.pending && !head.pending && head.tool === step.tool && head.kind === step.kind) {
      last.push(step);
    } else {
      groups.push([step]);
    }
  }
  return groups;
}

function groupChanges(changes: Change[]): [string, Change[]][] {
  const groups = new Map<string, Change[]>();
  for (const c of changes) groups.set(c.table, [...(groups.get(c.table) ?? []), c]);
  return [...groups.entries()];
}

function TicketCard({ ticket }: { ticket: TicketDetail }) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const first = ticket.messages.find((m) => m.author === "customer");
  const long = (first?.body.length ?? 0) > 220;
  return (
    <div className="ticket-card">
      <p className="ticket-card-head">
        <span className="mono">Ticket {ticket.id}</span>
        <span>{ticket.customer.name}</span>
        {first && <time dateTime={first.sent_at}>{when(first.sent_at)}</time>}
      </p>
      <p className="ticket-card-subject">{ticket.subject}</p>
      {first && (
        <>
          <p className={`ticket-card-body${long && !open ? " clamped" : ""}`} id={bodyId}>
            {first.body}
          </p>
          {long && (
            <button type="button" className="text-button" aria-expanded={open} aria-controls={bodyId}
              onClick={() => setOpen((o) => !o)}>
              {open ? "Show less" : "Read the whole message"}
            </button>
          )}
        </>
      )}
    </div>
  );
}

interface Props {
  id: string;
  status: Status | null;
  onChanged: () => void;
}

export function Conversation({ id, status, onChanged }: Props) {
  const [detail, setDetail] = useState<RehearsalDetail | null>(null);
  const [ticket, setTicket] = useState<TicketDetail | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [agentText, setAgentText] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Decision | null>(null);
  const [showSteps, setShowSteps] = useState(true);
  const [showChanges, setShowChanges] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [reviewer, setReviewer] = useState(() => {
    try {
      return localStorage.getItem("rehearsal-reviewer") ?? "";
    } catch {
      return "";
    }
  });
  const endRef = useRef<HTMLDivElement>(null);
  const outcomeRef = useRef<HTMLDivElement>(null);
  const ids = { steps: useId(), changes: useId(), ack: useId(), name: useId() };
  const writes = useMemo(() => new Set(status?.tools.filter((t) => t.writes).map((t) => t.name)), [status]);
  const writesRef = useRef(writes);
  writesRef.current = writes;
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  useEffect(() => {
    setDetail(null);
    setSteps([]);
    setAgentText(null);
    setStreamError(null);
    setError(null);
    setAcknowledged(false);
    setShowChanges(false);
    setShowSteps(true);
    let alive = true;
    const refresh = () =>
      api
        .rehearsal(id)
        .then((d) => {
          if (!alive) return;
          setDetail(d);
          if (!d.running) setShowSteps(false);
        })
        .catch((e) => alive && setError(errorMessage(e)));
    refresh();

    const stop = api.subscribe(id, (kind, d) => {
      if (kind === "tool_call") {
        const tool = String(d.tool);
        const args = typeof d.args === "object" && d.args !== null ? (d.args as Record<string, unknown>) : {};
        setSteps((s) => [...s, { key: s.length, kind: writesRef.current.has(tool) ? "write" : "read", tool, args,
          pending: true }]);
      } else if (kind === "tool_result") {
        const tool = String(d.tool);
        const ok = Boolean(d.ok);
        const text = String(d.text ?? "");
        setSteps((s) => {
          const index = s.findLastIndex((step) => step.pending && step.tool === tool);
          if (index < 0) return s;
          const step = s[index]!;
          const blocked = !ok && text.startsWith("Blocked");
          const next = s.slice();
          next[index] = { ...step, pending: false, kind: blocked ? "blocked" : ok ? step.kind : "error",
            note: ok ? undefined : text.replace(/^Blocked( before reaching the app)?:\s*/, ""), flagged: Boolean(d.warning) };
          return next;
        });
      } else if (kind === "agent_finished") {
        setAgentText(String(d.summary ?? ""));
      } else if (kind === "error") {
        setStreamError(String(d.message ?? "Something went wrong."));
      } else if (kind === "review") {
        refresh();
      } else if (kind === "done") {
        stop();
        refresh();
        onChangedRef.current();
      }
    });
    return () => {
      alive = false;
      stop();
    };
  }, [id]);

  const ticketId = detail?.ticket_id ?? null;
  useEffect(() => {
    setTicket(null);
    if (ticketId === null) return;
    let alive = true;
    api.ticket(ticketId).then((t) => alive && setTicket(t)).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [ticketId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [steps.length, agentText, detail?.state]);

  const canDecide = hasToken() || Boolean(status?.public_demo);
  const running = detail?.running ?? true;
  const review = detail?.review ?? null;
  const owners = new Set(review?.changes.map((c) => JSON.stringify(c.owner)).filter((o) => o !== "null"));
  const actions = detail?.actions ?? [];
  const needsAck = review?.risk === "high" && actions.length > 0;
  const target = status?.target ?? "the app";

  async function decide(decision: Decision) {
    if (!detail) return;
    setBusy(decision);
    setError(null);
    const name = reviewer.trim() || "Reviewer";
    try {
      localStorage.setItem("rehearsal-reviewer", name);
    } catch {
      /* optional convenience only */
    }
    try {
      setDetail(await api.decide(detail.id, decision, name));
      onChanged();
      requestAnimationFrame(() => {
        outcomeRef.current?.focus({ preventScroll: true });
        endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
      });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const Verdict = review ? { high: ShieldAlert, medium: ShieldQuestion, low: ShieldCheck }[review.risk] : null;
  const verdictText = review ? VERDICT[review.risk] : null;
  const outcome = detail?.outcome
    ? outcomeText(detail.outcome.state, actions.length, Boolean(detail.outcome.auto))
    : null;
  const shownActions = actions.length > 6 && !showChanges ? actions.slice(0, 5) : actions;

  return (
    <div className="thread">
      {detail && (
        <div className="msg user">
          <div className="bubble">
            <p>{detail.request}</p>
          </div>
          {ticket && <TicketCard ticket={ticket} />}
        </div>
      )}

      <div className="msg agent">
        <span className="avatar" aria-hidden="true">
          <Logo />
        </span>
        <div className="agent-body">
          <p className="agent-name">
            {detail?.mode === "external" ? "Connected agent" : "Rehearsal agent"} <span className="agent-model">{detail?.model ?? ""}</span>
          </p>

          <div className="activity">
            <button type="button" className="activity-head" aria-expanded={showSteps} aria-controls={ids.steps}
              onClick={() => setShowSteps((v) => !v)}>
              {running ? <Loader2 size={16} className="spin" aria-hidden="true" /> : <ShieldCheck size={16} aria-hidden="true" />}
              <span>
                {running
                  ? `Working on a private copy of ${target}`
                  : `Worked on a private copy of ${target}, ${steps.length} ${steps.length === 1 ? "step" : "steps"}`}
              </span>
              <ChevronDown size={16} className="chev" aria-hidden="true" />
            </button>
            <Expand open={showSteps} id={ids.steps}>
              <ol className="steps" aria-live={running ? "polite" : undefined} aria-label="What the agent did">
                {groupSteps(steps).map((group) => {
                  const step = group[0]!;
                  const Icon = { read: Eye, write: PenLine, blocked: Ban, error: CircleAlert }[step.kind];
                  return (
                    <li key={step.key} className="step" data-kind={step.kind}>
                      <span className="step-icon" aria-hidden="true">
                        {step.pending ? <Loader2 size={14} className="spin" /> : <Icon size={14} />}
                      </span>
                      <span className="step-text">
                        {group.length > 1
                          ? groupSentence(step.tool, group.length)
                          : stepSentence(step.tool, step.args)}
                        {step.kind === "write" && <span className="tag">on the copy</span>}
                        {step.kind === "blocked" && <span className="tag risk">blocked</span>}
                        {group.some((s) => s.flagged) && <span className="tag risk">looks like an injection</span>}
                        {step.note && <span className="step-note">{step.note}</span>}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </Expand>
            {running && steps.every((s) => !s.pending) && (
              <p className="typing" role="status" aria-label="The agent is thinking">
                <i /><i /><i />
              </p>
            )}
          </div>

          {agentText && !running && (
            <div className="agent-text md">
              <Markdown remarkPlugins={[remarkGfm]} skipHtml
                components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" /> }}>
                {agentText}
              </Markdown>
            </div>
          )}
          {streamError && <p className="agent-text warn">{streamError}</p>}

          {review && Verdict && verdictText && (
            <div className="proposal">
              {actions.length === 0 ? (
                <p className="agent-text">I did not need to change anything on the live store, so there is nothing to approve.</p>
              ) : (
                <>
                  <div className="verdict" data-risk={review.risk}>
                    <Verdict size={20} aria-hidden="true" />
                    <div>
                      <p className="verdict-title">{verdictText.title}</p>
                      <p>{verdictText.body}</p>
                    </div>
                  </div>

                  <p className="agent-text">Here is what I would like to do on the live store:</p>
                  <ul className="action-list">
                    {shownActions.map((a, i) => (
                      <ActionItem key={`${a}-${i}`} sentence={a} why={review.explain?.[i]} review={review}
                        calls={detail?.calls ?? []} />
                    ))}
                    {shownActions.length < actions.length && (
                      <li className="more">and {actions.length - shownActions.length} more</li>
                    )}
                  </ul>

                  <ReplyPreview review={review} />

                  <dl className="facts-row">
                    <div><dt>Money out</dt><dd className="num">{review.money_out} USD</dd></div>
                    <div><dt>Customers affected</dt><dd className="num">{owners.size}</dd></div>
                    <div><dt>Rows that change</dt><dd className="num">{review.changes.length}</dd></div>
                  </dl>

                  <SaidDid review={review} />

                  <BlastRadius review={review} homeId={ticket?.customer.id ?? null}
                    homeLabel={ticket ? ticket.customer.name.split(" ")[0]! : "Request"} />

                  {review.flags.length > 0 && (
                    <div className="warnings">
                      <p className="warnings-title">Why I am flagging this</p>
                      <ul>
                        {review.flags.map((f) => (
                          <li key={f.title} data-level={f.level}>
                            <strong>{f.title}.</strong> {f.detail}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <Expand open={showChanges} id={ids.changes}>
                    <div className="changes">
                      {groupChanges(review.changes).map(([table, rows]) => (
                        <div key={table} className="change-group">
                          <p className="change-table">{table} <span className="num">({rows.length})</span></p>
                          <ul>
                            {rows.map((c) => (
                              <li key={`${c.table}-${JSON.stringify(c.key)}`}>
                                <span className="kind" data-kind={c.kind}>{c.kind}</span>
                                <span>
                                  {c.summary}
                                  {c.caused_by && c.caused_by.length > 0 && (
                                    <span className="step-ref"> by step {c.caused_by.join(", ")}</span>
                                  )}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      ))}
                    </div>
                  </Expand>

                  {detail?.state === "ready" && (
                    <div className="decide">
                      <p className="agent-text question">Want me to go ahead?</p>
                      {needsAck && (
                        <label className="check" htmlFor={ids.ack}>
                          <input id={ids.ack} type="checkbox" checked={acknowledged}
                            onChange={(e) => setAcknowledged(e.target.checked)} />
                          <span>I have read the warnings and I still want this.</span>
                        </label>
                      )}
                      {error && <p className="error" role="alert">{error}</p>}
                      <div className="choices">
                        <button type="button" className="choice primary"
                          disabled={!canDecide || busy !== null || (needsAck && !acknowledged)}
                          onClick={() => decide("approve")}>
                          {busy === "approve" ? <Loader2 size={16} className="spin" aria-hidden="true" /> : <Check size={16} aria-hidden="true" />}
                          {busy === "approve" ? "Applying" : "Yes, apply it"}
                        </button>
                        <button type="button" className="choice" disabled={!canDecide || busy !== null}
                          onClick={() => decide("reject")}>
                          <X size={16} aria-hidden="true" /> No, cancel
                        </button>
                        <button type="button" className="choice quiet" aria-expanded={showChanges}
                          aria-controls={ids.changes} onClick={() => setShowChanges((v) => !v)}>
                          {showChanges ? "Hide the changes" : "Show me every change"}
                        </button>
                      </div>
                      <label className="signed" htmlFor={ids.name}>
                        <span>Signed in the audit log as</span>
                        <input id={ids.name} value={reviewer} maxLength={60} placeholder="Reviewer"
                          autoComplete="name" onChange={(e) => setReviewer(e.target.value)} />
                      </label>
                      {!canDecide && (
                        <p className="hint">Read only. Open the link printed when Rehearsal started to decide.</p>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {outcome && detail?.outcome && (
            <div className="outcome" data-state={detail.outcome.state} ref={outcomeRef} tabIndex={-1} role="status">
              {detail.outcome.state === "applied" ? (
                <Check size={18} aria-hidden="true" />
              ) : detail.outcome.state === "undone" ? (
                <RotateCcw size={18} aria-hidden="true" />
              ) : (
                <CircleAlert size={18} aria-hidden="true" />
              )}
              <div>
                <p className="outcome-title">{outcome.title}</p>
                <p>{outcome.body}</p>
                {detail.outcome.state === "applied" && (
                  <p className="verified">Verified against the rehearsal. The audit log has the details.</p>
                )}
                {detail.outcome.state === "applied" && canDecide && (
                  <button type="button" className="text-button undo" disabled={busy !== null}
                    onClick={() => decide("undo")}>
                    <Undo2 size={15} aria-hidden="true" /> Changed your mind? Undo it
                  </button>
                )}
                {error && <p className="error" role="alert">{error}</p>}
              </div>
            </div>
          )}
          {!detail && error && <p className="error" role="alert">{error}</p>}
        </div>
      </div>
      <div ref={endRef} className="thread-end" />
    </div>
  );
}
