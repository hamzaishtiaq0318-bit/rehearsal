# Security policy

Rehearsal is a safety layer for AI agents, so security reports are welcome and taken seriously.

## Reporting a problem

Open an issue at https://github.com/hamzaishtiaq0318-bit/rehearsal/issues with the steps to reproduce it. Do not include real customer data, API keys or other secrets in a report. If the problem lets someone apply changes to live data without a person's approval, say so in the title.

## What is in scope

- Anything that lets a change reach live data without the approval Rehearsal asks for, or lets live data differ from what was reviewed.
- Ways for an agent, or text it reads, to approve its own work or learn the approval token.
- Reading tickets, rehearsals or the audit log without the console link.
- Editing the audit log without the chain check noticing.

## What Rehearsal assumes

The threat model, how each threat is handled and tested, and the limits we know about are on the [security page](https://rehearsal-ai-app.web.app/security). In short: the agent can be fooled and the data it reads can be hostile; the console runs on the machine of the person who reviews; and an agent that also has shell access to that machine is outside what any local approval step can contain.
