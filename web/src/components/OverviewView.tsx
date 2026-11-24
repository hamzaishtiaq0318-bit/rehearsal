import { CircleAlert, Link2 } from "lucide-react";
import { useEffect, useState } from "react";

import type { Overview } from "../api";
import { api, errorMessage } from "../api";

const usd = (cents: number) =>
  `${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USD`;

/** The protection dashboard: what Rehearsal has done, counted from the hash chained audit log. */
export function OverviewView() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.overview().then((o) => alive && (setData(o), setError(null))).catch((e) => alive && setError(errorMessage(e)));
    load();
    const timer = window.setInterval(load, 10_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const decided = data ? data.approved + data.applied_automatically + data.cancelled : 0;
  return (
    <div className="main-inner">
      <section className="card" aria-labelledby="overview-title">
        <div className="card-head">
          <div>
            <p className="eyebrow">Overview</p>
            <h1 id="overview-title" className="ticket-title">What Rehearsal has done</h1>
          </div>
          {data && (
            <span className="pill" data-tone={data.chain_intact ? "good" : "high"}>
              {data.chain_intact ? <Link2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
              {data.chain_intact ? "Counted from an intact audit log" : "Audit log hash chain broken"}
            </span>
          )}
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {data ? (
          <>
            <dl className="facts-row overview-row">
              <div data-tone="high"><dt>Money stopped</dt><dd className="num">{usd(data.money_stopped_cents)}</dd></div>
              <div data-tone="high"><dt>High risk tasks stopped</dt><dd className="num">{data.high_risk_stopped}</dd></div>
              <div><dt>Money approved and live</dt><dd className="num">{usd(data.money_applied_cents)}</dd></div>
            </dl>
            <dl className="facts-row overview-row five">
              <div><dt>Tasks rehearsed</dt><dd className="num">{data.rehearsed}</dd></div>
              <div><dt>Applied without asking</dt><dd className="num">{data.applied_automatically}</dd></div>
              <div><dt>Approved by a person</dt><dd className="num">{data.approved}</dd></div>
              <div><dt>Cancelled</dt><dd className="num">{data.cancelled}</dd></div>
              <div><dt>Undone</dt><dd className="num">{data.undone}</dd></div>
            </dl>
            <p className="hint spaced-y">
              {decided === 0
                ? "Nothing has been decided yet. Start a conversation and these numbers fill in."
                : `Of ${decided} decided ${decided === 1 ? "task" : "tasks"}, ${data.applied_automatically} needed nobody, `
                  + `${data.approved} ${data.approved === 1 ? "was" : "were"} approved after a look, and `
                  + `${data.cancelled} never reached live data.`}
              {" "}Every number comes from the audit log, where an edited entry breaks the hash chain.
            </p>
          </>
        ) : (
          !error && <p className="hint" role="status">Loading the overview</p>
        )}
      </section>
    </div>
  );
}
