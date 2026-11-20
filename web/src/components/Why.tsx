// "Why is this here?" for each proposed action, and the agent's words set against its actions.

import { ChevronDown } from "lucide-react";

import type { Call, Explanation, Review, Source } from "../api";
import { stepSentence } from "./words";

function label(arg: string): string {
  const base = arg.replace(/_?id$/, "").replace(/_/g, " ").trim();
  return base ? base.charAt(0).toUpperCase() + base.slice(1) : "Record";
}

/** The excerpt with every whole-word occurrence of the value marked. */
function Highlight({ text, value }: { text: string; value: string }) {
  const safe = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(?<![\\w.])(${safe})(?!\\w)`));
  return <>{parts.map((part, i) => (i % 2 === 1 ? <mark key={i}>{part}</mark> : part))}</>;
}

function SourceLine({ source: s, calls }: { source: Source; calls: Call[] }) {
  const name = `${label(s.arg)} ${s.value}`;
  const step = calls.find((c) => c.index === s.call);
  const where = step ? `step ${step.index}: ${stepSentence(step.tool, step.args).toLowerCase()}` : `step ${s.call}`;
  switch (s.kind) {
    case "request":
      return <p>{name} is the one you asked about.</p>;
    case "data":
      return <p>{name} came from the app's own records ({where}).</p>;
    case "text":
      return (
        <>
          <p className="why-risk">
            {name} appears only inside text the agent read ({where}), never in the app's own records. Anyone can
            write text, including an attacker:
          </p>
          <blockquote className="why-quote">
            <Highlight text={s.excerpt ?? ""} value={String(s.value)} />
          </blockquote>
        </>
      );
    default:
      return <p className="why-risk">{name} never appeared in your request or in anything the agent read.</p>;
  }
}

interface ActionProps {
  sentence: string;
  why: Explanation | undefined;
  review: Review;
  calls: Call[];
}

/** One proposed action. It opens to show where its targets came from and every row it changes. */
export function ActionItem({ sentence, why, review, calls }: ActionProps) {
  if (!why) return <li>{sentence}</li>;
  const fromText = why.sources.some((s) => s.kind === "text");
  const unknown = why.sources.some((s) => s.kind === "unknown");
  const rows = why.rows.map((i) => review.changes[i]).filter((c) => c !== undefined);
  return (
    <li className="action">
      <details className="why">
        <summary>
          <span className="why-sentence">{sentence}</span>
          {fromText && <span className="tag risk">named in message text</span>}
          {unknown && <span className="tag risk">never looked up</span>}
          <span className="why-toggle">
            Why? <ChevronDown size={14} aria-hidden="true" />
          </span>
        </summary>
        <div className="why-body">
          {why.sources.map((s) => (
            <SourceLine key={s.arg} source={s} calls={calls} />
          ))}
          {rows.length > 0 && (
            <>
              <p className="why-label">What it changes on the live store</p>
              <ul className="why-rows">
                {rows.map((c) => (
                  <li key={`${c.table}-${JSON.stringify(c.key)}`}>{c.summary}</li>
                ))}
              </ul>
            </>
          )}
          <p className="why-step">Step {why.call} of the agent's work on the private copy.</p>
        </div>
      </details>
    </li>
  );
}

/** The exact text the changes would send to the customer, readable before anyone approves it. */
export function ReplyPreview({ review }: { review: Review }) {
  const replies = review.replies ?? [];
  if (replies.length === 0) return null;
  return (
    <details className="why reply-preview">
      <summary>
        <span className="why-sentence">
          Read the {replies.length === 1 ? "reply" : `${replies.length} replies`} the customer will get
        </span>
        <span className="why-toggle">
          Show <ChevronDown size={14} aria-hidden="true" />
        </span>
      </summary>
      {replies.map((text, i) => (
        <blockquote key={i} className="reply-text">{text}</blockquote>
      ))}
    </details>
  );
}

const singular = (table: string) => (table.endsWith("s") ? table.slice(0, -1) : table).replace(/_/g, " ");

function didLines(review: Review): string[] {
  const counts = new Map<string, { verb: string; table: string; n: number }>();
  for (const c of review.changes) {
    const verb = { added: "Add", changed: "Change", removed: "Delete" }[c.kind];
    const key = `${verb}:${c.table}`;
    const entry = counts.get(key) ?? { verb, table: c.table, n: 0 };
    entry.n += 1;
    counts.set(key, entry);
  }
  const lines = [...counts.values()].map(({ verb, table, n }) =>
    `${verb} ${n} ${n === 1 ? singular(table) : table.replace(/_/g, " ")}`);
  lines.unshift(Number(review.money_out.replace(/,/g, "")) > 0 ? `Pay out ${review.money_out} USD` : "Pay out nothing");
  return lines;
}

const clip = (text: string, n = 180) => (text.length > n ? `${text.slice(0, n - 3).trimEnd()}...` : text);

/** Shown only when what the agent says does not match what the changes do. */
export function SaidDid({ review }: { review: Review }) {
  const sayDo = review.flags.filter((f) => f.rule === "say_do");
  if (sayDo.length === 0) return null;
  const said = review.summary?.trim();
  const reply = sayDo.some((f) => f.title.startsWith("The reply")) ? review.replies?.[0]?.trim() : undefined;
  return (
    <div className="saydo">
      <div>
        <p className="saydo-label">The agent told you</p>
        <p className="saydo-said">{said ? `“${clip(said)}”` : "Nothing at all."}</p>
        {reply && (
          <>
            <p className="saydo-label saydo-gap">The reply tells the customer</p>
            <p className="saydo-said">“{clip(reply)}”</p>
          </>
        )}
      </div>
      <div>
        <p className="saydo-label">The changes would actually</p>
        <ul>
          {didLines(review).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}
