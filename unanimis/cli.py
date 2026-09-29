import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import uuid
from pathlib import Path

from .core import Core, UnimError


def default_data_dir():
    if os.environ.get("UNIM_DATA_DIR"):
        return os.environ["UNIM_DATA_DIR"]
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(base) / "unanimis")


def parser():
    p = argparse.ArgumentParser(prog="unim", description="unanimis: local shared memory and a reviewable librarian")
    p.add_argument("--data-dir", default=default_data_dir())
    p.add_argument("--project", default="unanimis")
    p.add_argument("--scope", default="personal")
    p.add_argument("--json", action="store_true", help="Print complete structured results")
    sub = p.add_subparsers(dest="command", required=True)
    store = sub.add_parser("store", help="Capture original text")
    store.add_argument("content", nargs="?")
    store.add_argument("--file", help="UTF-8 file, or - for stdin")
    store.add_argument("--status", choices=["captured", "proposed", "accepted"], default="captured")
    store.add_argument("--agent")
    store.add_argument("--origin")
    store.add_argument("--correlation-id")
    store.add_argument("--title")
    store.add_argument("--source")
    store.add_argument("--request-id")
    recall = sub.add_parser("recall", help="Retrieve cited records; no generated answer")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=5)
    recall.add_argument("--mode", choices=["auto", "lexical", "hybrid"], default="auto")
    recall.add_argument("--offset", type=int, default=0)
    recall.add_argument("--include-archived", action="store_true")
    get = sub.add_parser("get")
    get.add_argument("record_id")
    get.add_argument("--revision", type=int)
    correct = sub.add_parser("correct", help="Explicitly append an authorized correction")
    correct.add_argument("record_id")
    correct.add_argument("--expected-revision", required=True, type=int)
    correct.add_argument("--file", required=True)
    correct.add_argument("--reason", required=True)
    correct.add_argument("--request-id")
    imp = sub.add_parser("import", help="Capture a Markdown file; changed inputs produce a conflict")
    imp.add_argument("file")
    sub.add_parser("status")
    sub.add_parser("export", help="Recreate missing revision exports; preserve edited files")
    mcp = sub.add_parser("mcp", help="Run a local stdio MCP server")
    mcp.add_argument("--read-only", action="store_true", help="Enforce recall/get only with a read-only database connection")
    ingest = sub.add_parser("ingest", help="Capture a webpage, PDF or text file with its original bytes")
    ingest.add_argument("source")
    ingest.add_argument("--title")
    backup = sub.add_parser("backup", help="Create, verify or restore a complete local snapshot")
    backup_actions = backup.add_subparsers(dest="action", required=True)
    backup_create = backup_actions.add_parser("create")
    backup_create.add_argument("destination")
    backup_verify = backup_actions.add_parser("verify")
    backup_verify.add_argument("bundle")
    backup_restore = backup_actions.add_parser("restore")
    backup_restore.add_argument("bundle")
    backup_restore.add_argument("destination")
    search = sub.add_parser("search", help="Maintain local semantic retrieval")
    search_actions = search.add_subparsers(dest="action", required=True)
    search_actions.add_parser("index", help="Build or rebuild the local embedding index")
    librarian = sub.add_parser("librarian", help="Prepare, curate, draft, and review knowledge")
    actions = librarian.add_subparsers(dest="action", required=True)
    configure = actions.add_parser("configure", help="Choose an authorized model endpoint; does not schedule runs")
    configure.add_argument("--base-url", required=True)
    configure.add_argument("--model", required=True)
    run = actions.add_parser("run", help="Save proposed summaries for unprocessed current sources")
    run.add_argument("query", nargs="?")
    run.add_argument("--limit", type=int, default=3)
    run.add_argument("--record", dest="record_id")
    run.add_argument("--related-limit", type=int, default=3, help="Related records sent to model (0–3); 0 analyzes only the selected source")
    actions.add_parser("reports", help="List analysis reports and stale evidence")
    check = actions.add_parser("check", help="Audit report claims against sources; saves advisory findings only")
    check.add_argument("report_id")
    draft = actions.add_parser("draft", help="Generate a pending full edit from a selected analysis report")
    draft.add_argument("report_id")
    prepare = actions.add_parser("prepare")
    prepare.add_argument("query", nargs="?")
    prepare.add_argument("--limit", type=int, default=5)
    prepare.add_argument("--offset", type=int, default=0)
    propose = actions.add_parser("propose")
    propose.add_argument("file", help="JSON draft with citations, matching unim_propose arguments")
    actions.add_parser("list")
    approve = actions.add_parser("approve", help="Accept a reviewed draft as a new revision")
    approve.add_argument("proposal_id")
    review = actions.add_parser("show", help="Display a proposal and a unified diff")
    review.add_argument("proposal_id")
    wiki = sub.add_parser("wiki", help="Plan/import a legacy vault and maintain its Obsidian view")
    wa = wiki.add_subparsers(dest="action", required=True)
    wp = wa.add_parser("plan")
    wp.add_argument("--source", required=True)
    wm = wa.add_parser("migrate")
    wm.add_argument("--plan", required=True)
    wm.add_argument("--vault", required=True)
    wa.add_parser("sync")
    wa.add_parser("refresh")
    wa.add_parser("status")
    work = sub.add_parser("work", help="Read live Hermes work status and capture linked checkpoints")
    work.add_argument("--hermes-root", default=str(Path.home() / ".hermes"))
    work.add_argument("--board", help="Explicit board slug; the Hermes current pointer is never followed")
    ws = work.add_subparsers(dest="action", required=True)
    ws.add_parser("boards")
    wl = ws.add_parser("list")
    wl.add_argument("--status")
    wl.add_argument("--assignee")
    wg = ws.add_parser("show")
    wg.add_argument("task_id")
    for action in (wl, wg):
        action.add_argument("--limit", type=int, default=20)
        action.add_argument("--offset", type=int, default=0)
    wc = ws.add_parser("checkpoint", help="Save handoff context; does not claim, transfer or complete work")
    wc.add_argument("task_id")
    wc.add_argument("--file", required=True)
    wc.add_argument("--agent", required=True)
    wc.add_argument("--run-id", required=True, type=int, help="Observed current run ID, or 0 if no run")
    wc.add_argument("--request-id", required=True)
    ws.add_parser("mcp", help="Separate read-only work MCP connection, pinned to --board")
    return p


