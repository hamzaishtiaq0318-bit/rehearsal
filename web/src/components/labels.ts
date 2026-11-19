import type { Risk, TicketRow } from "../api";

export const RISK_LABEL: Record<Risk, string> = { high: "High risk", medium: "Review", low: "Low risk" };

export const STATE_LABEL: Record<string, string> = {
  rehearsing: "Rehearsing",
  ready: "Awaiting review",
  applied: "Applied",
  rejected: "Rejected",
  stale: "Needs a new rehearsal",
  failed: "Rolled back",
  undone: "Undone",
};

export function rehearsalTone(r: TicketRow["rehearsal"]): string | undefined {
  if (!r) return undefined;
  if (r.state === "ready") return r.risk ?? undefined;
  if (r.state === "applied") return "good";
  return undefined;
}

export function when(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
