# unanimis

unanimis is local-first shared memory for people and AI coding agents. A command-line tool (`unim`) and a stdio MCP server share one SQLite store of revisioned records with provenance, so a note you save from the terminal is the same record your agents recall through MCP. Every revision is also exported as a plain Markdown file, and an optional generated Obsidian vault gives you a readable view plus a `Notes/` folder for your own writing. Recall returns cited evidence, not an invented answer. An optional on-demand librarian can use a model you point it at to summarize and classify notes, but its output is only ever a proposal that stays pending until a person approves it. There is no server to run and no account; the product name is lowercase, and the CLI and skill are `unim`.

## Install

Requires Python 3.11 or newer on macOS or Linux (it uses `fcntl`, so Windows is not supported). Clone the repository and install from the checkout:

```sh
git clone https://github.com/jkuepker/unanimis.git
cd unanimis
pip install .
```

Or install straight from GitHub without cloning: `pip install "git+https://github.com/jkuepker/unanimis.git"`. That installs the `unim` command (`python -m unanimis` also works) with no third-party dependencies. Use a virtual environment or `pipx install .` if you prefer to keep it off your system Python. Two optional extras add features:

```sh
pip install ".[semantic]"       # local embeddings for hybrid search (fastembed)
pip install ".[pdf]"            # PDF text extraction (pypdf)
pip install ".[semantic,pdf]"   # both
```

By default your data lives in `$UNIM_DATA_DIR`, else `$XDG_DATA_HOME/unanimis`, else `~/.local/share/unanimis`; `--data-dir DIR` overrides it. Records are filed under a project and a scope, defaulting to `--project unanimis --scope personal`. Pass your own (`unim --project my-project --scope personal ...`, before the subcommand) so your notes are not mixed into the defaults. There is no environment variable for them; a shell alias saves typing. The examples below use the defaults.

## Quickstart

Save and recall:

```sh
unim store "We decided to use PostgreSQL for the billing service."
unim recall "which database did we choose for billing"
unim get RECORD_ID
```

`store` prints the new record ID. `recall` returns matching records as excerpts with their source, revision and freshness, never a generated answer. `get` prints one complete record. Corrections append a new revision and keep the old ones (`unim correct`). Add `--json` before the subcommand for structured output. Search is lexical by default; run `unim search index` (needs the `semantic` extra) to add local embeddings.

Obsidian vault and notes (optional). The generated vault is bootstrapped from a directory of existing Markdown notes: it needs at least one `.md` file in a `notes/` (or `findings/`, `projects/`, `maps/`, `sources/`, `inbox/`) subfolder. Your source files are only read.

```sh
mkdir -p ~/notes-source/notes
echo "# Getting started" > ~/notes-source/notes/getting-started.md
unim --json wiki plan --source ~/notes-source > plan.json
unim wiki migrate --plan plan.json --vault ~/unanimis-vault
```

Open `~/unanimis-vault` in Obsidian and start at `Home.md`. Write your own Markdown under `Notes/`, then run `unim wiki sync` to capture new notes and append revisions for changed ones. `Library/`, `Indexes/` and `Review/` are generated; hand edits there are preserved and reported as conflicts, never overwritten. Even without a vault, every revision is exported as Markdown under `DATA_DIR/vault/`.

Librarian (optional). Point it at an OpenAI-compatible server you run (see "What leaves your machine" first):

```sh
unim librarian configure --base-url http://localhost:8000/v1 --model YOUR_MODEL
unim librarian run "a topic" --limit 3
unim librarian reports
unim librarian draft REPORT_ID       # creates a pending edit proposal
unim librarian show PROPOSAL_ID      # review the diff and cited evidence
unim librarian approve PROPOSAL_ID   # only after you have reviewed it
```

Reports and drafts are separate proposed records; nothing changes your notes until you approve. See [the librarian guide](docs/librarian.md).

## Connect Claude Code or Codex via MCP

`unim mcp` runs a local stdio MCP server. It advertises `unim_capture_new`, `unim_propose_record_edit`, `unim_recall`, `unim_get` and `unim_librarian`, and its initialization instructions carry the memory rules from [the agent policy](unanimis/agent-policy.md). New unaccepted ideas are captures with `proposed` status; edits to an existing record are proposals that need citations and a person's approval. `unim mcp --read-only` limits a connection to recall and get.

`examples/mcp.json` is a ready-to-adapt configuration (replace `my-project`):

```json
{"mcpServers":{"unanimis":{"command":"unim","args":["--project","my-project","--scope","personal","mcp"]}}}
```

