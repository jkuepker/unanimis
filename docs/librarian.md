# Librarian operations

The on-demand librarian uses a model you configure to summarize and classify current notes, identify evidence-backed connections, and suggest cleanup. It saves separate **proposed AI analysis** records. Original notes remain unchanged. Reports are searchable with ordinary recall, and when an Obsidian vault is configured (see [the CLI reference](unim-interface.md#vault-and-notes)) they are listed under **Home → Review → Librarian reports**.

The librarian only runs when you ask. Nothing schedules it, and capture, recall and `wiki sync` never depend on a model being available.

## Normal workflow

```sh
unim librarian run "language learning" --limit 3
unim librarian reports
unim get REPORT_ID
```

Omit the query to process up to three eligible notes in creation order. Use `--record RECORD_ID` for one specific source. The maximum batch is ten. Unchanged sources with a current report are skipped without a model call; new revisions and changed cited evidence become eligible again. Derived reviews and archives are excluded from automatic selection. A failed item remains eligible and is listed explicitly; use a query or record selection to work around it while preserving the failure for review.

The model receives the full selected note, up to 24,000 characters, plus at most three lexically related full records of up to 8,000 characters each. Oversized sources are reported instead of silently truncated. Use `--related-limit 0` for a source-only pass, or 1–3 to bound related context. This is useful when relationship extraction repeatedly fails citation validation. Related retrieval is bounded lexical matching, not a complete semantic search of the vault. No source URLs or attachments are fetched. Image understanding is not included.

Categories include learning, article, decision, finding, issue, handoff, personal, and other. Tags and categories are AI suggestions stored with the report, not changes to a human note's frontmatter. Notes may remain informal.

## Draft a selected edit

```sh
unim librarian draft REPORT_ID
unim librarian show PROPOSAL_ID
```

Drafting makes one additional model call and submits a full replacement to the pending proposal queue. It does not approve the draft. Review the complete diff, especially personal wording, frontmatter, attachments and omitted details. The model may propose only classification metadata when no textual rewrite is justified.

Only after explicit user acceptance:

```sh
unim librarian approve PROPOSAL_ID
```

A repeated draft command returns its existing proposal. Approval checks the target and cited revisions again, and changed evidence blocks approval. Human files under a vault's `Notes/` remain user-controlled: approving a database edit does not rewrite the file, and a competing later file edit is surfaced by `wiki sync` for reconciliation. Prefer separate reports when human notes need only summarization. Approved editorial revisions are labelled as accepted editorial revisions, with evidence verification kept separate.

## Audit a report

```sh
unim librarian check REPORT_ID
```

This saves a separate advisory review that compares each summary and connection claim in a report with its source revisions. It calls the model, is not a human review, and never approves or edits anything. See [search, recovery and ingestion](search-recovery-ingestion.md#audit-a-librarian-report).

## Model configuration and limits

Point the librarian at an OpenAI-compatible chat-completions server you run or trust:

```sh
unim librarian configure --base-url http://localhost:8000/v1 --model YOUR_MODEL_NAME
```

The setting is stored as `librarian.json` in the data directory. The URL must use `http` or `https`, end in `/v1`, and carry no credentials, query or fragment. Configuration is explicit and content cannot choose a destination.

**What is sent, and where.** `run` sends the selected note and related records described above, `draft` sends the target, its report and the cited source records, and `check` sends the report and up to 48,000 characters of cited sources. All of it goes to the configured `base_url` and nowhere else. Choose an endpoint only when the selected content is authorized for that destination. If you point it at a hosted service, your note text is sent to that service.

The adapter targets servers that need no API key: it sends no `Authorization` header and does not read credentials from the environment. It does not follow redirects and does not use environment proxies. Each call is a chat-completions request with temperature 0, `response_format` set to `json_object`, `chat_template_kwargs` set to `{"enable_thinking": false}` and `max_tokens` 4096, with a 90-second timeout per source. These extra fields are always sent, so pick a server that accepts (or ignores) them; responses over 256 KiB are rejected. Errors produce an explicit failed item and a nonzero CLI exit code; there are no hidden automatic retries.

Strict validation checks response structure, allowed categories, bounded lists, source identities and revisions, and literal quotes. Connections require citations from both records. These checks establish provenance and quote existence, **not semantic truth**. Model summaries, classifications and relationship judgments still require human review. Sources can contain adversarial instructions; the model receives them as data and has no tools or execution channel.

Analysis is persisted atomically together with a check that the cited revisions are still current, and concurrent identical runs converge on one report. Historical reports remain readable and become visibly stale (in `unim librarian reports` and in the generated Obsidian view) when cited revisions change.

## Working without the local model

Agents connected through `unim mcp` do not need a model on your side. `unim_librarian` (or `unim librarian prepare`) returns excerpt pages and mechanical duplicate and link checks, the agent summarizes and classifies, and it submits a cited draft with `unim_propose_record_edit`. That draft is a pending proposal exactly like a model-generated one. See [the MCP interface](mcp-interface.md).
