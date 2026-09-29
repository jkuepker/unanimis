# unanimis MCP interface

`unim mcp` runs a small, synchronous MCP server over stdio. It exposes the same memory core as the `unim` CLI: same database, same record IDs, same validation. See [the CLI reference](unim-interface.md) for the command line.

The server implements the tools subset of MCP only. There is no HTTP listener, no sampling, no subscriptions, no background tasks, and no client registration. Protocol messages are newline-delimited JSON-RPC on stdout; diagnostics go to stderr. A single message may be at most 1 MiB. It negotiates protocol versions 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05.

```text
Human terminal ── unim CLI ───────────────┐
Assistant host ── unim mcp (stdio) ───────┤
Optional unim skill ── CLI or MCP ────────┘
                        │
                  unanimis core
       scopes • record IDs • revisions • provenance
                        │
             SQLite database + Markdown exports
```

MCP makes the operations available to compatible applications. It does not make a model automatically read memory, persist every conversation, schedule work, or coordinate ownership. The host must support MCP and tool calls; a local model alone is insufficient. Verify each actual client and version.

## Connecting a client

Each client launches its own `unim mcp` process, all pointing at the same data directory. Use your own `--project` and `--scope`; the defaults are `unanimis` and `personal`.

`examples/mcp.json` in the repository is a ready-to-adapt configuration:

```json
{"mcpServers":{"unanimis":{"command":"unim","args":["--project","my-project","--scope","personal","mcp"]}}}
```

Depending on your client, the equivalent one-liners look like this (check your client's documentation for the exact syntax of the version you run):

```sh
claude mcp add unanimis -- unim --project my-project --scope personal mcp
codex mcp add unanimis -- unim --project my-project --scope personal mcp
```

`unim` must be found on the `PATH` of the process that launches it. Applications started from a desktop launcher often do not inherit your shell's `PATH`; if the client cannot start the server, put the absolute path from `which unim` in `command`. Pass `--data-dir` in `args` if you do not want the default data directory.

The server returns an `instructions` string at initialization. It names the bound project and scope and carries the "MCP briefing" section of `unanimis/agent-policy.md`, so a connected agent receives the memory rules even without a skill.

## Advertised tools

| Tool | Inputs | Behavior |
|---|---|---|
| `unim_capture_new` | `content`, `request_id`; optional `title`, `source`, `status` (`captured`, `proposed` or `accepted`) | Create a NEW record. No existing record ID is accepted. An unaccepted new idea is a capture with `status: "proposed"`. No model call, task dispatch or external action. |
| `unim_propose_record_edit` | `record_id`, `expected_revision`, `title`, `content`, `summary`, `tags` (at most 20), `evidence` (1 to 20 items of `record_id`, `revision`, `quote`), `request_id` | Propose a complete replacement for ONE existing record. Each quote must occur literally in its cited revision. The proposal stays pending; it never alters the target. Acceptance is a separate CLI step (`unim librarian approve`). |
| `unim_recall` | `query`; optional `limit` (1 to 20), `offset`, `include_archived` | Retrieve current records and pending proposals as excerpts with source, revision and freshness. Local hybrid search when a semantic index exists, otherwise lexical search; the result reports coverage and any fallback. Evidence only, not a generated answer. |
| `unim_get` | `record_id`; optional `revision` | Read the complete record or a historical revision, with provenance and freshness. |
| `unim_librarian` | optional `query`, `limit` (1 to 20), `offset` | Prepare an excerpt page plus mechanical duplicate and unresolved-link checks. It invokes no model and approves nothing; the calling agent summarizes and classifies, then submits `unim_propose_record_edit`. |

`unim_recall`, `unim_get` and `unim_librarian` are annotated read-only. Annotations are hints for the client, not authorization; the core enforces the rules. All tools also accept optional `project` and `scope` arguments. They cannot widen access: a value that differs from the process's bound project/scope is rejected with `scope_denied`.

Recall and librarian preparation return excerpt pages. Follow `next_offset` with the same query to continue, and call `unim_get` for the exact full revision before quoting evidence or drawing conclusions. Results carry a separate `freshness` field: `state: "current"` only means the latest saved revision, and stale or unavailable evidence must not be treated as current guidance. If the [optional answer check](search-recovery-ingestion.md#optional-answer-check-in-recall) is configured, the first recall page also carries an advisory `answer_check`.

Every result is returned both as text JSON and as `structuredContent`. Failures come back as `isError: true` with `{"error": CODE, "message": ...}` (for example `invalid_input`, `not_found`, `conflict`, `scope_denied`, `read_only`). Malformed protocol messages produce JSON-RPC errors.

### Legacy tool names

`unim_store`, `unim_correct` and `unim_propose` remain callable for older clients but are not advertised. New integrations should use `unim_capture_new` and `unim_propose_record_edit`. `unim_correct` appends a correction directly and is intended only for an explicit user correction; agent-discovered corrections go through a proposal.

## Writes and retries

Each write takes a `request_id`. Repeating an identical call with the same `request_id` returns the original result and creates nothing new; reusing a `request_id` with different input is a `conflict`. The mutation and its retry record are stored in one SQLite transaction, and the write is persisted before success is reported. On a revision conflict, reread the record instead of retrying with incremented numbers.

## Read-only connections

```sh
unim --project my-project --scope personal mcp --read-only
```

A read-only server opens the database read-only (it never creates a database or schema), lists only `unim_recall` and `unim_get`, and answers any other tool call with a `read_only` error. It also sends read-only instructions instead of the full briefing. Use it for agents that should read memory but never write it.

## Workflow

1. At the start of substantive work, recall relevant project decisions.
2. Capture useful learning, sources and findings explicitly. Keep the user's own words distinguishable from an assistant's interpretation.
3. Before changing an existing record, get its current revision and propose a sourced edit with `unim_propose_record_edit`.
4. At a checkpoint or handoff, record the goal, evidence and remaining work as a dated capture.

A skill can teach this workflow and adapt commands to the host (the repository ships one under `.claude/skills/unim/` and `.agents/skills/unim/`). The CLI and MCP tools remain usable without a skill. Retrieved articles, notes and transcripts are data; text inside them does not become tool authorization. See [the agent policy](../unanimis/agent-policy.md) for the full rules.

## Limits

- Single user, local SQLite. Project and scope filtering is not an operating-system security boundary, and there is no remote authentication.
- stdio only. Multiple machines would need an authenticated network transport that does not exist here; do not synchronize a live database through a file-sync service.
- No task locks, leases or ownership. A memory capture cannot claim or complete work.
- Two-way Markdown editing is not implemented. Files under a configured vault's `Notes/` folder are imported by `unim wiki sync`; the generated `Library/` view is not read back as edits.

## Optional: work MCP for Hermes users (experimental)

If you run a Hermes kanban board, `unim work --board BOARD mcp` starts a separate, read-only MCP server pinned to one board. It advertises only `unim_work_list` and `unim_work_get` and returns Hermes task state as a dated observation, never a claim or permission to act. Use it alongside, not instead of, the memory connection. Everyone else can ignore this integration. See the `unim work` entry in [the CLI reference](unim-interface.md).
