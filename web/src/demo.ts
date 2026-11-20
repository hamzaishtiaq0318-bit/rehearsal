// The website's "Try it" demo: the real console, driven by real engine runs captured with
// `python -m rehearsal.capture`. Nothing here talks to a server. Decisions follow the same rules
// the engine enforces (approve or cancel only once, undo only after an approval).

import type { Api, AuditEntry, ConversationRow, Decision, EventHandler, RehearsalDetail, StartMode, Status,
  TicketDetail, TicketRow } from "./api";
import { ApiError } from "./api";
import data from "./demo-data.json";

interface Scenario {
  ticket_id: number;
  mode: StartMode;
  title: string;
  request: string;
  model: string;
  events: { kind: string; data: Record<string, unknown> }[];
  ready: RehearsalDetail;
  outcomes: Record<"applied" | "rejected" | "undone", RehearsalDetail["outcome"]>;
}

interface DemoData {
  captured_at: string;
  status: Status;
  tickets: TicketRow[];
  ticket_details: Record<string, TicketDetail>;
  audit: { verified: boolean; count: number; entries: AuditEntry[] };
  scenarios: Scenario[];
}

interface LocalRun {
  id: string;
  scenario: number;
  request: string;
  created_at: string;
  started: number; // Date.now() when playback began
  state: RehearsalDetail["state"];
}

const DEMO = data as unknown as DemoData;
// Saved runs point at scenarios by position, so they belong to one capture; a new capture starts fresh.
const STORE = `rehearsal-demo-runs:${DEMO.captured_at}`;

/** How long each kind of event takes to appear, in milliseconds, so the run reads like a live one. */
const PACE: Record<string, number> = { started: 300, message: 700, tool_call: 170, tool_result: 160,
  agent_finished: 600, review: 500, done: 50 };

function schedule(s: Scenario): number[] {
  let t = 0;
  return s.events.map((e) => (t += PACE[e.kind] ?? 200));
}

function newId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function createDemoApi(): Omit<Api, "reloadTools" | "pause"> {
  let runs: LocalRun[] = load();

  function load(): LocalRun[] {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORE) ?? "[]") as LocalRun[];
      return Array.isArray(saved) ? saved.filter((r) => DEMO.scenarios[r.scenario]) : [];
    } catch {
      return [];
    }
  }

  function save(): void {
    try {
      sessionStorage.setItem(STORE, JSON.stringify(runs));
    } catch {
      /* the demo then lasts for this page view only */
    }
  }

  function find(id: string): LocalRun {
    const run = runs.find((r) => r.id === id);
    if (!run) throw new ApiError(404, "Unknown rehearsal");
    return run;
  }

  const running = (run: LocalRun): boolean => {
    const times = schedule(DEMO.scenarios[run.scenario]!);
    return Date.now() - run.started < (times.at(-1) ?? 0);
  };

  function detail(run: LocalRun): RehearsalDetail {
    const s = DEMO.scenarios[run.scenario]!;
    const base = { ...s.ready, id: run.id, request: run.request, created_at: run.created_at };
    if (running(run)) {
      return { ...base, state: "rehearsing", review: null, actions: [], replay: [], outcome: null, running: true };
    }
    const decided = run.state === "applied" || run.state === "rejected" || run.state === "undone";
    return { ...base, state: run.state, outcome: decided ? s.outcomes[run.state as keyof Scenario["outcomes"]] : null,
      running: false };
  }

  return {
    async status() {
      return { ...DEMO.status, public_demo: true, static_demo: true, models: [],
        active: runs.filter(running).length };
    },

    async tickets() {
      return DEMO.tickets.map((t) => {
        const run = runs.filter((r) => DEMO.scenarios[r.scenario]!.ticket_id === t.id).at(-1);
        const risk = run && DEMO.scenarios[run.scenario]!.ready.review?.risk;
        return { ...t, rehearsal: run ? { id: run.id, state: run.state, risk: risk ?? null } : null };
      });
    },

    async ticket(id: number) {
      const t = DEMO.ticket_details[String(id)];
      if (!t) throw new ApiError(404, "Not found in the live app");
      return t;
    },

    async rehearsal(id: string) {
      return detail(find(id));
    },

    async conversations(): Promise<ConversationRow[]> {
      return [...runs].reverse().map((run) => {
        const d = detail(run);
        return { id: run.id, title: d.title, ticket_id: d.ticket_id, mode: d.mode, model: d.model, state: d.state,
          created_at: run.created_at, risk: d.review?.risk ?? null, running: d.running,
          changes: d.review ? d.review.changes.length : null };
      });
    },

    async start(body: { ticket_id?: number; message?: string; mode: StartMode }) {
      const index = DEMO.scenarios.findIndex((s) => s.ticket_id === body.ticket_id && s.mode === body.mode);
      if (index < 0) {
        throw new ApiError(404, body.ticket_id ? "This browser demo has no recorded run for that ticket. Try 1001 to 1006."
          : "The browser demo only takes requests about a ticket. Mention a ticket number.");
      }
      const s = DEMO.scenarios[index]!;
      // A run the trust policy applied by itself starts out applied, exactly as the engine left it.
      const run: LocalRun = { id: newId(), scenario: index, request: body.message?.trim() || s.request,
        created_at: new Date().toISOString(), started: Date.now(), state: s.ready.state };
      runs.push(run);
      save();
      return { id: run.id, title: s.title };
    },

    async decide(id: string, decision: Decision) {
      const run = find(id);
      if (running(run)) throw new ApiError(409, "The agent is still working. Wait for the review.");
      const s = DEMO.scenarios[run.scenario]!;
      if (decision === "undo") {
        if (run.state !== "applied") throw new ApiError(409, "Only applied changes can be undone");
        run.state = "undone";
      } else {
        if (run.state !== "ready") throw new ApiError(409, `This rehearsal is ${run.state} and cannot change now`);
        if (decision === "approve" && s.ready.actions.length === 0) throw new ApiError(409, "Nothing to approve");
        run.state = decision === "approve" ? "applied" : "rejected";
      }
      save();
      return detail(run);
    },

    async audit() {
      return DEMO.audit;
    },

    async reset() {
      runs = [];
      save();
      return { ok: true };
    },

    subscribe(id: string, on: EventHandler) {
      const run = runs.find((r) => r.id === id);
      if (!run) return () => undefined;
      const s = DEMO.scenarios[run.scenario]!;
      const elapsed = Date.now() - run.started;
      // Events already due play at once (after a reload, for example); the rest keep their pace.
      // Always asynchronous, so a handler may stop the subscription from inside itself.
      const timers = schedule(s).map((at, i) =>
        window.setTimeout(() => on(s.events[i]!.kind, s.events[i]!.data), Math.max(0, at - elapsed)));
      return () => timers.forEach((t) => window.clearTimeout(t));
    },
  };
}
