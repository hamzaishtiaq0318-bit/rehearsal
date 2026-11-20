import { CircleAlert, Link2, RefreshCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { AuditEntry } from "../api";
import { api, errorMessage, hasToken } from "../api";
import { when } from "./labels";

const EVENT_LABEL: Record<string, string> = {
  engine_started: "Rehearsal started up",
  rehearsal_started: "Rehearsal opened",
  call: "Tool call on shadow",
  review_ready: "Review ready",
  rehearsal_applied: "Applied to live",
  rehearsal_rejected: "Rejected",
  rehearsal_stale: "Refused: live changed",
  rehearsal_failed: "Rolled back",
  rehearsal_undone: "Undone",
  rollback_verified: "Rollback verified",
  rollback_unverified: "Rollback not verified",
  demo_reset: "Demo data reset",
  tools_reloaded: "Tools reloaded",
};

function details(e: AuditEntry): string {
  if (e.event === "call") {
    const parts = [String(e.tool ?? ""), e.writes ? "write" : "read"];
    if (e.blocked) parts.push("blocked");
    else if (e.ok === false) parts.push("failed");
    return parts.join(", ");
  }
  if (e.event === "review_ready") return `${String(e.rows_changed)} rows, ${String(e.risk)} risk`;
  if (typeof e.message === "string") return e.message;
  if (typeof e.task === "string") return e.task;
  return "";
}

export function AuditView({ publicDemo, onReset }: { publicDemo: boolean; onReset: () => void }) {
  const [data, setData] = useState<{ verified: boolean; entries: AuditEntry[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloaded, setReloaded] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  async function reloadTools() {
    setReloaded(null);
    try {
      const r = await api.reloadTools();
      const changes = [r.added.length ? `added ${r.added.join(", ")}` : "", r.removed.length ? `removed ${r.removed.join(", ")}` : ""]
        .filter(Boolean).join("; ");
      setReloaded(`Loaded ${r.server_file} with ${r.tools.length} tools${changes ? `: ${changes}` : ", no changes"}.`);
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  const load = useCallback(() => {
    api
      .audit()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);
  useEffect(load, [load]);

  async function reset() {
    dialog.current?.close();
    try {
      await api.reset();
      onReset();
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="main-inner">
      <section className="card" aria-labelledby="audit-title">
        <div className="card-head">
          <div>
            <p className="eyebrow">Audit log</p>
            <h1 id="audit-title" className="ticket-title">
              Every rehearsal, call and decision
            </h1>
          </div>
          {data && (
            <span className="pill" data-tone={data.verified ? "good" : "high"}>
              {data.verified ? <Link2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
              {data.verified ? "Hash chain intact" : "Hash chain broken"}
            </span>
          )}
        </div>
        <p className="hint spaced-bottom">
          Each entry stores the hash of the one before it, so editing or deleting any past entry breaks the chain.
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {data ? (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Audit log entries, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Event</th>
                  <th scope="col">Rehearsal</th>
                  <th scope="col">By</th>
                  <th scope="col">Details</th>
                </tr>
              </thead>
              <tbody>
                {[...data.entries].reverse().map((e) => (
                  <tr key={String(e.hash)}>
                    <td className="num nowrap">{when(e.at)}</td>
                    <td>{EVENT_LABEL[e.event] ?? e.event}</td>
                    <td className="mono">{e.rehearsal ?? ""}</td>
                    <td>{e.by ?? (typeof e.actor === "string" ? e.actor : "")}</td>
                    <td>{details(e)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !error && (
            <p className="hint" role="status">
              Loading the audit log
            </p>
          )
        )}
      </section>

      {(hasToken() || publicDemo) && (
        <section className="card" aria-labelledby="reset-title">
          <h2 id="reset-title">Demo data</h2>
          <p className="hint spaced-y">
            Put Kestrel Desk back to its original demo records. The audit log keeps its history.
          </p>
          <button type="button" className="button" onClick={() => dialog.current?.showModal()}>
            <RefreshCcw size={17} aria-hidden="true" /> Reset demo data
          </button>
        </section>
      )}

      {hasToken() && !publicDemo && (
        <section className="card" aria-labelledby="tools-title">
          <h2 id="tools-title">Synapse tools</h2>
          <p className="hint spaced-y">
            Rebuilt the MCP server with <code>synapse build --discover</code>? Load it without restarting. A rehearsal
            made with the old server is refused at approval and must be run again.
          </p>
          <button type="button" className="button" onClick={reloadTools}>
            <RefreshCcw size={17} aria-hidden="true" /> Reload tools
          </button>
          {reloaded && <p className="hint spaced-y" role="status">{reloaded}</p>}
        </section>
      )}

      <dialog ref={dialog} aria-labelledby="reset-dialog-title">
        <h2 id="reset-dialog-title">Reset the demo data?</h2>
        <p className="hint">Every ticket, order and refund returns to its original state. This cannot be undone.</p>
        <div className="decision-buttons spaced-top-lg">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="button" className="button primary" onClick={reset}>
            Reset
          </button>
        </div>
      </dialog>
    </div>
  );
}
