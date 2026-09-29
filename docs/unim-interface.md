# unim command reference

`unim` is the command-line front end to unanimis. It and the [MCP server](mcp-interface.md) (`unim mcp`) use the same core, so they share record identities, persistence and validation. Neither keeps a private copy of your memory.

```text
unim [--data-dir DIR] [--project NAME] [--scope NAME] [--json] COMMAND ...
```

`python -m unanimis` is equivalent to `unim`.

## Global options

| Option | Meaning |
|---|---|
| `--data-dir DIR` | Where the database and exports live. Default: `$UNIM_DATA_DIR`, else `$XDG_DATA_HOME/unanimis`, else `~/.local/share/unanimis`. |
| `--project NAME` | Project the process is bound to. Default `unanimis`. Pass your own. |
| `--scope NAME` | Scope the process is bound to. Default `personal`. Pass your own. |
| `--json` | Print the complete structured result instead of the readable rendering. It goes before the command: `unim --json recall "..."`. |

Every command works on one project and scope at a time; records outside it are not found. This is a filter for keeping collections apart, not a security boundary: anyone who can read the data directory can read everything in it.

Errors are printed to stderr as a JSON object with `error` and `message` and the exit status is 1. `wiki sync` and `librarian run` also exit 1 when they report per-item errors.

## Capture

```sh
unim store "We decided to use PostgreSQL for the billing service."
unim store --file notes.md --title "Billing notes" --source "design review 2026-03-02"
some-command | unim store --file -
unim import path/to/note.md
```

`store` persists the original text before any optional AI interpretation and prints the new record ID and revision. It never calls a model, registers anything, or assigns a task. Capturing a sentence records what was said; it does not make the sentence verified.

- Give the text as an argument, or with `--file PATH` (`-` for standard input). Use a file for multi-line or untrusted text so the shell never interprets it.
- `--title`, `--source` (a provenance label; it is not fetched or executed), `--agent`, `--origin` and `--correlation-id` add context.
- `--status captured|proposed|accepted` labels the record. An unaccepted new idea is `proposed`; an agent should not label its own idea `accepted`.
- `--request-id ID` makes a retry safe: repeating an identical `store` with the same ID returns the original record, and reusing the ID with different text is a conflict. If you omit it, a random ID is used (no retry protection).

`import FILE` captures a UTF-8 Markdown file as a source record. Importing the same path again with unchanged content returns the same record; changed content is reported as a conflict instead of replacing it.

## Recall and read

```sh
unim recall "which database did we choose for billing"
unim recall "billing" --limit 10 --offset 10
unim recall "billing" --mode lexical
unim recall "billing" --include-archived
unim get RECORD_ID
unim get RECORD_ID --revision 2
```

`recall` returns evidence, not an invented answer: matching records as excerpts with ID, revision, source, status, freshness and any pending editorial proposals, followed by a command for the next page. Without a semantic index it uses SQLite full-text search. With one (see `search index`) it fuses lexical and local embedding results. `--mode lexical` forces lexical search, `--mode hybrid` makes an unavailable semantic index an error, and the default `auto` falls back to lexical with a visible warning. Archived raw captures are excluded unless `--include-archived` is given. `--limit` is 1 to 20. See [search, recovery and ingestion](search-recovery-ingestion.md).

`get` prints one complete record or a historical revision, with provenance and an evidence-freshness line. Recall shows excerpts; use `get` before quoting or drawing conclusions.

## Correct and review

```sh
unim correct RECORD_ID --expected-revision 1 --file corrected.md --reason "typo in the date"
unim librarian list
unim librarian show PROPOSAL_ID
unim librarian approve PROPOSAL_ID
```

`correct` appends a new revision from a file and keeps the history. It rejects a stale `--expected-revision`. Use it for an explicit correction by the user. Suggested edits (from an agent or the librarian) are instead submitted as proposals, which stay pending until a person runs `librarian approve`. `librarian list` shows pending proposals, `librarian show` shows one with a unified diff and its cited evidence, and `approve` applies it as a new revision after re-checking that the target and cited revisions are unchanged. See [the librarian](librarian.md).

## Librarian

The librarian summarizes, classifies and links notes with a model you configure. Its subcommands:

| Command | Purpose |
|---|---|
| `librarian configure --base-url URL --model NAME` | Choose the OpenAI-compatible endpoint. Selected record text is sent there. |
| `librarian run [QUERY] [--limit N] [--record ID] [--related-limit 0-3]` | Save proposed analysis reports for unprocessed current notes. Calls the model. |
| `librarian reports` | List reports and whether their cited evidence is stale. |
| `librarian draft REPORT_ID` | Turn a report into a pending edit proposal. Calls the model. |
| `librarian check REPORT_ID` | Save an advisory audit of a report's claims. Calls the model. |
| `librarian prepare [QUERY] [--limit N] [--offset N]` | Print an excerpt page and mechanical duplicate/link checks for an agent to work from. No model call. |
| `librarian propose DRAFT.json` | Submit a cited replacement draft (same fields as `unim_propose_record_edit`; each `evidence` quote must occur in its cited revision). |
| `librarian list`, `show`, `approve` | Review pending proposals, as above. |

