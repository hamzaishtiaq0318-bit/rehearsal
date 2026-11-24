# Rehearsal: submission write-up

**Track 4, open innovation.** Every AI agent action gets a dress rehearsal.

- Website: https://rehearsal-ai-app.web.app
- Try it in the browser, no install: https://rehearsal-ai-app.web.app/try
- Repository: https://github.com/hamzaishtiaq0318-bit/rehearsal
- 48 second film: https://rehearsal-ai-app.web.app/#film
- 2 minute walkthrough of the real console (a free model, the attack, a real Synapse rebuild): https://rehearsal-ai-app.web.app/assets/film/walkthrough.mp4

## The problem

Synapse makes an existing app usable by AI agents in minutes. The hard part comes next: letting the agent use the tools that change data. Filtering the words an agent reads misses polite attacks; a dedicated injection classifier scored ours 0.14 out of 1. Approving every call shows a person one reasonable looking refund at a time and never the whole effect: twelve refunds, 3,428 dollars, twelve different customers, from one ticket.

## What Rehearsal does

1. **Rehearse.** Each task gets a private copy of the app and its database. The agent's tool calls run there and are recorded.
2. **Review.** Rehearsal diffs the copy row by row and tells a person, in plain words, what would change, with reasons for anything unusual: money leaving, several customers in one task, ids found only inside message text, a reply that leaks another customer's data, a summary that leaves out the money, a customer paid past the daily limit.
3. **Decide and verify.** Approving replays only the recorded writes on the live app, then checks live changed exactly as rehearsed. If anything differs, or the tool server fails part way, live is restored and the restore is checked. Undo is one click.
4. **Record.** Every step goes into a hash chained audit log, keyed when `REHEARSAL_AUDIT_KEY` is set.

Work that raises no flag at all, such as a reply on one ticket, is applied by a trust policy without asking, so people are interrupted only when money moves or something looks wrong.

## Why Synapse is at the core

- The only path to the app is the MCP server Synapse generated, committed exactly as written. Zero lines of the app changed.
- One generated server, launched twice: once against the private copy, once against live. It reads `ENDPOINT_BASE_URL` on every call, which is what makes a rehearsal possible.
- Rehearsal reads which tools write from the generated code itself. After `synapse build --discover`, it loads the new build and refuses any approval rehearsed with the old one.
- `rehearsal init` writes the whole config for any Synapse built app. A second, unrelated app (a physio clinic) is protected with no Rehearsal code that knows about it.
- It works with your own agent too: put Rehearsal where the Synapse server was in Claude Desktop, Cursor or Claude Code.

## Results

- **7 of 7** attacks in our gallery flagged by Rehearsal; Prompt Guard caught **0 of 7**.
- **12 of 12** story tickets handled exactly as the refund policy says by a free `gpt-oss-120b` model working through Rehearsal.
- **208 tests** that start real app processes and real generated MCP servers, passing on a fresh clone.
- About 4 seconds to start a private copy, 30 ms per tool call on it, and 0 model calls added by Rehearsal.

## Try it

In the browser: open https://rehearsal-ai-app.web.app/try and pick "Watch Rehearsal catch an attack".

On your machine (Python 3.12+, Node 20+, a free Groq key for the live agent; recorded runs work without one):

```bash
git clone https://github.com/hamzaishtiaq0318-bit/rehearsal.git && cd rehearsal
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..
python -m rehearsal.server
```

Open the link it prints. Built with Synapse CLI, the MCP Python SDK, FastAPI, SQLite, React and free models. Total cost: nothing.