def execute(args, core):
    cmd = args.command
    if cmd == "work":
        from .work import Board, boards
        if args.action == "boards": return boards(args.hermes_root)
        if not args.board: raise UnimError("invalid_input", "Select --board explicitly before the work action")
        view = Board(args.hermes_root, args.board, core)
        if args.action == "list": return view.work_list(args.status, args.assignee, args.limit, args.offset)
        if args.action == "show": return view.work_get(args.task_id, args.limit, args.offset)
        if args.action == "checkpoint":
            content = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            return view.checkpoint(args.task_id, content, args.agent, args.request_id, args.run_id)
    if cmd == "store":
        if (args.content is None) == (args.file is None):
            raise UnimError("invalid_input", "Provide either content or --file, not both")
        content = args.content if args.file is None else (sys.stdin.read() if args.file == "-" else Path(args.file).read_bytes().decode("utf-8"))
        metadata = {"status": args.status}
        for name in ("agent", "origin", "correlation_id"):
            value = getattr(args, name)
            if value is not None: metadata[name] = value
        return core.store(content, args.request_id or str(uuid.uuid4()), args.title, args.source, metadata=metadata)
    if cmd == "recall":
        return core.recall(args.query, args.limit, args.offset, args.include_archived, args.mode)
    if cmd == "get":
        return core.get(args.record_id, args.revision)
    if cmd == "correct":
        return core.correct(args.record_id, args.expected_revision, Path(args.file).read_text(), args.reason, args.request_id or str(uuid.uuid4()))
    if cmd == "import":
        return core.import_file(args.file)
    if cmd == "status":
        return dict(project=core.project, scope=core.scope, data_dir=str(core.data_dir), records=len(core.list_records()),
                    pending_proposals=len(core.list_proposals()["proposals"]), obsidian_plugin="not implemented",
                    security="local single-user; project/scope filtering is not an OS-user security boundary")
    if cmd == "export":
        return core.export_all()
    if cmd == "wiki":
        from . import wiki
        if args.action == "plan": return wiki.plan(args.source)
        if args.action == "migrate": return wiki.migrate(core, json.loads(Path(args.plan).read_text()), args.vault)
        if args.action == "sync": return wiki.sync(core)
        if args.action == "refresh": return wiki.refresh(core)
        if args.action == "status":
            state = wiki.config(core)
            return {k: state.get(k) for k in ("vault", "legacy_source", "project", "scope", "refreshed_at", "conflicts")}
    if cmd == "ingest":
        from .ingest import ingest
        return ingest(core, args.source, args.title)
    if cmd == "backup":
        from . import backup
        if args.action == "create": return backup.create(core, args.destination)
        if args.action == "verify": return backup.verify(args.bundle)
        return backup.restore(args.bundle, args.destination)
    if cmd == "search":
        from .semantic import index
        return index(core)
    if cmd == "librarian":
        if args.action == "check":
            from .quality import check
            return check(core, args.report_id)
        if args.action in ("configure", "run", "reports", "draft"):
            from . import librarian
            if args.action == "configure": return librarian.configure(core, args.base_url, args.model)
            if args.action == "run":
                if args.query and args.record_id: raise UnimError("invalid_input", "Choose query or --record, not both")
                return librarian.run(core, args.query, args.limit, args.record_id, related_limit=args.related_limit)
            if args.action == "reports": return librarian.reports(core)
            return librarian.draft(core, args.report_id)
        if args.action == "prepare":
            return core.librarian(args.query, args.limit, args.offset)
        if args.action == "propose":
            from .mcp import CALLABLE_TOOLS, validate
            payload = json.loads(Path(args.file).read_text())
            spec = CALLABLE_TOOLS["unim_propose"]
            validate(payload, spec["inputSchema"])
            core.check_scope(payload.pop("project", None), payload.pop("scope", None))
            return core.propose(**payload)
        if args.action == "list":
            return core.list_proposals()
        if args.action == "approve":
            return core.approve(args.proposal_id)
        if args.action == "show":
            import difflib
            proposal = next((p for p in core.list_proposals()["proposals"] if p["id"] == args.proposal_id), None)
            if not proposal:
                raise UnimError("not_found", "Pending proposal not found")
            current = core.get(proposal["record_id"])
            diff = "".join(difflib.unified_diff(current["content"].splitlines(True), proposal["payload"]["content"].splitlines(True),
                                               fromfile="current", tofile="proposed"))
            return dict(proposal=proposal, diff=diff)
    raise UnimError("invalid_input", "Unknown operation")


