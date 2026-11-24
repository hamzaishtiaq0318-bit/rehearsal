// Typed client for the Rehearsal console API.

export type Risk = "high" | "medium" | "low";

/** How the console can start a run. Runs from outside agents over MCP show up as "external". */
export type StartMode = "agent" | "simulated" | "replay";

export interface Tool {
  name: string;
  description: string;
  writes: boolean;
  method: string;
  path: string;
}

export interface Status {
  target: string;
  public_demo: boolean;
  server_file: string;
  tools: Tool[];
  models: { model: string; state: string }[];
  prompt_guard: boolean;
  recordings: number[];
  simulation_ticket: number;
  /** Tickets a scripted agent can act out as an attack, with the attack's name. */
  simulations?: { ticket_id: number; name: string }[];
  active: number;
  max_active: number;
  /** Set only by the website's browser demo, which replays recorded runs with no server. */
  static_demo?: boolean;
}

export interface TicketRow {
  id: number;
  subject: string;
  status: "open" | "pending" | "closed";
  priority: "low" | "normal" | "high" | "urgent";
  order_id: number | null;
  customer: string;
  opened_at: string;
  rehearsal: { id: string; state: string; risk: Risk | null } | null;
}

export interface TicketDetail {
  id: number;
  subject: string;
  status: TicketRow["status"];
  priority: TicketRow["priority"];
  order_id: number | null;
  opened_at: string;
  customer: { id: number; name: string; email: string; tier: string; store_credit: string };
  messages: { id: number; author: "customer" | "support"; body: string; sent_at: string }[];
}

export interface Flag {
  level: Risk;
  title: string;
  detail: string;
  /** effect: what would change. source: where the targets came from. say_do: words against actions. */
  rule?: "effect" | "source" | "say_do";
}

export interface Change {
  table: string;
  key: Record<string, unknown>;
  kind: "added" | "changed" | "removed";
  owner: unknown;
  summary: string;
  /** The recorded calls that changed this row. */
  caused_by?: number[];
}

/** Where one id an action works on first came from. */
export interface Source {
  arg: string;
  value: string | number;
  kind: "request" | "data" | "text" | "unknown";
  call: number | null;
  tool: string | null;
  excerpt: string | null;
}

/** Why one action is proposed: the rows it changes and where its targets came from. */
export interface Explanation {
  call: number;
  rows: number[];
  sources: Source[];
}

export interface Review {
  risk: Risk;
  money_out: string;
  flags: Flag[];
  changes: Change[];
  explain?: Explanation[];
  summary?: string;
  replies?: string[];
  /** Everyone the changes touch, with how many of their rows change and the money they would get. */
  blast?: { id: string | number; name: string; rows: number; money_cents: number }[];
}

export interface Call {
  index: number;
  tool: string;
  args: Record<string, unknown>;
  writes: boolean;
  ok: boolean;
  blocked: boolean;
  result: string;
  injection_score: number | null;
  injection_excerpt: string | null;
  at: string;
}

export interface ConversationRow {
  id: string;
  title: string;
  ticket_id: number | null;
  mode: RehearsalDetail["mode"];
  model: string;
  state: RehearsalDetail["state"];
  created_at: string;
  risk: Risk | null;
  running: boolean;
  changes: number | null;
}

export interface RehearsalDetail {
  id: string;
  task: string;
  title: string;
  request: string;
  ticket_id: number | null;
  actions: string[];
  actor: string;
  state: "rehearsing" | "ready" | "applied" | "rejected" | "stale" | "failed" | "undone";
  created_at: string;
  calls: Call[];
  replay: number[];
  review: Review | null;
  /** Who has approved so far, and whether this one needs two different people (high risk, when configured). */
  approvals?: string[];
  second_approval?: boolean;
  /** auto: decided by the trust policy (no flags at all) rather than a person. */
  outcome: { state: string; message: string; verified: boolean; auto?: boolean } | null;
  mode: StartMode | "external";
  model: string;
  running: boolean;
}

export interface AuditEntry {
  at: string;
  event: string;
  rehearsal?: string;
  by?: string;
  [key: string]: unknown;
}

/** What Rehearsal has done, counted from the audit log. */
export interface Overview {
  rehearsed: number;
  applied_automatically: number;
  approved: number;
  cancelled: number;
  undone: number;
  held_or_rolled_back: number;
  high_risk_stopped: number;
  money_stopped_cents: number;
  money_applied_cents: number;
  chain_intact: boolean;
}

/** The audit log's tamper demonstration: one entry edited on a copy, and where the chain check fails. */
export interface TamperCheck {
  edited: number | null;
  event?: string;
  field?: string;
  was?: string;
  broken_at?: number | null;
  count?: number;
  real_log_intact?: boolean;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const TOKEN_KEY = "rehearsal-token";
const TOKEN_HEADER = "X-Rehearsal-Token";

/** Move the launch token from the address bar into session storage, so it is not left in history. */
export function captureToken(): void {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  if (!token) return;
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage can be blocked; the token then lasts only for this page view */
    memoryToken = token;
  }
  url.searchParams.delete("token");
  window.history.replaceState(null, "", url.toString());
}

let memoryToken: string | null = null;

function token(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

export const hasToken = (): boolean => token() !== null;

/** Every API request carries the link's token: reads show customer data and the audit log, so they need it too. */
function authHeaders(): Record<string, string> {
  const t = token();
  return t ? { [TOKEN_HEADER]: t } : {};
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders() };
  const init: RequestInit = { headers, cache: "no-store" };
  if (body !== undefined) {
    init.method = "POST";
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Cannot reach the Rehearsal server. Is it running?");
  }
  if (!response.ok) {
    let detail = response.statusText || "Request failed";
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail)) detail = "Please check the values you entered.";
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export type Decision = "approve" | "reject" | "undo";

