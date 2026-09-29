import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unanimis.core import Core
from unanimis.mcp import Server, TOOLS, CALLABLE_TOOLS, validate



def message(method, params=None, request_id=1):
    result = dict(jsonrpc="2.0", method=method, params=params or {})
    if request_id is not None:
        result["id"] = request_id
    return result


def initialize():
    return message("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core(self.temp.name)
        self.server = Server(self.core)

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def start(self):
        self.server.dispatch(initialize())
        self.server.dispatch(message("notifications/initialized", request_id=None))

    def test_read_only_mcp_denies_all_write_names_and_preserves_database(self):
        import sqlite3
        record = self.core.store("Read-only fixture", "fixture")
        proposal = self.core.propose(record["record_id"], 1, "Draft", "Read-only fixture draft", "test", [],
                                    [{"record_id":record["record_id"],"revision":1,"quote":"fixture"}], "draft")
        before = list(self.core.db.iterdump())
        reader = Core(self.temp.name, read_only=True)
        try:
            server = Server(reader)
            init = server.dispatch(initialize())["result"]
            self.assertIn("READ-ONLY", init["instructions"])
            server.dispatch(message("notifications/initialized", request_id=None))
            self.assertEqual({t["name"] for t in server.dispatch(message("tools/list"))["result"]["tools"]},
                             {"unim_recall", "unim_get"})
            for name in ("unim_capture_new", "unim_propose_record_edit", "unim_store", "unim_correct", "unim_propose", "unim_librarian"):
                result = server.dispatch(message("tools/call", {"name":name,"arguments":{}}))["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(json.loads(result["content"][0]["text"])["error"], "read_only")
            for name, args in (("unim_get", {"record_id":record["record_id"]}), ("unim_recall", {"query":"fixture"})):
                self.assertFalse(server.dispatch(message("tools/call", {"name":name,"arguments":args}))["result"]["isError"])
            with self.assertRaisesRegex(Exception, "writes are disabled"):
                reader.store("forbidden", "no")
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM records")
            reader.db.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM records")
        finally:
            reader.close()
        self.assertEqual(list(self.core.db.iterdump()), before)

    def test_read_only_cli_missing_store_is_not_created(self):
        missing = Path(self.temp.name)/"missing"
        p = subprocess.run([sys.executable,'-m', 'unanimis',"--data-dir",str(missing),"mcp","--read-only"],
                           input="",text=True,capture_output=True,timeout=15)
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(missing.exists())

    def test_read_only_cli_retrieves_shared_data_and_refuses_a_valid_write(self):
        self.core.store("Read-only process fixture", "fixture")
        before = list(self.core.db.iterdump())
        messages = [initialize(), message("notifications/initialized",request_id=None),
                    message("tools/list",request_id=2),
                    message("tools/call",{"name":"unim_store","arguments":{"content":"must not persist","request_id":"denied"}},3),
                    message("tools/call",{"name":"unim_recall","arguments":{"query":"fixture"}},4)]
        p=subprocess.run([sys.executable,'-m', 'unanimis',"--data-dir",self.temp.name,"mcp","--read-only"],
                         input="".join(json.dumps(m)+"\n" for m in messages),text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr)
        replies=[json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(len(replies[1]["result"]["tools"]),2)
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual(len(replies[3]["result"]["structuredContent"]["matches"]),1)
        self.assertEqual(list(self.core.db.iterdump()),before)

    def test_tools_list_accepts_mcp_metadata_without_relaxing_read_only(self):
        self.server = Server(self.core, read_only=True)
        self.start()
        for params in ({"_meta": {"traceparent":"synthetic"}}, {"cursor":None,"_meta":{}}, {}):
            result=self.server.dispatch(message("tools/list",params))["result"]
            self.assertEqual({t["name"] for t in result["tools"]},{"unim_recall","unim_get"})
        for params in ({"cursor":"unknown"},{"_meta":"invalid"},{"read_only":False}):
            self.assertIn("error",self.server.dispatch(message("tools/list",params)))

    def test_lifecycle_and_tool_schemas(self):
        self.assertIn("error", self.server.dispatch(message("tools/list")))
        self.start()
        tools = self.server.dispatch(message("tools/list"))["result"]["tools"]
        self.assertEqual(len(tools), 5)
        self.assertEqual([t["name"] for t in tools if not t["annotations"]["readOnlyHint"]], ["unim_capture_new", "unim_propose_record_edit"])
        self.assertNotIn("unim_approve", [t["name"] for t in tools])
        self.assertTrue(all(t["inputSchema"]["additionalProperties"] is False for t in tools))

    def test_invalid_arguments_leave_server_operational(self):
        self.start()
        for arguments in (["wrong"], {"query": "x", "limit": True}, {"query": "x", "bad": 1}):
            reply = self.server.dispatch(message("tools/call", {"name": "unim_recall", "arguments": arguments}))
            self.assertIn("error", reply)
        reply = self.server.dispatch(message("tools/call", {"name": "unim_recall", "arguments": {"query": "x", "project": "private-other"}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.server.dispatch(message("ping"))["result"], {})

    def test_notifications_and_batches_do_not_mutate(self):
        self.start()
        self.assertIsNone(self.server.dispatch(message("tools/call", {"name": "unim_store", "arguments": {"content": "x", "request_id": "x"}}, None)))
        self.assertIn("error", self.server.dispatch([message("ping")]))
        self.assertEqual(self.core.list_records(), [])

    def run_client(self, requests):
        lines = [initialize(), message("notifications/initialized", request_id=None)] + requests
        proc = subprocess.run([sys.executable, '-m', 'unanimis', "--data-dir", self.temp.name, "mcp"],
                              input="\n".join(json.dumps(x) for x in lines) + "\n", text=True,
                              capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return [json.loads(line) for line in proc.stdout.splitlines()]

    def test_cli_and_two_independent_mcp_clients_share_records(self):
        cli = subprocess.run([sys.executable, '-m', 'unanimis', "--data-dir", self.temp.name, "--json", "store", "Shared CLI record unanimis", "--request-id", "cli"], capture_output=True, text=True, timeout=15)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        record = json.loads(cli.stdout)
        first = self.run_client([message("tools/call", {"name": "unim_get", "arguments": {"record_id": record["record_id"]}}, 2)])[-1]["result"]
        second = self.run_client([message("tools/call", {"name": "unim_recall", "arguments": {"query": "Shared CLI"}}, 2)])[-1]["result"]
        self.assertEqual(first["structuredContent"]["record_id"], record["record_id"])
        self.assertEqual(second["structuredContent"]["matches"][0]["record_id"], record["record_id"])
        self.assertEqual(json.loads(first["content"][0]["text"]), first["structuredContent"])
        validate(first["structuredContent"], CALLABLE_TOOLS["unim_get"]["outputSchema"])

    def test_mcp_librarian_submission_is_pending(self):
        record = self.core.store("Original evidence.", "source")
        args = dict(record_id=record["record_id"], expected_revision=1, title="Reviewed", content="A narrower conclusion.",
                    summary="A source-linked summary.", tags=["review"], evidence=[dict(record_id=record["record_id"], revision=1, quote="Original evidence.")], request_id="proposal")
        response = self.run_client([message("tools/call", {"name": "unim_librarian", "arguments": {}}, 2),
                                    message("tools/call", {"name": "unim_propose", "arguments": args}, 3)])
        self.assertEqual(response[-1]["result"]["structuredContent"]["status"], "pending")
        self.assertEqual(self.core.get(record["record_id"])["revision"], 1)


    def test_initialize_carries_shared_policy_and_actual_connection_scope(self):
        self.core.project = "learning"
        self.core.scope = "private"
        result = self.server.dispatch(initialize())["result"]
        self.assertIn("project=learning scope=private", result["instructions"])
        self.assertIn("unanimis shared-memory rules", result["instructions"])
        self.assertIn("never claim a task lease", result["instructions"])
        self.assertTrue(result["instructions"].endswith("End of shared-memory briefing."))
        self.assertLess(len(result["instructions"]), 2000)

    def test_mcp_pagination_and_invalid_offset(self):
        ids = [self.core.store("Page evidence " + str(i), str(i))["record_id"] for i in range(3)]
        self.start()
        for name, field in (("unim_librarian", "records"), ("unim_recall", "matches")):
            seen = []
            for offset in range(3):
                result = self.server.dispatch(message("tools/call", {"name": name, "arguments": {"query": "Page", "limit": 1, "offset": offset}}))["result"]
                self.assertFalse(result["isError"])
                page = result["structuredContent"]
                self.assertEqual(page["next_offset"], offset + 1 if offset < 2 else None)
                seen.extend(item["record_id"] for item in page[field])
            self.assertCountEqual(seen, ids)
            result = self.server.dispatch(message("tools/call", {"name": name, "arguments": {"query": "Page", "offset": -1}}))
            self.assertIn("error", result)


    def test_disjoint_capture_and_edit_schemas_prevent_cross_use(self):
        self.start()
        def call(name,args):
            return self.server.dispatch(message("tools/call", {"name":name, "arguments":args}))
        new={"content":"An unaccepted idea", "status":"proposed", "request_id":"idea"}
        idea=call("unim_capture_new",new)["result"]["structuredContent"]
        self.assertEqual(idea["operation"], "capture_created")
        self.assertEqual(self.core.list_proposals()["proposals"], [])
        self.assertIn("error",call("unim_capture_new",dict(new,record_id=idea["record_id"])))
        args={"content":"An edited idea", "title":"Edited", "summary":"Clarify", "tags":[], "request_id":"edit",
              "record_id":idea["record_id"], "expected_revision":1,
              "evidence":[{"record_id":idea["record_id"],"revision":1,"quote":"An unaccepted idea"}]}
        proposal=call("unim_propose_record_edit",args)["result"]["structuredContent"]
        self.assertEqual(proposal["operation"], "edit_proposed")
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.core.get(idea["record_id"])["revision"],1)
        self.assertIn("error",call("unim_propose_record_edit",dict(args,status="accepted")))
        missing=dict(args); del missing["record_id"]
        self.assertIn("error",call("unim_propose_record_edit",missing))
        self.assertTrue(call("unim_propose_record_edit",dict(args,record_id="invented",request_id="fake"))["result"]["isError"])
        self.assertEqual(call("unim_capture_new",new)["result"]["structuredContent"]["record_id"],idea["record_id"])
        self.assertEqual(self.core.approve(proposal["proposal_id"])["revision"],2)


if __name__ == "__main__":
    unittest.main()
