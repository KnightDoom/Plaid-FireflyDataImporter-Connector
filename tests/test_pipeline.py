import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plaid_firefly import Store, SyncError, fetch_updates, process_item


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cursors = []

    def post(self, url, json, timeout):
        self.cursors.append(json.get("cursor"))
        return self.responses.pop(0)


class FakeFirefly:
    def __init__(self):
        self.ids = {}
        self.posts = []

    def find(self, external_id):
        return self.ids.get(external_id)

    def store(self, tx, mapping, ext_id):
        self.posts.append(ext_id)
        self.ids[ext_id] = "123"
        return "123"


ITEM = {"key": "bank1", "access_token": "test-token", "accounts": {"bank_acct": {"firefly_account_id": 3, "currency": "CAD"}}}
TX = {"transaction_id": "txn123", "account_id": "bank_acct", "date": "2026-11-01", "amount": 12.34, "name": "Store", "pending": False}


class PipelineTests(unittest.TestCase):
    def test_mutation_restarts_at_initial_cursor(self):
        session = FakeSession([
            FakeResponse(200, {"added": [TX], "modified": [], "removed": [], "next_cursor": "page1", "has_more": True}),
            FakeResponse(400, {"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}),
            FakeResponse(200, {"added": [TX], "modified": [], "removed": [], "next_cursor": "finished", "has_more": False}),
        ])
        added, _, _, cursor = fetch_updates(session, ITEM, "id", "secret", "https://sandbox.plaid.com", "saved")
        self.assertEqual(session.cursors, ["saved", "page1", "saved"])
        self.assertEqual(len(added), 1)
        self.assertEqual(cursor, "finished")

    def test_cursor_advances_only_after_import_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store(str(Path(tmp) / "state.db")) as db:
                firefly = FakeFirefly()
                args = (ITEM, db, None, firefly, None, "id", "secret", "https://sandbox.plaid.com", False)
                with patch('plaid_firefly.fetch_updates', return_value=([TX], [], [], "c1")):
                    process_item(*args)
                self.assertEqual(len(firefly.posts), 1)
                self.assertEqual(db.cursor("bank1"), "c1")
                with patch('plaid_firefly.fetch_updates', return_value=([TX], [], [], "c2")):
                    process_item(*args)
                self.assertEqual(len(firefly.posts), 1)
                self.assertEqual(db.cursor("bank1"), "c2")

    def test_unmapped_account_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store(str(Path(tmp) / "state.db")) as db:
                unknown = dict(TX, account_id="unknown")
                with patch('plaid_firefly.fetch_updates', return_value=([unknown], [], [], "c1")):
                    with self.assertRaises(SyncError):
                        process_item(ITEM, db, None, FakeFirefly(), None, "id", "secret", "host", False)
                self.assertIsNone(db.cursor("bank1"))

    def test_dry_run_never_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store(str(Path(tmp) / "state.db")) as db:
                with patch('plaid_firefly.fetch_updates', return_value=([TX], [], [], "c1")):
                    process_item(ITEM, db, None, FakeFirefly(), None, "id", "secret", "host", True)
                self.assertIsNone(db.cursor("bank1"))


if __name__ == '__main__':
    unittest.main()