For Claude Code, `claude mcp add unanimis -- unim --project my-project --scope personal mcp` registers the same server. For Codex, add to `~/.codex/config.toml`:

```toml
[mcp_servers.unanimis]
command = "unim"
args = ["--project", "my-project", "--scope", "personal", "mcp"]
```

Check your client's documentation for the syntax of the version you run. `unim` must be on the `PATH` of the process that starts it; if a desktop client cannot find it, use the absolute path from `which unim`. This repository also ships a `unim` skill (`.claude/skills/unim/` and `.agents/skills/unim/`) and contributor instructions in `CLAUDE.md` and `AGENTS.md`. Copy the skill into your own project if you want agents there to follow the same workflow. Its link to the policy points inside this repository, but the MCP instructions carry the core rules regardless. Details are in [the MCP interface](docs/mcp-interface.md).

## What leaves your machine

unanimis works offline and has no telemetry. These are the only network paths, all opt-in:

1. **Embedding model download (`semantic` extra).** The first `unim search index` makes fastembed download the `sentence-transformers/all-MiniLM-L6-v2` embedding model, from Hugging Face (its ONNX conversion, `qdrant/all-MiniLM-L6-v2-onnx`; fastembed 0.8 falls back to a Google Cloud Storage bucket if that fails). It is cached in the data directory. Recall queries then use the cache only and never download. No note text is sent for this.
2. **Librarian.** `unim librarian run`, `draft` and `check` send selected record text to the `base_url` you set with `unim librarian configure`, over the OpenAI-compatible chat-completions protocol. Nothing is configured by default, so nothing is sent until you do it. If you point it at a hosted service, your notes go to that service; no API key is sent, so it is meant for local or unauthenticated servers. Details on what is sent are in [the librarian guide](docs/librarian.md).
3. **Answer check.** If you set `UNANIMIS_ANSWER_CHECK_URL`, recall sends the text of its top records to that endpoint to get an advisory estimate. Only loopback hosts (`127.0.0.1`, `localhost`, `::1`) are accepted; any other address is refused before anything is sent, proxy settings are ignored, and redirects are refused. It is off by default.
4. **Document ingestion.** `unim ingest URL` fetches the HTML or PDF at a URL you supply (plus any redirects, up to three): HTTP(S) only, public addresses only, nothing but a plain `GET` with no cookies or credentials, at most 25 MiB. Local files are read from disk and never sent anywhere. See [search, recovery and ingestion](docs/search-recovery-ingestion.md).

unanimis sends nothing else. One caveat is outside its control: whatever an agent recalls through MCP or the CLI becomes part of that agent's conversation and goes wherever the agent's own model provider does. Connect only agents you trust with the records they can read, or use `--read-only` and a separate project or scope for sensitive material.

## Limitations

- Single-user, local SQLite. Project and scope only separate collections; they are not a security boundary. There is no remote authentication, no network transport (MCP is stdio only), no per-record sharing controls, and nothing is encrypted at rest.
- No task locks, leases or ownership. A memory record can describe work but cannot claim or complete it.
- macOS and Linux only.
- The data directory holds a live SQLite database; do not put it in a folder replicated by a file-sync service. Use `unim backup create` for copies.
- Lexical search matches words, without stemming or spelling correction. Semantic search needs the optional extra and a one-time model download.
- The Obsidian view is generated one way: it is bootstrapped from an existing notes directory, there is no Obsidian plugin, and edits under `Library/` are never read back as changes.
- The librarian cannot send an API key and its model output can be wrong; quote checks show that text exists, not that a summary is faithful.
- Nothing runs on a schedule. Run `wiki sync` and the librarian yourself, or from your own scheduler.
- Experimental: `unim work` and `unim work ... mcp` are a read-only adapter for a Hermes kanban board (default root `~/.hermes`) that can also save linked memory checkpoints. It is only for Hermes users and can be ignored otherwise. See [the CLI reference](docs/unim-interface.md#optional-unim-work-for-hermes-users-experimental).

## Documentation

- [Command reference](docs/unim-interface.md): every `unim` command
- [MCP interface](docs/mcp-interface.md): tools, read-only mode, connecting a client
- [Librarian operations](docs/librarian.md): the model-assisted review workflow and what it sends
- [Search, recovery and ingestion](docs/search-recovery-ingestion.md): hybrid search, answer check, backup and restore, importing documents, report audits
- [Agent memory policy](unanimis/agent-policy.md): the rules agents follow when capturing and recalling

## Development

```sh
python -m unittest discover -s tests -v
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
