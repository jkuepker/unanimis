# Search, recovery, source ingestion, and quality checks

## Optional dependencies

Capture, storage, lexical recall, the librarian and backups use only the Python standard library. Two features need extra packages:

```sh
pip install ".[semantic]"       # local embeddings for hybrid recall (fastembed)
pip install ".[pdf]"            # PDF text extraction (pypdf)
pip install ".[semantic,pdf]"   # both
```

Run these from the repository root. Without `semantic`, `unim search index` fails with a `dependency_missing` error and recall stays lexical. Without `pdf`, ingesting a PDF fails with the same error code; other sources are unaffected. The data directory holds a live SQLite database, so do not place it in a folder replicated by a file-sync service. Restart long-running MCP servers after changing the code or the index so they load the new behavior.

## Better recall

```sh
unim recall "which database did we choose for billing"
unim recall "which database did we choose for billing" --mode lexical
unim search index
```

Without an index, recall uses SQLite full-text ranking over current revisions. After `unim search index`, normal recall combines that lexical ranking with local embeddings using reciprocal-rank fusion. The authoritative records, revision history, source citations and freshness checks always stay in the main database, and search returns evidence, not an invented answer.

- **Model and network.** The embedding model is `sentence-transformers/all-MiniLM-L6-v2`, run locally through fastembed. It is downloaded from Hugging Face the first time you run `unim search index`. Recall queries load it from the local cache only, in `models/fastembed` inside the data directory, and never download it. Once the model is cached, record text and queries stay on your machine.
- **The index is rebuildable.** It lives in `semantic.sqlite3` in the data directory. Records are split into 1,400-character chunks with 200 characters of overlap; at most 50 vector candidates are fused, and similarity is a linear cosine scan (no vector server). Changed or deleted source revisions are excluded from vector results immediately, while current lexical matches remain available. New or changed records need another `unim search index` to gain semantic coverage; the index is rebuilt in full each time, and nothing rebuilds it in the background.
- **Coverage and fallback.** Results report whether the index covers every eligible record. In the default `--mode auto`, recall falls back explicitly to lexical search (with a warning) if embeddings are unavailable. `--mode hybrid` turns an unavailable semantic index into an error instead of a fallback, and `--mode lexical` never uses it.

### Source filenames in hybrid ranking

Hybrid recall also uses exact topic words from the names of local source files (`.md`, `.txt`, `.pdf`, `.html`, `.htm`) recorded as absolute paths in a record's source. This helps when a note has an unhelpful first-line title but a useful filename. Filenames are treated as provenance labels only, never opened or executed, and no note content or title is rewritten. The vote is weighted by the fraction of meaningful query words the filename covers, so collection names and question stopwords alone earn no boost. It applies only in hybrid mode, and needs no index rebuild. JSON search metadata reports `source_filename_matches`. Matching is on exact words: it does not correct spelling or stem words.

## Optional answer check in recall

Recall can ask a local decision model whether any of its top records actually contains the specific information the query asks for. This is off unless you set `UNANIMIS_ANSWER_CHECK_URL`.

```sh
UNANIMIS_ANSWER_CHECK_URL=http://127.0.0.1:8019 unim recall "how many users does the service have"
```

