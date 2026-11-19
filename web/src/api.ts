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
  active: number;
  max_active: number;
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
}

export interface Change {
  table: string;
  key: Record<string, unknown>;
  kind: "added" | "changed" | "removed";
  owner: unknown;
  summary: string;
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
  review: { risk: Risk; money_out: string; flags: Flag[]; changes: Change[] } | null;
  outcome: { state: string; message: string; verified: boolean } | null;
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

async function request<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { headers, cache: "no-store" };
  if (body !== undefined) {
    init.method = "POST";
    headers["Content-Type"] = "application/json";
    const t = token();
    if (t) headers[TOKEN_HEADER] = t;
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

export const api = {
  status: () => request<Status>("/api/status"),
  tickets: () => request<TicketRow[]>("/api/tickets"),
  ticket: (id: number) => request<TicketDetail>(`/api/tickets/${id}`),
  rehearsal: (id: string) => request<RehearsalDetail>(`/api/rehearsals/${encodeURIComponent(id)}`),
  conversations: () => request<ConversationRow[]>("/api/rehearsals"),
  start: (body: { ticket_id?: number; message?: string; mode: StartMode }) =>
    request<{ id: string; title: string }>("/api/rehearsals", body),
  decide: (id: string, decision: Decision, reviewer: string, reason = "") =>
    request<RehearsalDetail>(`/api/rehearsals/${encodeURIComponent(id)}/${decision}`, { reviewer, reason }),
  audit: () => request<{ verified: boolean; count: number; entries: AuditEntry[] }>("/api/audit"),
  reset: () => request<{ ok: boolean }>("/api/reset", {}),
  eventsUrl: (id: string) => `/api/rehearsals/${encodeURIComponent(id)}/events`,
};

export const errorMessage = (e: unknown): string =>
  e instanceof Error ? e.message : "Something went wrong. Please try again.";
