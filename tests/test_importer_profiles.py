import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from plaid_firefly import (Importer, Store, SyncError, check_importer_profile,
                          render_importer_csv, process_item)


CHEQUING = {
    'importer_config': {'test_importer': 'chequing'},
    'csv': {
        'date_format': '%d/%m/%Y',
        'delimiter': ';',
        'decimal_separator': ',',
        'thousands_separator': '.',
        'amount_sign': 'inflow_positive',
        'columns': [
            {'header': 'Date', 'field': 'date'},
            {'header': 'Payee', 'field': 'description'},
            {'header': 'Debit', 'field': 'debit'},
            {'header': 'Credit', 'field': 'credit'},
            {'header': 'Plaid Ref', 'field': 'external_id'},
        ],
    },
}
CARD = {
    'importer_config': {'test_importer': 'credit-card'},
    'csv': {
        'date_format': '%m/%d/%Y',
        'delimiter': ',',
        'include_header': False,
        'amount_sign': 'outflow_positive',
        'columns': [
            {'header': 'Account', 'field': 'account_name'},
            {'header': 'Posted', 'field': 'date'},
            {'header': 'Description', 'field': 'description'},
            {'header': 'Amount', 'field': 'amount'},
            {'header': 'TransactionId', 'field': 'external_id'},
        ],
    },
}


def transaction(amount, txid='x1'):
    return {'date': '2026-11-08', 'amount': amount, 'name': 'Example Shop',
            'account_id': 'checking', 'transaction_id': txid,
            '_external_id': 'plaid:bank:' + txid, '_currency': 'CAD', 'pending': False}


class Response:
    ok = True
    status_code = 200


class UploadSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return Response()


class ProfileTests(unittest.TestCase):
    def test_chequing_split_columns_and_locale(self):
        csv_bytes = render_importer_csv([transaction('1234.50'), transaction('-42.10', 'x2')], {}, CHEQUING)
        rows = list(csv.reader(io.StringIO(csv_bytes.decode('utf-8')), delimiter=';'))
        self.assertEqual(rows[0], ['Date', 'Payee', 'Debit', 'Credit', 'Plaid Ref'])
        self.assertEqual(rows[1], ['08/11/2026', 'Example Shop', '1.234,50', '', 'plaid:bank:x1'])
        self.assertEqual(rows[2], ['08/11/2026', 'Example Shop', '', '42,10', 'plaid:bank:x2'])

    def test_card_no_header_and_positive_outflows(self):
        value = render_importer_csv([transaction('19.99'), transaction('-30.22', 'x2')],
                                    {'name': 'Visa Account'}, CARD)
        rows = list(csv.reader(io.StringIO(value.decode('utf-8'))))
        self.assertEqual(rows[0], ['Visa Account', '11/08/2026', 'Example Shop', '19.99', 'plaid:bank:x1'])
        self.assertEqual(rows[1][-2], '-30.22')

    def test_upload_uses_account_selected_profile_and_json(self):
        session = UploadSession()
        importer = Importer(session, 'http://importer', 'not-a-real-secret', 'test-token',
                            {'bank-chequing': CHEQUING, 'visa-card': CARD})
        importer.send('plaid-card', [transaction('22.95')], {'importer_profile': 'visa-card', 'name': 'Visa'})
        self.assertEqual(len(session.calls), 1)
        args, kwargs = session.calls[0]
        self.assertEqual(kwargs['files']['json'][1], b'{"test_importer": "credit-card"}')
        self.assertIn(b'11/08/2026', kwargs['files']['importable'][1])
        self.assertNotIn(b'Posted', kwargs['files']['importable'][1])

    def test_unknown_profile_does_not_upload(self):
        session = UploadSession()
        importer = Importer(session, 'http://importer', 'secret', 'token', {'bank-chequing': CHEQUING})
        with self.assertRaises(SyncError):
            importer.send('id', [transaction('1.00')], {'importer_profile': 'does-not-exist'})
        self.assertFalse(session.calls)

    def test_external_id_required(self):
        invalid = {'importer_config': {'x': 'y'}, 'csv': {'columns': [{'field': 'date', 'header': 'Date'}]}}
        with self.assertRaises(SyncError):
            check_importer_profile(invalid)

    def test_missing_profile_preserves_cursor(self):
        class NoMatches:
            def find(self, ext):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with Store(str(Path(tmp) / 's.db')) as store:
                session = UploadSession()
                importer = Importer(session, 'http://importer', 'secret', 'token', {'bank-chequing': CHEQUING})
                item = {'key': 'bank', 'access_token': 'token',
                        'accounts': {'checking': {'currency': 'CAD', 'firefly_account_id': 4,
                                                  'importer_profile': 'not-configured'}}}
                with patch('plaid_firefly.fetch_updates', return_value=([transaction('22.01')], [], [], 'newcursor')):
                    with self.assertRaises(SyncError):
                        process_item(item, store, None, NoMatches(), importer, 'client', 'secret', 'host', False)
                self.assertIsNone(store.cursor('bank'))
                self.assertFalse(session.calls)

    def test_encoding_excel_bom(self):
        bom = dict(CARD)
        bom['csv'] = dict(CARD['csv'], encoding='utf-8-sig')
        self.assertTrue(render_importer_csv([transaction('1.00')], {}, bom).startswith(b'\xef\xbb\xbf'))


if __name__ == '__main__':
    unittest.main()