- **Endpoint.** unanimis does not ship a model for this. It expects a local server that answers `POST /v1/systemone` with a yes/no ("noul") question about a piece of text and returns a probability between 0 and 1, an interface that the third-party [Kev](https://github.com/jaredpalmer/kev) project serves (for example `python -m kev.serve --run jaredpalmer/kev-9b --port 8019`; see that project for how to install and run it).
- **Loopback only.** Record text is sent only to `127.0.0.1`, `localhost` or `::1`. A URL pointing anywhere else is refused before anything is sent, and the check reports `unavailable` with that reason.
- **What is asked.** Each of the top records (its title and first 4,000 characters) gets one question: does this document contain the specific information the query asks for? The check applies to the first page of results only.
- **Result.** The recall result carries `answer_check` with a `status` (`answer_likely` when the highest probability reaches the threshold, `answer_unlikely` when it does not, `not_checked` when there were no matches, or `unavailable`), the highest `probability`, the `threshold`, `best_record_id` and every record and revision checked. The CLI prints one line. The `unim_recall` MCP tool description tells agents that it is advisory.
- **Never blocks recall.** A failure, timeout or refusal is reported as `unavailable` and recall still returns its records.
- **Settings.** `UNANIMIS_ANSWER_CHECK_THRESHOLD` (default 0.5), `UNANIMIS_ANSWER_CHECK_TOP` (records checked, default 5) and `UNANIMIS_ANSWER_CHECK_TIMEOUT` (seconds for the whole check, default 10). Environment variables are per process: an MCP server needs the variable in its own configuration (an `env` block in the client's server entry) and a reconnect.

It is a model estimate, not verification. `answer_unlikely` suggests memory lacks the answer, but a record that answers a yes/no question with "no" can score low, so read the records before concluding anything. The check adds one model request per checked record to every recall.

## Verified backup and recovery

```sh
unim backup create /path/to/new-backup.zip
unim backup verify /path/to/new-backup.zip
unim backup restore /path/to/new-backup.zip /path/to/new-instance
/path/to/new-instance/unim recall "a remembered topic" --mode lexical
```

Backups are administrative snapshots of the **entire database and the bound vault** (when one is configured), not a scope-filtered export. They include SQLite history, pending and accepted proposals, idempotency records, immutable Markdown exports, human Notes, attachments, preserved imported sources and the data and vault configuration. SQLite's backup API produces a consistent database copy; if source files or the database change while the backup is being made, it aborts without publishing. Every archived file has a checked SHA-256 digest. The destination must be a new file outside the data and vault directories; existing backups are never overwritten. The archive also carries a copy of the package's Python files and the agent policy.

Restore requires a new destination and refuses existing files, symlinks, unsafe archive paths, corrupted manifests and checksum mismatches. It verifies database integrity and foreign keys, rebinds the readable vault to the restored `vault/` directory, and disconnects the old legacy inbox so a test restore cannot drain the original source directory. The restored layout is `new-instance/data`, `new-instance/vault` and `new-instance/app`, plus a small launcher script, `new-instance/unim`, that runs the archived copy of the code against the restored data. The launcher uses the Python interpreter that runs it, and it needs no installation. You can equally point an installed `unim` at the restored data with `unim --data-dir /path/to/new-instance/data ...` (or `UNIM_DATA_DIR`). Historical provenance keeps its original source locations.

Model caches and the semantic index are omitted because they are rebuildable; run `unim --data-dir /path/to/new-instance/data search index` to recreate the index. Optional packages, model weights and any agent-host configuration are also excluded, so install the extras you use and deliberately reconfigure your MCP connections (which still point at the original data directory) before switching to a restored instance. Backups are local ZIP files, not encrypted or automatically scheduled, with a 2 GiB verification and restore bound. Their checksums detect corruption, not the trustworthiness of an unknown backup producer, so restore only backups you made or trust. Keep a copy on separate storage to protect against loss of the machine.

## Capture an article, PDF, or text file

```sh
unim ingest https://example.org/article
unim ingest /path/to/paper.pdf
unim ingest /path/to/note.md
unim search index
```

`unim ingest` accepts an `http(s)` URL or a local file path. It preserves the original bytes under `sources/` in the data directory (named by their SHA-256), records the digest, source location and extraction details, and captures the readable text as a source record. No model is called and nothing needs approval. Repeating an unchanged import returns the same record; changed source bytes create a new capture instead of silently replacing the earlier observation.

- **Text and Markdown** are captured verbatim as UTF-8. Other encodings and binary files are not supported.
- **HTML** is reduced to the article or main text, excluding scripts, styles, navigation, headers, footers and forms.
- **PDF** extraction needs the `pdf` extra. Page boundaries are kept as `## Page N` headings and empty pages are reported. Encrypted PDFs are refused, and a PDF with no extractable text fails with `ocr_required`: there is no OCR, and images and visual layout are not interpreted.
- Imported URLs or document text never authorize commands or further downloads. A source's claims stay source-reported.

**Fetching a URL** is the only network access ingestion performs, and it contacts only the URL you supply (and any redirect targets, below).

- Only `http` and `https`, with no credentials embedded in the URL. Cleartext `http` is allowed.
- The host is resolved first and the request is refused unless every resolved address is a public, globally routable one. Loopback, private-network and link-local destinations (including `localhost`, and typically anything on your LAN or intranet) cannot be ingested by URL. Download such a file yourself and ingest the local path.
- The connection goes to the address that was checked. HTTPS certificates are validated with Python's default trust store.
- Up to three redirects are followed, and each redirect target is validated the same way. Environment proxy settings are ignored.
- The request is a plain `GET` with a `unanimis/0.1 source capture` user agent. No cookies or credentials are sent, and only an HTTP 200 response is accepted. Each connection has a 30-second timeout.
- Limits: 25 MiB for the original (local files too), 300 PDF pages, and 190,000 extracted characters. Split larger sources first.
- Authenticated and paywalled content is not supported.

## Audit a librarian report

```sh
unim librarian check REPORT_ID
```

The checker compares every summary sentence and connection claim in a [librarian report](librarian.md) against the cited source revisions, looking for overgeneralization, attribution errors, status or tense changes and omitted uncertainty. Each supplied claim must receive exactly one assessment (`supported`, `overgeneralized`, `contradicted`, `unsupported` or `unclear`) and select relevant source passages. The code binds each claim and the selected passages to exact report and source citations, so the model does not transcribe long IDs or quotes. Missing claims or invented evidence reject the check. A report needs current evidence, at most twelve claims and a summary under 6,000 characters, and the cited sources must total under 48,000 characters. A saved audit is separate proposed review material with provenance and freshness tracking, and repeating an unchanged check returns the existing result.

The configured model performs this advisory check. It is not an independent human reviewer or a fact-verification service, and it never approves changes or overwrites notes. Splitting summaries into sentences and requiring exact citations improve granularity but do not solve entailment: verdicts can be wrong in either direction. Treat a zero-issue result as "the model raised no issues", never as verified, correct or approved, and keep human editorial review. Broader accuracy on your own material is untested, so evaluate it there before relying on it.
