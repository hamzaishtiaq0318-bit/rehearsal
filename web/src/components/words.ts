// Everything the console says, written the way a helpful colleague would say it.

import type { Risk } from "../api";

function money(value: unknown): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(2)} USD` : String(value);
}

/** A plain sentence for one tool call the agent made on the private copy. */
export function stepSentence(tool: string, args: Record<string, unknown>): string {
  const a = args;
  switch (tool) {
    case "get_ticket":
      return `Read ticket ${a.ticket_id}`;
    case "list_tickets":
      return "Looked through the ticket queue";
    case "get_order":
      return `Looked up order ${a.order_id}`;
    case "list_customer_orders":
      return `Checked the orders of customer ${a.customer_id}`;
    case "get_customer":
      return `Looked up customer ${a.customer_id}`;
    case "get_refund_policy":
      return "Read the refund policy";
    case "refund_order":
      return `Refunded ${money(a.amount)} on order ${a.order_id}`;
    case "add_store_credit":
      return `Added ${money(a.amount)} store credit for customer ${a.customer_id}`;
    case "reply_to_ticket":
      return `Wrote a reply on ticket ${a.ticket_id}`;
    case "update_ticket":
      if (a.status === "closed") return `Closed ticket ${a.ticket_id}`;
      if (a.status === "pending") return `Marked ticket ${a.ticket_id} as pending`;
      if (a.priority) return `Set ticket ${a.ticket_id} to ${String(a.priority)} priority`;
      return `Updated ticket ${a.ticket_id}`;
    default: {
      const name = tool.replace(/_/g, " ");
      return name.charAt(0).toUpperCase() + name.slice(1);
    }
  }
}

export const VERDICT: Record<Risk, { title: string; body: string }> = {
  high: {
    title: "Heads up. This one looks risky.",
    body: "Some of these changes are not what a support task normally does. Please read the warnings before you decide.",
  },
  medium: {
    title: "This looks reasonable, but money is involved.",
    body: "Nothing unusual came up. Give the changes a quick look before you approve.",
  },
  low: {
    title: "This looks safe.",
    body: "Nothing unusual came up in the checks.",
  },
};

export function outcomeText(state: string, count: number, auto = false): { title: string; body: string } {
  const changes = `${count} ${count === 1 ? "change" : "changes"}`;
  switch (state) {
    case "applied":
      return auto
        ? {
            title: "Applied without asking you, because nothing was flagged.",
            body: `No money moves, only one customer is involved and nothing looked unusual, so your trust policy let ${changes} through. I checked that the live store matches the rehearsal.`,
          }
        : {
            title: "Done. It is live.",
            body: `I made ${changes} on the live store and checked that everything matches what you approved.`,
          };
    case "rejected":
      return { title: "No problem. I left everything as it was.", body: "Nothing was changed on the live store." };
    case "undone":
      return { title: "Undone.", body: "The live store is back to exactly how it was before you approved." };
    case "stale":
      return {
        title: "I held off.",
        body: "The live data changed while you were reviewing, so these changes might not fit anymore. Ask me again and I will rehearse it fresh.",
      };
    case "failed":
      return {
        title: "Something did not match, so I put everything back.",
        body: "The live store is exactly as it was before you approved.",
      };
    default:
      return { title: state, body: "" };
  }
}

export const MODES = {
  agent: { label: "AI agent", short: "Agent", hint: "A free AI model works on the request." },
  replay: { label: "Recorded run", short: "Recorded", hint: "Replays a real model's earlier decisions. Works without an API key." },
  simulated: { label: "Attack simulation", short: "Attack", hint: "A scripted agent that obeys the hidden instruction in ticket 1006." },
} as const;

export function dayLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Earlier";
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return "Today";
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return "Earlier";
}

/** A plain sentence for several calls to the same tool in a row. */
export function groupSentence(tool: string, count: number): string {
  switch (tool) {
    case "get_order":
      return `Looked up ${count} orders`;
    case "get_ticket":
      return `Read ${count} tickets`;
    case "get_customer":
      return `Looked up ${count} customers`;
    case "refund_order":
      return `Refunded ${count} orders`;
    case "add_store_credit":
      return `Added store credit ${count} times`;
    case "reply_to_ticket":
      return `Wrote ${count} replies`;
    case "update_ticket":
      return `Updated ${count} tickets`;
    default:
      return `${stepSentence(tool, {})} (${count} times)`;
  }
}