## Documents and search index

```sh
unim ingest https://example.org/article
unim ingest path/to/paper.pdf --title "Paper title"
unim search index
```

`ingest` captures a web page, PDF or text file with its original bytes preserved and its extracted text as a source record. A URL is fetched over the network; see [search, recovery and ingestion](search-recovery-ingestion.md) for its limits. `search index` builds the local embedding index; it needs the `semantic` extra and downloads the embedding model the first time.

## Vault and Notes

```sh
unim --json wiki plan --source path/to/existing-notes > plan.json
unim wiki migrate --plan plan.json --vault path/to/new-empty-vault
unim wiki sync
unim wiki refresh
unim wiki status
```

Every revision is always exported as an immutable Markdown file at `DATA_DIR/vault/RECORD_ID/rNNNN.md`, whatever else you configure. The commands above set up and maintain a separate, generated Obsidian view.

- `wiki plan --source DIR` lists the Markdown files in an existing notes directory. It reads `findings/`, `notes/`, `projects/`, `maps/` and `sources/` (curated notes), `inbox/*.md` (loose captures) and `inbox/processed/`, `inbox/quarantine/` (archived captures, excluded from default recall). The directory must contain at least one such file.
- `wiki migrate --plan FILE --vault DIR` imports exactly those files as records and creates the vault. The vault directory must be new or empty and must not contain, or be inside, the source directory. Your source files are only read. The vault gets `Home.md`, a `Notes/` folder for your own Markdown, and generated `Library/`, `Indexes/`, `Review/` and `Archive/` folders. If the source changes between `plan` and `migrate`, the command stops and asks for a new plan. Until you have migrated, `wiki sync` and `wiki status` fail with `not_configured` and `wiki refresh` does nothing.
- `wiki sync` captures new Markdown files in the vault's `Notes/` folder verbatim, appends a revision when a synced file changed, and picks up new `inbox/*.md` drops in the source directory. It then refreshes the view. It is deterministic (no model, no approvals), skips and reports a file whose record was changed elsewhere, and never overwrites your files. Run it by hand or from your own scheduler; unanimis installs none.
- `wiki refresh` regenerates the view without importing anything. Hand-edited files under `Library/` are preserved and reported as conflicts, never overwritten.
- `wiki status` prints the vault, source directory, project, scope, last refresh and conflicts.

## Housekeeping

```sh
unim status
unim export
unim backup create backup.zip
unim backup verify backup.zip
unim backup restore backup.zip new-instance-dir
```

`status` prints the resolved data directory, project, scope and counts. `export` recreates missing revision export files and preserves any you edited. The `backup` commands are described in [search, recovery and ingestion](search-recovery-ingestion.md).

## MCP server

```sh
unim mcp
unim mcp --read-only
```

Runs the stdio MCP server described in [the MCP interface](mcp-interface.md). `--read-only` permits only `unim_recall` and `unim_get` and opens the database read-only.

## Agent skills

The repository includes a `unim` skill (`.claude/skills/unim/SKILL.md` and `.agents/skills/unim/SKILL.md`) that teaches an agent this workflow and calls the CLI or MCP tools. Slash-style invocations such as `/unim store ...` work only in hosts that support skills; the syntax is not a shell command. The skill is guidance and owns no separate memory. The rules it follows are in [the agent policy](../unanimis/agent-policy.md).

## Optional: `unim work` for Hermes users (experimental)

If you use a Hermes kanban board, `unim work` gives read-only access to it and can save a linked memory checkpoint. Everyone else can ignore it.

```sh
unim work boards
unim work --board BOARD list [--status S] [--assignee A]
unim work --board BOARD show TASK_ID
unim work --board BOARD checkpoint TASK_ID --file FILE --agent NAME --run-id N --request-id ID
unim work --board BOARD mcp
```

- `--hermes-root DIR` (default `~/.hermes`) is where the boards are read from, and `--board` is always explicit; the Hermes current-board pointer is never followed.
- `boards`, `list` and `show` only read. Hermes remains the sole owner of task state, claims and transitions, and what you see is a dated observation, not a reservation.
- `checkpoint` is the only write: it saves handoff text as a memory record linked to the task. It does not claim, transfer or complete anything. `--run-id` is the current run ID, or 0 if the task has none, and the command refuses if the run has changed.
- `mcp` starts a separate read-only MCP server (`unim_work_list`, `unim_work_get`) pinned to one board.
