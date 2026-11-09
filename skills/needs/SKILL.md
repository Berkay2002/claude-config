---
name: needs
description: Say in a few lines what this session needs from the user - decisions, approvals, answers, actions - with why and since when, or "nothing". Use when the user types /needs, asks "what do you need from me", "anything waiting on me", "what's blocked on me", or asks for it from all sessions at once (/needs all).
---

# What do you need from me?

## /needs (this session)
Read your open decisions first: agent-mail `decide_list(status="open", role=<you>)` (the one store; questions you asked
with AskUserQuestion are in it automatically). Before listing, close what is no longer true (`decide_close` withdraw, or
`answer` if he answered elsewhere) and open anything missing with `decide_open`. Then list what is waiting on the user,
oldest first, in at most 5 lines, marking stale ones. Use one line each:

`- <what you need> (since <date>): <why it matters, or what it unblocks>`

Count only things the user must do or decide: a go, a choice, a text to approve, a login, a physical action. Leave
out work you can do yourself, and things waiting on other sessions (name those only if the user asked).
Nothing waiting: reply `nothing`. Asked for it in a council post: answer there, same lines.

Then offer to ask the first item now with AskUserQuestion, so the user can answer it on the spot. Each question
stands on its own (one plain sentence of context, the recommended option first). Skip this if the
user only wanted the list.

## /needs all (every session)
Start from `decide_list(status="open")` grouped by role; then ask the roles to confirm or correct their part.
For collecting from several long-running sessions at once:
- **If a council channel exists** (agent-mail `list()` shows one you belong to): post `/needs: reply with your list
  or "nothing"` there. Wait for the replies, then merge them into one list grouped by session.
- **Otherwise:** send each session the same one-line request with SendMessage (agent-mail `send` for Codex
  workers), then merge the replies the same way.

Mark a session that doesn't answer within a few minutes as `no answer`. Never guess for it.
