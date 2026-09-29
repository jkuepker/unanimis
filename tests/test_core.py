import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from unanimis.core import Core, UnimError


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.core = Core(self.root)

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def capture(self, content="The product is unanimis.", key="test"):
        return self.core.store(content, key)

    def proposal(self, record, evidence=None):
        return self.core.propose(record["record_id"], 1, "Product name", "The product is unanimis, always lowercase.",
                                 "Clarify lowercase styling.", ["decisions"], evidence or [{"record_id": record["record_id"], "revision": 1,
                                 "quote": "The product is unanimis."}], "proposal-test")

    def test_capture_survives_restart_and_preserves_verbatim_text(self):
        content = '  My note: "hello"\n你好\nDo not execute $(touch /tmp/should-not-exist).'
        record = self.capture(content)
        self.core.close()
        self.core = Core(self.root)
        self.assertEqual(self.core.get(record["record_id"])["content"], content)
        self.assertIn(content, Path(record["readable_path"]).read_text())

    def test_retry_is_durable_and_conflicting_reuse_rejected(self):
        first = self.capture()
        self.assertEqual(first, self.capture())
        with self.assertRaisesRegex(UnimError, "different input"):
            self.capture("Changed text")
        self.assertEqual(len(self.core.list_records()), 1)

    def test_concurrent_retry_creates_one_record(self):
        def store(_):
            c = Core(self.root)
            try:
                return c.store("one concurrent capture", "same-key")["record_id"]
            finally:
                c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(store, range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.core.list_records()), 1)

    def test_concurrent_corrections_have_one_winner(self):
        record = self.capture()
        def correct(n):
            c = Core(self.root)
            try:
                c.correct(record["record_id"], 1, "revision " + str(n), "test", "change-" + str(n))
                return "accepted"
            except UnimError as error:
                return error.code
            finally:
                c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(correct, range(2)))
        self.assertCountEqual(results, ["accepted", "conflict"])
        self.assertEqual(self.core.get(record["record_id"], 1)["content"], "The product is unanimis.")

    def test_current_search_excludes_superseded_content(self):
        record = self.capture("The codename is mango.")
        new = self.core.correct(record["record_id"], 1, "The codename is unanimis.", "rename", "rename")
        self.assertEqual(self.core.recall("mango")["matches"], [])
        self.assertEqual(self.core.recall("unanimis")["matches"][0]["revision"], 2)
        self.assertEqual(self.core.get(record["record_id"], 1)["state"], "superseded")
        self.assertEqual(new, self.core.correct(record["record_id"], 1, "The codename is unanimis.", "rename", "rename"))

    def test_other_scope_cannot_read_or_modify(self):
        r = self.capture()
        other = Core(self.root, scope="work")
        try:
            self.assertEqual(other.recall("unanimis")["matches"], [])
            with self.assertRaises(UnimError):
                other.get(r["record_id"])
            with self.assertRaises(UnimError):
                other.correct(r["record_id"], 1, "x", "y", "z")
        finally:
            other.close()

    def test_librarian_does_not_modify_records(self):
        a = self.capture("Same content [[missing-note]]", "a")
        b = self.capture("Same content [[missing-note]]", "b")
        packet = self.core.librarian()
        self.assertEqual(len(packet["duplicates"]), 1)
        self.assertEqual(packet["unresolved_link_count"], 2)
        self.assertEqual(self.core.get(a["record_id"])["revision"], 1)
        self.assertEqual(self.core.get(b["record_id"])["revision"], 1)

    def test_proposal_requires_literal_source_evidence(self):
        r = self.capture()
        with self.assertRaisesRegex(UnimError, "absent"):
            self.proposal(r, [{"record_id": r["record_id"], "revision": 1, "quote": "invented quote"}])
        self.assertEqual(self.core.list_proposals()["proposals"], [])

    def test_proposal_does_not_replace_current_until_approval(self):
        r = self.capture()
        p = self.proposal(r)
        self.assertEqual(self.core.get(r["record_id"])["revision"], 1)
        self.assertEqual(self.core.recall("unanimis")["matches"][0]["pending_proposals"][0]["status"], "pending")
        approved = self.core.approve(p["proposal_id"])
        self.assertEqual(approved["revision"], 2)
        self.assertEqual(self.core.get(r["record_id"])["tags"], ["decisions"])
        self.assertEqual(self.core.get(r["record_id"], 1)["content"], "The product is unanimis.")
        self.assertEqual(approved, self.core.approve(p["proposal_id"]))

    def test_approval_rejects_changed_evidence(self):
        target = self.capture()
        evidence = self.capture("Measured on September 6.", "evidence")
        p = self.proposal(target, [{"record_id": evidence["record_id"], "revision": 1, "quote": "Measured on September 6."}])
        self.core.correct(evidence["record_id"], 1, "Date was September 7.", "fix date", "date")
        with self.assertRaisesRegex(UnimError, "evidence changed"):
            self.core.approve(p["proposal_id"])
        self.assertEqual(self.core.get(target["record_id"])["revision"], 1)

    def test_edited_export_is_preserved_and_database_write_is_recoverable(self):
        r = self.capture()
        path = Path(r["readable_path"])
        path.write_text("My manual edit")
        retry = self.capture()
        self.assertIn("export_warning", retry)
        self.assertEqual(path.read_text(), "My manual edit")
        self.assertEqual(self.core.get(r["record_id"])["content"], "The product is unanimis.")
        path.unlink()
        self.core.export_all()
        self.assertIn("The product is unanimis.", path.read_text())

    def test_changed_import_does_not_silently_replace_a_record(self):
        path = self.root / "note.md"
        path.write_text("Original")
        first = self.core.import_file(path)
        path.write_text("Edited")
        with self.assertRaises(UnimError):
            self.core.import_file(path)
        self.assertEqual(self.core.get(first["record_id"])["content"], "Original")

    def test_search_handles_punctuation_and_rejects_invalid_limit(self):
        self.capture("Qwen3.8-27B model results")
        self.assertTrue(self.core.recall('Qwen3.8-27B " OR *')["matches"])
        with self.assertRaises(UnimError):
            self.core.recall("model", True)


    def test_excerpt_pages_cover_all_records_without_losing_full_evidence(self):
        content = "Pagination evidence " + "long source text " * 2500
        ids = {self.capture(content + str(i), "page-%s" % i)["record_id"] for i in range(15)}
        for read_page, field in ((lambda offset: self.core.recall("Pagination", 20, offset), "matches"),
                                 (lambda offset: self.core.librarian("Pagination", 20, offset), "records")):
            seen, offset = [], 0
            while True:
                page = read_page(offset)
                self.assertLess(len(json.dumps(page)), 22000)
                self.assertEqual(page["total_matches"], 15)
                for item in page[field]:
                    self.assertTrue(item["content_truncated"])
                    self.assertLessEqual(len(item["content"]), 1200)
                    self.assertIn(item["content"], self.core.get(item["record_id"], item["revision"])["content"])
                    seen.append(item["record_id"])
                if page["next_offset"] is None:
                    break
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
            self.assertEqual(set(seen), ids)
            self.assertEqual(len(seen), len(ids))
        self.assertGreater(len(self.core.get(next(iter(ids)))["content"]), 30000)
        self.assertEqual(self.core.librarian(offset=100)["records"], [])
        self.assertEqual(self.core.librarian(offset=100)["next_offset"], None)

    def test_acceptance_does_not_claim_independent_verification(self):
        decision = self.core.store("Product name: unanimis", "naming", metadata={"status": "accepted user naming decision"})
        for item in (self.core.get(decision["record_id"]), self.core.recall("Product")["matches"][0]):
            self.assertEqual(item["acceptance"], "accepted user naming decision")
            self.assertEqual(item["evidence_verification"], "not independently verified by unanimis")
        record = self.capture()
        self.core.approve(self.proposal(record)["proposal_id"])
        self.assertEqual(self.core.get(record["record_id"])["acceptance"], "accepted editorial revision")
        self.assertEqual(self.core.get(record["record_id"], 1)["acceptance"], "not recorded")

    def test_source_survives_corrections_without_rewriting_history(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unanimis.cli import render
        r = self.core.store("logo first", "source-test", source="/vault/Notes/Logo.md",
                            metadata={"status": "accepted user decision"})
        rid = r["record_id"]
        self.core.correct(rid, 1, "logo second", "Human note changed", "source-edit")
        self.core.correct(rid, 2, "logo third", "Human note changed", "source-edit-again")
        before = [tuple(row) for row in self.core.db.execute("SELECT * FROM revisions ORDER BY revision")]
        exports = {p: p.read_bytes() for p in self.root.rglob("*.md")}
        for record in (self.core.get(rid), self.core.get(rid, 2),
                       self.core.recall("logo")["matches"][0], self.core.librarian("logo")["records"][0]):
            self.assertEqual(record["source_context"], {"source": "/vault/Notes/Logo.md", "revision": 1})
            self.assertEqual(record["acceptance"], "not recorded")
        current = self.core.get(rid)
        self.assertNotIn("source", current["provenance"])
        self.assertEqual(current["provenance"]["previous_revision"], 2)
        for result in (current, self.core.recall("logo")):
            output = StringIO()
            with redirect_stdout(output):
                render(result)
            self.assertIn("/vault/Notes/Logo.md", output.getvalue())
        self.core._export(dict(record_id=rid, revision=3))
        self.assertEqual(before, [tuple(row) for row in self.core.db.execute("SELECT * FROM revisions ORDER BY revision")])
        self.assertEqual(exports, {p: p.read_bytes() for p in self.root.rglob("*.md")})
        self.assertEqual(self.core.get(rid, 1)["acceptance"], "accepted user decision")

    def test_source_lineage_with_approval_and_no_source(self):
        r = self.core.store("The product is unanimis.", "sourced", source="/vault/decision.md")
        self.core.approve(self.proposal(r)["proposal_id"])
        self.assertEqual(self.core.recall("product")["matches"][0]["source_context"],
                         {"source": "/vault/decision.md", "revision": 1})
        self.assertEqual(self.core.get(r["record_id"])["acceptance"], "accepted editorial revision")
        n = self.core.store("No original source", "no-source")
        self.core.correct(n["record_id"], 1, "Still no source", "edit", "edit-no-source")
        self.assertEqual(self.core.get(n["record_id"])["source_context"], {"source": None, "revision": None})
        # A future source declaration must never leak into a historical read.
        self.core.db.execute("UPDATE revisions SET provenance=? WHERE record_id=? AND revision=2",
                             (json.dumps({"source": "/later/source.md"}), n["record_id"]))
        self.assertIsNone(self.core.get(n["record_id"], 1)["source_context"]["source"])
        self.assertEqual(self.core.get(n["record_id"])["source_context"]["revision"], 2)

    def test_invalid_pagination_is_rejected(self):
        for arguments in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 21}):
            for operation in (self.core.librarian, lambda **args: self.core.recall("query", **args)):
                with self.assertRaises(UnimError):
                    operation(**arguments)


if __name__ == "__main__":
    unittest.main()