def render(result, command_prefix=None):
    command_prefix = command_prefix or "unim"
    if result.get("view_warning"):
        print(result["view_warning"])
    if "content" in result and "record_id" in result:
        print_freshness(result, command_prefix)
        print("%s · revision %s · %s\n%s\n\n%s" % (result["record_id"], result["revision"], result["state"], result["title"], result["content"]))
        source = result.get("source_context", {})
        if source.get("source"):
            print("\nSource (recorded in revision %s): %s" % (source["revision"], source["source"]))
        print("\nProvenance: " + json.dumps(result["provenance"], ensure_ascii=False))
    elif "matches" in result:
        print(result["retrieval"])
        if result.get("search", {}).get("warning"): print(result["search"]["warning"])
        if result.get("search", {}).get("coverage_complete") is False: print("Semantic index is incomplete; current lexical matches are included. Refresh with unim search index.")
        check = result.get("answer_check")
        if check:
            if check.get("probability") is not None:
                print("Answer check: %s (p=%s at threshold %s over the top %s records; advisory, read the records)." % (
                    check["status"], check["probability"], check["threshold"], len(check["checked"])))
            else:
                print("Answer check: %s (%s)." % (check["status"], check["reason"]))
        if not result["matches"]:
            print("No matching records.")
        for r in result["matches"]:
            print("\n%s\n%s · revision %s · %s/%s" % (r["title"], r["record_id"], r["revision"], r["project"], r["scope"]))
            print("Source: " + str(r["provenance"].get("source") or "not recorded"))
            source_revision = r.get("source_context", {}).get("revision")
            if source_revision is not None and source_revision != r["revision"]:
                print("Source recorded in revision %s; current revision is %s." % (source_revision, r["revision"]))
            record_status = r["provenance"].get("metadata", {}).get("status")
            if record_status and record_status != "not recorded":
                print("Record status: " + record_status)
            print("Evidence status: " + r["evidence_verification"])
            print_freshness(r, command_prefix)
            if r["kind"] == "review":
                print("REVIEW MATERIAL: check proposal status before treating as an accepted conclusion.")
            preview = re.sub(r"\A---\n.*?\n---\n", "", r["content"], count=1, flags=re.S).strip()
            matched = r.get("semantic_match", {})
            if matched.get("excerpt") and matched["excerpt"] not in preview:
                preview = "Matched passage (offset %s):\n%s" % (matched.get("start", 0), matched["excerpt"])
            print(preview[:700] + ("…" if len(preview) > 700 else ""))
            for proposal in r["pending_proposals"]:
                print("PENDING proposal %s: %s" % (proposal["id"], proposal["payload"]["summary"]))
            print("Read full evidence: " + command_prefix + " get " + shlex.quote(r["record_id"]))
        if result["next_offset"] is not None:
            print("\nNext page: " + command_prefix + " recall " + shlex.quote(result["query"]) + " --offset " + str(result["next_offset"]))
    elif "readable_path" in result:
        print("Saved %s · revision %s\n%s" % (result["record_id"], result["revision"], result["readable_path"] or "Export pending"))
        if "export_warning" in result:
            print(result["export_warning"] + "\n" + result["recovery"])
    elif "proposals" in result:
        if not result["proposals"]:
            print("No pending librarian proposals.")
        for proposal in result["proposals"]:
            print("PENDING %s\n%s\n%s\nReview: %s librarian show %s\n" % (
                proposal["id"], proposal["payload"]["title"], proposal["payload"]["summary"], command_prefix, shlex.quote(proposal["id"])))
    elif "proposal" in result and "diff" in result:
        proposal = result["proposal"]
        print("PENDING %s\n%s\n%s\nTags: %s\n" % (proposal["id"], proposal["payload"]["title"],
              proposal["payload"]["summary"], ", ".join(proposal["payload"]["tags"])))
        print("Evidence:")
        for citation in proposal["payload"]["evidence"]:
            print("%s revision %s: %s" % (citation["record_id"], citation["revision"], citation["quote"]))
        print("\n" + result["diff"])
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def print_freshness(record, command_prefix):
    freshness = record.get('freshness', {})
    status = freshness.get('status', 'not_assessed')
    if status == 'stale':
        print('STALE EVIDENCE: cited sources have newer revisions. Read those revisions before using this as current guidance.')
    elif status == 'unavailable':
        print('EVIDENCE FRESHNESS UNAVAILABLE: some references could not be checked; do not assume this is current guidance.')
    elif status == 'references_current':
        print('Evidence freshness: direct cited revisions match; claims and live state are not independently verified.')
    else:
        print('Evidence freshness: not assessed; no external revision references to check.')
    changed = [r for r in freshness.get('references', []) if r['status'] != 'references_current']
    for ref in changed[:3]:
        if ref['status'] == 'stale':
            print('  %s: cited revision %s; current revision %s. Read: %s get %s' %
                  (ref['record_id'], ref['cited_revision'], ref['current_revision'], command_prefix, ref['record_id']))
        else:
            print('  %s revision %s: unavailable in this connection.' % (ref['record_id'], ref['cited_revision']))
    if len(changed) > 3 or freshness.get('checks_truncated'):
        print('  Additional references: use JSON get for details; some checks may be incomplete.')
    if freshness.get('incomplete') and status == 'stale':
        print('  Some other references could not be checked.')


