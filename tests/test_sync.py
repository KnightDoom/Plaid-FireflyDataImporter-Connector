import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plaid_firefly import amount_string, external_id, fingerprint


class TransformTests(unittest.TestCase):
    def test_external_id(self):
        self.assertEqual(external_id("bank1", "abc"), "plaid:bank1:abc")

    def test_amount(self):
        self.assertEqual(amount_string(-12.34), "12.34")

    def test_fingerprint_stability(self):
        tx = {"transaction_id": "x", "account_id": "a", "date": "2026-01-01", "amount": 1.50}
        self.assertEqual(fingerprint(tx), fingerprint(dict(reversed(list(tx.items())))))


if __name__ == "__main__":
    unittest.main()
