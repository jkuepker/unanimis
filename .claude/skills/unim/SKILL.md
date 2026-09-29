---
name: unim
description: Capture and recall unanimis knowledge, record consequential decisions and findings, preserve handoffs, and prepare cited librarian proposals. Use for explicit memory requests or substantive work in a project that uses unanimis shared memory; do not archive every turn or unrelated personal material.
---

# unim

Read [the shared agent policy](../../../unanimis/agent-policy.md) before using memory. The link resolves from this skill directory to the policy in the repository.

Use the `unim` command from your `PATH` (`python -m unanimis` is equivalent). The default collection is project `unanimis`, scope `personal`; pass your own `--project` and `--scope` before the subcommand, or rely on the binding of the configured MCP connection. Preserve each note's originating project in its source or attribution. A narrower configured connection takes precedence.

Prefer the configured unanimis MCP tools when available. Otherwise use the `unim` CLI; both access the same records. Do not register new servers or claim tools are available solely because this skill exists.

| Intent | MCP | CLI |
|---|---|---|
| Save original text | `unim_capture_new` with `content`, `request_id`, optional `title`/`source` | `unim store TEXT --request-id ID` |
| Retrieve evidence | `unim_recall` with `query` and bounded `limit`; `unim_get` with `record_id`, optional `revision` | `unim recall QUERY`; `unim get ID` |
| Apply an explicitly authorized correction | Use the CLI for a direct user correction outside the proposal workflow | `unim correct ID --expected-revision N --file DRAFT --reason REASON --request-id ID` |
| Prepare librarian work | `unim_librarian` with optional `query`, `limit`, `offset` | `unim --json librarian prepare QUERY` |
| Submit an editorial draft | `unim_propose_record_edit` with target ID/revision, title, content, summary, tags, evidence, request ID | `unim librarian propose DRAFT.json` |

Recall and librarian preparation return excerpts. Follow `next_offset` using the same query; call `unim_get` for the exact full revision before drawing conclusions or quoting evidence. CLI pagination uses `--limit N --offset N`. Acceptance and evidence verification are separate: an accepted decision or edit does not mean unanimis reproduced a measurement.

For a proposal, each evidence item is `{ "record_id": "...", "revision": 1, "quote": "literal source text" }`. Read the actual source revision before quoting it. The body must be the complete proposed replacement. Existence checks do not establish logical support. Drafts remain pending until accepted; do not use correction or direct file/database writes to evade approval.

With shell access, quote literal text safely. For multiline or untrusted captures, prefer structured MCP arguments or a temporary UTF-8 file captured with `unim import /absolute/path/note.md`; do not interpolate note text into executable shell syntax. Use `--json` before the subcommand for structured CLI results. Keep the returned record ID/revision and surface write failures.

When invoked as `/unim store ...`, `$unim`, or a host's named skill, interpret the user's requested operation and call the real tools. Slash/skill syntax is not a shell executable. Do not approve a pending draft merely because the user viewed it.

New unaccepted ideas use `unim_capture_new` with `status: "proposed"`; no target ID exists. An editorial proposal edits a real existing record and requires its ID/revision and source evidence. Accept a user-approved pending draft with `unim librarian approve ID`; do not substitute direct correction.

When an Obsidian vault has been configured with `unim wiki migrate`, personal Markdown lives in its `Notes/` folder. `unim wiki sync` imports new/changed Notes files, bridges inbox drops from the source directory used at migration, and refreshes the generated Library. `unim recall QUERY --include-archived` includes retained raw captures; ordinary recall excludes them. For multiline CLI capture use `unim store --file PATH`, or `--file -` for stdin.
