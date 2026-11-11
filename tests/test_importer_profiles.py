import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from plaid_firefly import (CsvExporter, Store, SyncError, check_importer_profile,
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

    def test_writes_csv_to_bank_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            exporter = CsvExporter({'plaid-card': tmp}, {'visa-card': CARD})
            account = {'importer_profile': 'visa-card', 'name': 'Visa'}
            output = exporter.write('plaid-card', 'checking', [transaction('22.95')], account, 'cursor-1')
            self.assertEqual(output.parent, Path(tmp))
            self.assertIn(b'11/08/2026', output.read_bytes())
            self.assertNotIn(b'Posted', output.read_bytes())
            config = output.with_suffix('.json')
            self.assertEqual(config.read_text(), '{"test_importer": "credit-card"}')
            same_output = exporter.write('plaid-card', 'checking', [transaction('22.95')], account, 'cursor-1')
            self.assertEqual(output, same_output)
            self.assertEqual(len(list(Path(tmp).glob('*.csv'))), 1)
            self.assertEqual(len(list(Path(tmp).glob('*.json'))), 1)

    def test_unknown_profile_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            exporter = CsvExporter({'bank-chequing': tmp}, {'bank-chequing': CHEQUING})
            with self.assertRaises(SyncError):
                exporter.write('bank-chequing', 'id', [transaction('1.00')],
                               {'importer_profile': 'does-not-exist'}, 'cursor-1')
            self.assertFalse(list(Path(tmp).glob('*.csv')))

    def test_destination_is_required_for_each_bank(self):
        with tempfile.TemporaryDirectory() as tmp:
            exporter = CsvExporter({'other-bank': tmp}, {'bank-chequing': CHEQUING})
            with self.assertRaises(SyncError):
                exporter.write('bank-chequing', 'id', [transaction('1.00')],
                               {'importer_profile': 'bank-chequing'}, 'cursor-1')

    def test_importer_json_is_required(self):
        profile = dict(CARD)
        profile.pop('importer_config')
        with self.assertRaises(SyncError):
            check_importer_profile(profile)

    def test_invalid_relative_destination(self):
        with self.assertRaises(SyncError):
            CsvExporter({'bank-chequing': 'relative/path'}, {'bank-chequing': CHEQUING})

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
                exporter = CsvExporter({'bank': tmp}, {'bank-chequing': CHEQUING})
                item = {'key': 'bank', 'access_token': 'token',
                        'accounts': {'checking': {'currency': 'CAD', 'firefly_account_id': 4,
                                                  'importer_profile': 'not-configured'}}}
                with patch('plaid_firefly.fetch_updates', return_value=([transaction('22.01')], [], [], 'newcursor')):
                    with self.assertRaises(SyncError):
                        process_item(item, store, None, NoMatches(), exporter, 'client', 'secret', 'host', False)
                self.assertIsNone(store.cursor('bank'))
                self.assertFalse(list(Path(tmp).glob('*.csv')))

    def test_export_marks_rows_and_advances_cursor_after_write(self):
        class NoMatches:
            def find(self, ext):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with Store(str(Path(tmp) / 's.db')) as store:
                exporter = CsvExporter({'bank': tmp}, {'bank-chequing': CHEQUING})
                item = {'key': 'bank', 'access_token': 'token',
                        'accounts': {'checking': {'currency': 'CAD', 'firefly_account_id': 4,
                                                  'importer_profile': 'bank-chequing'}}}
                with patch('plaid_firefly.fetch_updates',
                           return_value=([transaction('22.01')], [], [], 'newcursor')):
                    process_item(item, store, None, NoMatches(), exporter,
                                 'client', 'secret', 'host', False)
                self.assertEqual(store.cursor('bank'), 'newcursor')
                self.assertEqual(store.seen('plaid:bank:x1')[1], 'exported')
                self.assertEqual(len(list(Path(tmp).glob('*.csv'))), 1)

    def test_encoding_excel_bom(self):
        bom = dict(CARD)
        bom['csv'] = dict(CARD['csv'], encoding='utf-8-sig')
        self.assertTrue(render_importer_csv([transaction('1.00')], {}, bom).startswith(b'\xef\xbb\xbf'))


if __name__ == '__main__':
    unittest.main()