/** Receives each event of a rehearsal once, in order. */
export type EventHandler = (kind: string, data: Record<string, unknown>) => void;

export const EVENT_KINDS = ["started", "message", "tool_call", "tool_result", "agent_finished", "error", "review",
  "done"] as const;

const KINDS: ReadonlySet<string> = new Set(EVENT_KINDS);

/** Follow a rehearsal's live event stream. Returns a function that stops listening.
 *  It reads the server sent events with fetch, because EventSource cannot send the token header. Like
 *  EventSource, a dropped connection is picked up again from the last event received. */
function subscribe(id: string, on: EventHandler): () => void {
  const stop = new AbortController();
  const url = `/api/rehearsals/${encodeURIComponent(id)}/events`;
  let last = -1;
  let finished = false;

  const handle = (block: string) => {
    let eventId = "";
    let kind = "";
    let data = "";
    for (const line of block.split("\n")) {
      const colon = line.indexOf(":");
      if (colon <= 0) continue; // a blank line or a ": keep-alive" comment
      const value = line.slice(colon + 1).replace(/^ /, "");
      const name = line.slice(0, colon);
      if (name === "id") eventId = value;
      else if (name === "event") kind = value;
      else if (name === "data") data += value;
    }
    const n = Number(eventId);
    if (!KINDS.has(kind) || !data || !Number.isInteger(n) || n <= last) return; // each event once, in order
    last = n;
    finished ||= kind === "done";
    on(kind, JSON.parse(data) as Record<string, unknown>);
  };

  void (async () => {
    for (let attempt = 0; !stop.signal.aborted; attempt++) {
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream", ...authHeaders() };
        if (last >= 0) headers["Last-Event-ID"] = String(last);
        const response = await fetch(url, { headers, cache: "no-store", signal: stop.signal });
        if (response.status >= 400 && response.status < 500) return; // unknown rehearsal or no access: stop
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break; // the server ends the stream after the run's last event; anything else reconnects
          attempt = 0;
          buffer += value;
          for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
            handle(buffer.slice(0, end));
            buffer = buffer.slice(end + 2);
          }
        }
      } catch {
        if (stop.signal.aborted) return;
      }
      if (finished) return;
      await new Promise((resolve) => window.setTimeout(resolve, Math.min(1000 * 2 ** attempt, 10_000)));
    }
  })();
  return () => stop.abort();
}

const live = {
  status: () => request<Status>("/api/status"),
  tickets: () => request<TicketRow[]>("/api/tickets"),
  ticket: (id: number) => request<TicketDetail>(`/api/tickets/${id}`),
  rehearsal: (id: string) => request<RehearsalDetail>(`/api/rehearsals/${encodeURIComponent(id)}`),
  conversations: () => request<ConversationRow[]>("/api/rehearsals"),
  start: (body: { ticket_id?: number; message?: string; mode: StartMode }) =>
    request<{ id: string; title: string }>("/api/rehearsals", body),
  decide: (id: string, decision: Decision, reviewer: string, reason = "") =>
    request<RehearsalDetail>(`/api/rehearsals/${encodeURIComponent(id)}/${decision}`, { reviewer, reason }),
  revise: (id: string, reviewer: string, feedback: string) =>
    request<{ id: string; title: string }>(`/api/rehearsals/${encodeURIComponent(id)}/revise`, { reviewer, feedback }),
  audit: () => request<{ verified: boolean; count: number; entries: AuditEntry[] }>("/api/audit"),
  tamperCheck: () => request<TamperCheck>("/api/audit/tamper-check"),
  overview: () => request<Overview>("/api/overview"),
  reset: () => request<{ ok: boolean }>("/api/reset", {}),
  reloadTools: () =>
    request<{ server_file: string; tools: string[]; added: string[]; removed: string[] }>("/api/tools/reload", {}),
  pause: (reviewer: string) => request<{ stopped: number }>("/api/pause", { reviewer }),
  subscribe,
};

export type Api = typeof live;

/** The website's "Try it" build (vite build --mode static) runs on recorded engine runs, with no server. */
export const STATIC_DEMO = import.meta.env.MODE === "static";

function staticApi(): Api {
  const demo = import("./demo").then((m) => m.createDemoApi());
  const call = <K extends Exclude<keyof Api, "subscribe" | "reloadTools" | "pause" | "revise">>(name: K) =>
    (async (...args: unknown[]) => ((await demo)[name] as (...a: unknown[]) => unknown)(...args)) as Api[K];
  return {
    status: call("status"),
    tickets: call("tickets"),
    ticket: call("ticket"),
    rehearsal: call("rehearsal"),
    conversations: call("conversations"),
    start: call("start"),
    decide: call("decide"),
    audit: call("audit"),
    tamperCheck: call("tamperCheck"),
    overview: call("overview"),
    reset: call("reset"),
    reloadTools: async () => {
      throw new ApiError(403, "The browser demo has no tool server to reload");
    },
    pause: async () => ({ stopped: 0 }),
    revise: async () => {
      throw new ApiError(403, "Sending work back needs the live agent. Run Rehearsal yourself to try it.");
    },
    subscribe: (id, on) => {
      let stop = () => undefined as void;
      let stopped = false;
      void demo.then((d) => {
        if (!stopped) stop = d.subscribe(id, on);
      });
      return () => {
        stopped = true;
        stop();
      };
    },
  };
}

export const api: Api = STATIC_DEMO ? staticApi() : live;

export const errorMessage = (e: unknown): string =>
  e instanceof Error ? e.message : "Something went wrong. Please try again.";
