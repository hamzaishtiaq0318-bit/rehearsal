import {
  Ban,
  CircleAlert,
  CircleCheck,
  Eye,
  Loader2,
  MessageSquare,
  PenLine,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Undo2,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { Change, Decision, RehearsalDetail, Status } from "../api";
import { api, errorMessage, hasToken } from "../api";

type StepKind = "read" | "write" | "blocked" | "message" | "error";

interface Step {
  key: number;
  kind: StepKind;
  title: string;
  detail?: string;
  args?: string;
  pending?: boolean;
  guard?: string;
}

const VERDICT = {
  high: { icon: ShieldAlert, title: "High risk. Look closely before approving.",
    body: "Rehearsal found changes that a careful person would question. Approve only if every flag below is expected." },
  medium: { icon: ShieldQuestion, title: "Review the changes before approving.",
    body: "Nothing alarming, but money or customer records would change." },
  low: { icon: ShieldCheck, title: "Low risk.",
    body: "No rule flagged anything unusual about these changes." },
} as const;

function shortArgs(args: unknown): string {
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args);
  } catch {
    return "";
  }
}

function preview(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > 160 ? `${clean.slice(0, 157)}...` : clean;
}

const STEP_ICON = { read: Eye, write: PenLine, blocked: Ban, message: MessageSquare, error: CircleAlert };

