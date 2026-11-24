# `synapse build --rehearse`: a proposal for the Synapse team

Synapse answers the first question a company asks about agents: *can my existing app be used by one?*
Rehearsal answers the second: *can I let it change anything?* This page sketches how Synapse could
answer both, using what this project already proves works.

## The gap

Every Synapse user who exposes a write endpoint hits the same wall. The generated tools work, but a
single wrong call refunds the wrong customer, closes the wrong ticket or mails the wrong person, and
there is no undo. The usual answers do not hold up:

- **Injection filters** judge words. In our [attack gallery](../README.md#attack-gallery-seven-ways-an-agent-goes-wrong),
  Llama Prompt Guard 2 caught none of seven attacks.
- **Approving every call** shows a person one reasonable looking call at a time ("refund 88 dollars on
  order 5035") and never the whole effect: twelve refunds, 3,428 dollars, twelve strangers.

## What Rehearsal adds, without touching Synapse's output

| Synapse property | What Rehearsal builds on it |
|---|---|
| The generated server reads `ENDPOINT_BASE_URL` on every call | Launch the same server twice: once against a shadow copy of the app, once against live |
| The generated code holds each tool's method and route | Know which tools write, with no hand maintained list |
| Rebuilds with `--discover` keep the same tools | A rehearsal made with an old server is refused at approval |
| Auto mode works on unmodified apps | `rehearsal init` writes the whole config from the server, the app's code and its schema |

The result: every agent task runs on a private copy, a person sees the exact row changes with plain
reasons, and approved changes are replayed on live and checked to match. Work that raises no flag is
applied automatically, so people are only interrupted when money moves or something looks wrong.

## What it could look like inside Synapse

```bash
synapse build --local --auto --rehearse
```

1. **Generate as today**, plus a `rehearsal.toml` written the way `rehearsal init` does it: start
   command, database variable, owner table, money tables, outgoing text, personal data columns.
2. **Emit a rehearsing server** next to `synapse_auto_server.py`: the same tools, wrapped so that calls
   run on a shadow copy and a `submit_for_review` tool hands the result to a person. MCP clients
   configure it exactly like today's server; nothing changes for Cursor or other MCP client users.
3. **Show the review** in the Synapse console: the changes, the flags, Approve and Undo.
4. **Keep the audit trail** with each server in the account, hash chained so edits are detectable.

Steps 1 and 2 are what this repository already does locally (`python -m rehearsal.init` and
`python -m rehearsal.proxy`). The open work is mostly databases beyond SQLite, which would use
template databases or branching for the shadow copy, with the same review and replay on top.

## Why it matters for Synapse

- It turns "agent ready" into "agent ready and safe to switch on", which is the step where most
  pilots stall today.
- It makes the generated server's write endpoints usable in production without each customer
  building their own guardrails.
- It gives Synapse a reason to sit on the write path of every agent action, not only the first build.

## Seven things we found while building on Synapse 0.1.9

Each is worked around in Rehearsal and covered by a test; the list with details is in
[docs/mcp-setup.md](mcp-setup.md#synapse-behaviour-we-found-and-worked-around). The two that matter
most for a rehearsing mode: query parameters are not forwarded (filters are silently dropped), and
tool arguments are typed `Any` with path arguments pasted into URLs, so `1/orders` or `../admin`
reaches a different route unless something checks it first.
