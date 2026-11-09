---
name: standup
description: The weekday morning standup for long-running role sessions - each role posts a short status to the council, and the coordinating role turns the posts into one "Today" note with decisions the user can answer. Use when a "Daily standup" or "Compose the Today note" message arrives, when the user asks "what's today?", "today's note" or "standup", or types /standup.
---

# Daily standup

A scheduled job sends two messages each weekday morning:
- **"Daily standup <date>"** goes to every role at the start;
- **"Compose the Today note <date>"** goes to the coordinating role (the one the machine's role rules name) about 15
  minutes later.

## 1. Every role: post your status
Post once to the council (agent-mail `post`), at most 5 lines, in this order. Leave out a line that would be empty:

```
standup <date> · <your role>
done: <finished since the last standup, with links or paths>
today: <what you will do>
blocked on: <role or user, and what>
needs from user: <decision, one plain sentence>
```

- Before posting, check your open decisions (`decide_list(role=<you>)`). Each stale one (open over 3 days or past
  `valid_until`): close it (`withdraw`, or `answer` if he answered elsewhere) or re-ask it (`touch`, then AskUserQuestion
  in your window). Never repeat one silently.

- If nothing has changed since the last standup, post `standup <date> · <your role>: nothing`.
- Facts only, in plain words: no progress chatter and no internal jargon. Each "needs from user" item must make sense
  on its own, because the user may read only that line.
- Don't ask the user anything with AskUserQuestion during the standup; he reads the Today note when he arrives.
- You may start work that needs no go (builders on agreed specs, dry runs, reviews). Merges to main, deploys, runs on
  real equipment, writes to shared data and messages to other people still wait for his own words.
- No council `post` tool in this session yet (it arrives after an agent-mail reconnect or a restart)? Then send the
  same lines to the coordinating role with agent-mail `send`.

## 2. The coordinating role: compose the Today note
On "Compose the Today note", or at the latest 20 minutes after the standup:
1. Read the council since the standup message (`read` with `since`). A role that hasn't posted gets `no post`;
   never guess for it.
2. Settle requests between roles in the council, and start the work that needs no go.
3. Write the note as one JSON file, `<agent-mail dir>/today/<YYYY-MM-DD>.json` (create the folder, mode 700). Use a
   script with `json.dumps`, never hand-built JSON. Write it to a temp file in the same folder and rename it into
   place.
   ```json
   {
     "date": "YYYY-MM-DD",
     "written": "<ISO time with zone>",
     "summary": ["at most 5 lines: top 3 priorities, who does what, decisions for the user"],
     "decisions": ["project-manager-261106-1", "toolsmith-261105-2"],
     "posts": [{"role": "<role>", "post_id": 123, "text": "<the post, verbatim>"}],
     "next_week": ["Fridays only: deadlines, switches, demos"],
     "council_post": 130
   }
   ```
   - `decisions` lists ids from the decisions store (agent-mail `decide_list`), never copies of the text: every open
     decision the note should show, from any role. A new question for the note is opened first with
     `decide_open(asked_in="today")`. Mothership reads the live state, so an answered or withdrawn one drops off by
     itself. `decisions` may be empty, and `next_week` appears only on Fridays.
   - `council_post` is the id of the post from step 4: the summary plus the numbered decisions.
4. Post the summary and the decisions to the council, then put that post's id in `council_post` and rewrite the file
   the same way.

## 3. Answers
- An answer from the page or the council is a council post `from: user` that replies to `council_post` and starts
  with `<date> <decision id>:`. Mothership records the `answer` event for page answers; for a
  council answer typed by hand, the owning role records it (`decide_close(id, "answer", answered_in="council#<post>")`). It is the user's own words: act on it as his go for what the decision covered, and
  write the go-ledger line with `heard_in: council#<post id>`.
- An answer in the coordinating role's own chat counts the same way, with `heard_in` set to that role's window.

## 4. "What's today?"
Print the newest `today/*.json` as readable text: the summary, the open decisions, then the folded role posts. If it
isn't from today, say so.