/** Consecutive calls to the same tool become one row, so twelve lookups read as one line, not twelve. */
function StepList({ steps, live = false }: { steps: Step[]; live?: boolean }) {
  const groups: Step[][] = [];
  for (const step of steps) {
    const last = groups.at(-1);
    const head = last?.[0];
    if (last && head && !step.pending && !head.pending && head.title === step.title && head.kind === step.kind &&
      (step.kind === "read" || step.kind === "write")) {
      last.push(step);
    } else {
      groups.push([step]);
    }
  }
  return (
    <ol className="timeline" aria-live={live ? "polite" : undefined} aria-relevant="additions" aria-label="Agent steps">
      {groups.map((group) => {
        const step = group[0]!;
        const Icon = STEP_ICON[step.kind];
        return (
          <li key={step.key} className="step" data-kind={step.kind}>
            <span className="step-icon" aria-hidden="true">
              {step.pending ? <Loader2 size={15} className="spin" /> : <Icon size={15} />}
            </span>
            <div>
              <p className="step-title">
                <span className={step.kind === "message" ? undefined : "mono"}>{step.title}</span>
                {group.length > 1 && <span className="pill num">x{group.length}</span>}
                {step.kind === "write" && <span className="pill" data-tone="medium">changes data</span>}
                {step.kind === "blocked" && <span className="pill" data-tone="high">blocked</span>}
                {group.some((s) => s.guard) && <span className="pill" data-tone="high">Prompt Guard flagged this</span>}
              </p>
              {group.length === 1 ? (
                <>
                  {step.args && <p className="args">{step.args}</p>}
                  {step.detail && <p className="step-detail">{step.detail}</p>}
                </>
              ) : (
                <details className="step-group">
                  <summary className="hint">Show all {group.length} calls</summary>
                  <ul>
                    {group.map((s) => (
                      <li key={s.key}>
                        <p className="args">{s.args}</p>
                        {s.detail && <p className="step-detail">{s.detail}</p>}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function groupChanges(changes: Change[]): [string, Change[]][] {
  const groups = new Map<string, Change[]>();
  for (const c of changes) groups.set(c.table, [...(groups.get(c.table) ?? []), c]);
  return [...groups.entries()];
}

interface Props {
  id: string;
  status: Status | null;
  onChanged: () => void;
}

export function RehearsalPanel({ id, status, onChanged }: Props) {
  const [detail, setDetail] = useState<RehearsalDetail | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [summary, setSummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Decision | null>(null);
  const [reviewer, setReviewer] = useState(() => {
    try {
      return localStorage.getItem("rehearsal-reviewer") ?? "";
    } catch {
      return "";
    }
  });
  const [note, setNote] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const outcomeRef = useRef<HTMLDivElement>(null);
  const ids = { reviewer: useId(), note: useId(), ack: useId() };
  const writes = useMemo(() => new Set(status?.tools.filter((t) => t.writes).map((t) => t.name)), [status]);
  const writesRef = useRef(writes);
  writesRef.current = writes;
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  useEffect(() => {
    setDetail(null);
    setSteps([]);
    setSummary(null);
    setError(null);
    setAcknowledged(false);
    let alive = true;
    const refresh = () =>
      api.rehearsal(id).then((d) => alive && setDetail(d)).catch((e) => alive && setError(errorMessage(e)));
    refresh();

    const source = new EventSource(api.eventsUrl(id));
    const seen = new Set<string>();
    const on = (kind: string, handler: (data: Record<string, unknown>) => void) =>
      source.addEventListener(kind, (event) => {
        const message = event as MessageEvent<string>;
        if (seen.has(message.lastEventId)) return;
        seen.add(message.lastEventId);
        handler(JSON.parse(message.data) as Record<string, unknown>);
      });

    on("message", (d) => {
      setSteps((s) => [...s, { key: s.length, kind: "message", title: "Agent", detail: preview(String(d.text ?? "")) }]);
    });
    on("tool_call", (d) => {
      const tool = String(d.tool);
      setSteps((s) => [...s, { key: s.length, kind: writesRef.current.has(tool) ? "write" : "read", title: tool,
        args: shortArgs(d.args), pending: true }]);
    });
    on("tool_result", (d) => {
      const tool = String(d.tool);
      const ok = Boolean(d.ok);
      const text = String(d.text ?? "");
      setSteps((s) => {
        const index = s.findLastIndex((step) => step.pending && step.title === tool);
        const blocked = !ok && text.startsWith("Blocked");
        const updated: Step = {
          key: index >= 0 ? s[index]!.key : s.length,
          kind: blocked ? "blocked" : ok ? (writesRef.current.has(tool) ? "write" : "read") : "error",
          title: tool,
          args: index >= 0 ? s[index]!.args : undefined,
          detail: preview(ok ? text : text),
          guard: d.warning ? "flagged" : undefined,
        };
        if (index < 0) return [...s, updated];
        const next = s.slice();
        next[index] = updated;
        return next;
      });
    });
    on("agent_finished", (d) => setSummary(String(d.summary ?? "")));
    on("error", (d) => {
      setSteps((s) => [...s, { key: s.length, kind: "error", title: "Stopped", detail: String(d.message ?? "") }]);
    });
    on("review", () => refresh());
    on("done", () => {
      source.close();
      refresh();
      onChangedRef.current();
    });
    return () => {
      alive = false;
      source.close();
    };
  }, [id]);

  const canDecide = hasToken() || Boolean(status?.public_demo);
  const review = detail?.review ?? null;
  const owners = new Set(review?.changes.map((c) => JSON.stringify(c.owner)).filter((o) => o !== "null"));
  const replayCalls = detail ? detail.calls.filter((c) => detail.replay.includes(c.index)) : [];
  const replayCounts = replayCalls.reduce<Record<string, number>>((acc, c) => {
    acc[c.tool] = (acc[c.tool] ?? 0) + 1;
    return acc;
  }, {});
  const needsAck = review?.risk === "high";

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
      const updated = await api.decide(detail.id, decision, name, note.trim());
      setDetail({ ...updated });
      onChanged();
      requestAnimationFrame(() => outcomeRef.current?.focus());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const running = detail?.running ?? true;
  const Verdict = review ? VERDICT[review.risk] : null;

  return (
    <section className="card" aria-labelledby={`rehearsal-${id}`}>
      <div className="card-head">
        <div>
          <p className="eyebrow">Rehearsal {id}</p>
          <h2 id={`rehearsal-${id}`}>{running ? "The agent is working on a shadow copy" : "What the agent did"}</h2>
        </div>
        {detail && <span className="pill">{detail.model}</span>}
      </div>

      <div className="shadow-note">
        <ShieldCheck size={18} aria-hidden="true" />
        <p>
          Every call the agent made ran against a private copy of {status?.target ?? "the app"}. Live data has not been
          touched. {running ? "" : "You decide what happens next."}
        </p>
      </div>

      {!review && (
        <>
          <StepList steps={steps} live />
          {running && (
            <p className="working" role="status">
              <Loader2 size={16} className="spin" aria-hidden="true" /> Working. Free models can pause briefly for
              rate limits.
            </p>
          )}
        </>
      )}

      {review && Verdict && (
        <div className="review">
          <div className="verdict" data-risk={review.risk}>
            <Verdict.icon size={22} aria-hidden="true" />
            <div>
              <h3>{review.changes.length === 0 ? "The agent changed nothing." : Verdict.title}</h3>
              <p className="hint">{review.changes.length === 0 ? "There is nothing to apply." : Verdict.body}</p>
            </div>
          </div>

          <dl className="stats">
            <div className="stat">
              <dt>Money out</dt>
              <dd>{review.money_out} USD</dd>
            </div>
            <div className="stat">
              <dt>Rows that would change</dt>
              <dd>{review.changes.length}</dd>
            </div>
            <div className="stat">
              <dt>Customers affected</dt>
              <dd>{owners.size}</dd>
            </div>
            <div className="stat">
              <dt>Writes to replay</dt>
              <dd>{replayCalls.length}</dd>
            </div>
          </dl>

          {review.flags.length > 0 && (
            <>
              <h3 className="visually-hidden">Risk flags</h3>
              <ul className="flags">
                {review.flags.map((f) => (
                  <li key={f.title} className="flag" data-level={f.level}>
                    <span className="pill" data-tone={f.level}>{f.level}</span>
                    <div>
                      <p className="flag-title">{f.title}</p>
                      <p className="flag-detail">{f.detail}</p>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}

          {review.changes.length > 0 && (
            <div className="changes">
              <h3>Exact changes, row by row</h3>
              {groupChanges(review.changes).map(([table, rows]) => (
                <details key={table} className="change-group" open={rows.length <= 8}>
                  <summary>
                    {table} <span className="hint num">({rows.length})</span>
                  </summary>
                  <ul className="change-list">
                    {rows.map((c) => (
                      <li key={`${c.table}-${JSON.stringify(c.key)}`}>
                        <span className="kind" data-kind={c.kind}>{c.kind}</span>
                        <span>{c.summary}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              ))}
            </div>
          )}

          {replayCalls.length > 0 && detail?.state === "ready" && (
            <div className="replay">
              <h3>If you approve</h3>
              <p className="hint">
                Rehearsal replays {replayCalls.length} recorded write{replayCalls.length === 1 ? "" : "s"} on live
                data (
                {Object.entries(replayCounts)
                  .map(([tool, n]) => `${tool} ${n > 1 ? `x${n}` : ""}`.trim())
                  .join(", ")}
                ), then checks that live changed exactly as shown above. If anything differs, live is restored.
              </p>
            </div>
          )}

          {detail?.state === "ready" && (
            <div className="decision">
              {!canDecide && (
                <p className="hint">
                  This view is read only. Open the console with the link printed when Rehearsal started to make
                  decisions.
                </p>
              )}
              <div className="fields">
                <label className="field" htmlFor={ids.reviewer}>
                  Your name
                  <input id={ids.reviewer} value={reviewer} maxLength={60} autoComplete="name"
                    placeholder="Reviewer" onChange={(e) => setReviewer(e.target.value)} />
                </label>
                <label className="field" htmlFor={ids.note}>
                  Note for the audit log (optional)
                  <input id={ids.note} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
                </label>
              </div>
              {needsAck && review.changes.length > 0 && (
                <label className="check" htmlFor={ids.ack}>
                  <input id={ids.ack} type="checkbox" checked={acknowledged}
                    onChange={(e) => setAcknowledged(e.target.checked)} />
                  <span>I have read every high risk flag and still want to apply these changes.</span>
                </label>
              )}
              {error && <p className="error" role="alert">{error}</p>}
              <div className="decision-buttons">
                <button type="button" className="button danger" disabled={!canDecide || busy !== null}
                  onClick={() => decide("reject")}>
                  {busy === "reject" ? <Loader2 size={17} className="spin" aria-hidden="true" /> : <X size={17} aria-hidden="true" />}
                  Reject
                </button>
                <button type="button" className="button primary"
                  disabled={!canDecide || busy !== null || review.changes.length === 0 || (needsAck && !acknowledged)}
                  onClick={() => decide("approve")}>
                  {busy === "approve" ? <Loader2 size={17} className="spin" aria-hidden="true" /> : <CircleCheck size={17} aria-hidden="true" />}
                  {busy === "approve" ? "Applying" : "Approve and apply"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {review && (
        <details className="steps-details">
          <summary>
            What the agent did, step by step <span className="hint num">({steps.length})</span>
          </summary>
          {summary && <p className="hint spaced-top">Agent summary: {summary}</p>}
          <StepList steps={steps} />
        </details>
      )}

      {detail?.outcome && (
        <div className="outcome" data-state={detail.outcome.state} ref={outcomeRef} tabIndex={-1} role="status">
          {detail.outcome.state === "applied" ? (
            <CircleCheck size={22} aria-hidden="true" />
          ) : detail.outcome.state === "undone" ? (
            <RotateCcw size={22} aria-hidden="true" />
          ) : (
            <CircleAlert size={22} aria-hidden="true" />
          )}
          <div className="outcome-body">
            <h3>
              {{ applied: "Applied and verified", rejected: "Rejected", undone: "Undone", stale: "Not applied",
                failed: "Rolled back" }[detail.outcome.state] ?? detail.outcome.state}
            </h3>
            <p>{detail.outcome.message}</p>
            {detail.outcome.state === "applied" && canDecide && (
              <div>
                <button type="button" className="button" disabled={busy !== null} onClick={() => decide("undo")}>
                  <Undo2 size={17} aria-hidden="true" /> Undo
                </button>
              </div>
            )}
            {error && <p className="error" role="alert">{error}</p>}
          </div>
        </div>
      )}
      {!detail && error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}