def main(argv=None):
    args = parser().parse_args(argv)
    core = None
    try:
        # Verifying or restoring a bundle reads only the bundle; never create a store as a side effect.
        if not (args.command == "backup" and args.action != "create"):
            core = Core(args.data_dir, args.project, args.scope,
                        read_only=(args.command == "mcp" and args.read_only) or (args.command == "work" and args.action != "checkpoint"))
        if args.command == "work" and args.action == "mcp":
            from .work import server
            from .mcp import serve
            if not args.board: raise UnimError("invalid_input", "Work MCP requires --board")
            serve(core, server=server(core, args.hermes_root, args.board))
            return
        if args.command == "mcp":
            from .mcp import serve
            serve(core)
            return
        result = execute(args, core)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "work" and args.action != "checkpoint":
            from .work import render as render_work
            render_work(result)
        else:
            command = ["unim"]
            if core and core.data_dir != Path(default_data_dir()).resolve():
                command += ["--data-dir", str(core.data_dir)]
            if core and core.project != "unanimis":
                command += ["--project", core.project]
            if core and core.scope != "personal":
                command += ["--scope", core.scope]
            render(result, " ".join(shlex.quote(part) for part in command))
        if ((args.command == "wiki" and args.action == "sync") or (args.command == "librarian" and args.action == "run")) and result.get("errors"):
            raise SystemExit(1)
    except (UnimError, OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"error": getattr(error, "code", type(error).__name__), "message": str(error)}), file=sys.stderr)
        raise SystemExit(1)
    finally:
        if core:
            core.close()
