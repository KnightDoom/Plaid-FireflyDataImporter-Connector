"""Plaid Transactions Sync -> Firefly III (API or Data Importer).

Reference implementation for a single-instance scheduled Kubernetes CronJob.
Secrets are read from environment variables sourced from a SOPS-encrypted
Kubernetes Secret; sqlite on a PVC stores cursors and minimal metadata.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import time
from decimal import Decimal
from datetime import date
from typing import Any

import requests

LOG = logging.getLogger("plaid_firefly")
PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}


class SyncError(RuntimeError):
    pass


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SyncError(f"Required setting {name} is missing")
    return value


def get_json_env(name: str, default: Any = None) -> Any:
    value = os.environ.get(name, "")
    return json.loads(value) if value.strip() else default


def fingerprint(tx: dict[str, Any]) -> str:
    fields = ("transaction_id", "account_id", "date", "amount", "name", "merchant_name", "iso_currency_code", "pending")
    value = {name: tx.get(name) for name in fields}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def external_id(item_key: str, transaction_id: str) -> str:
    return f"plaid:{item_key}:{transaction_id}"


def amount_string(value: str | float | int) -> str:
    return format(abs(Decimal(str(value))), "f")


def display_name(tx: dict[str, Any]) -> str:
    return (tx.get("merchant_name") or tx.get("name") or "Unknown counterparty")[:200]


def fetch_updates(session: requests.Session, item: dict[str, Any], client_id: str, secret: str,
                  host: str, cursor: str | None) -> tuple[list[dict], list[dict], list[dict], str]:
    """Fully page from one cursor; restart the whole loop after a Plaid mutation error."""
    for attempt in range(4):
        page_cursor = cursor
        added: list[dict] = []
        modified: list[dict] = []
        removed: list[dict] = []
        while True:
            payload = {"client_id": client_id, "secret": secret,
                       "access_token": item["access_token"], "count": 500}
            if page_cursor:
                payload["cursor"] = page_cursor
            try:
                response = session.post(f"{host}/transactions/sync", json=payload, timeout=45)
            except requests.RequestException as exc:
                raise SyncError("Plaid network request failed") from exc
            if response.status_code != 200:
                try:
                    code = response.json().get("error_code", "UNKNOWN")
                except ValueError:
                    code = "UNKNOWN"
                if code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                    LOG.warning("Plaid changed data during pagination; restarting item pagination")
                    break
                raise SyncError(f"Plaid sync request failed with HTTP {response.status_code} ({code})")
            try:
                data = response.json()
                added.extend(data["added"])
                modified.extend(data["modified"])
                removed.extend(data["removed"])
                page_cursor = data["next_cursor"]
                if not data["has_more"]:
                    return added, modified, removed, page_cursor
            except (KeyError, ValueError, TypeError) as exc:
                raise SyncError("Plaid returned an invalid sync response") from exc
        LOG.warning("Pagination retry %s of 4", attempt + 1)
    raise SyncError("Plaid pagination repeatedly mutated; cursor remains unchanged")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("CREATE TABLE IF NOT EXISTS cursors (item_key TEXT PRIMARY KEY, cursor TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS ledger (
            external_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, firefly_group_id TEXT)""")
        self.db.commit()

    def cursor(self, key: str) -> str | None:
        row = self.db.execute("SELECT cursor FROM cursors WHERE item_key=?", (key,)).fetchone()
        return row[0] if row else None

    def seen(self, key: str) -> tuple[str, str] | None:
        row = self.db.execute("SELECT fingerprint, status FROM ledger WHERE external_id=?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def mark(self, key: str, fp: str, status: str, group_id: str | None = None) -> None:
        with self.db:
            self.db.execute("""INSERT INTO ledger (external_id, fingerprint, status, firefly_group_id)
                VALUES (?, ?, ?, ?) ON CONFLICT(external_id) DO UPDATE SET
                fingerprint=excluded.fingerprint, status=excluded.status,
                firefly_group_id=COALESCE(excluded.firefly_group_id, ledger.firefly_group_id)""",
                (key, fp, status, group_id))

    def advance(self, item_key: str, cursor: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO cursors(item_key, cursor) VALUES (?, ?)
                ON CONFLICT(item_key) DO UPDATE SET cursor=excluded.cursor""", (item_key, cursor))

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


class Firefly:
    def __init__(self, session: requests.Session, base_url: str, token: str):
        self.session = session
        self.url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def find(self, ext_id: str) -> str | None:
        # This Firefly search operator is supported: external_id:"..."
        try:
            response = self.session.get(f"{self.url}/api/v1/search/transactions",
                                        headers=self.headers,
                                        params={"query": f'external_id:"{ext_id}"'}, timeout=45)
        except requests.RequestException as exc:
            raise SyncError("Firefly search failed due to a network error") from exc
        if response.status_code != 200:
            raise SyncError(f"Firefly search failed with HTTP {response.status_code}")
        for group in response.json().get("data", []):
            for entry in group.get("attributes", {}).get("transactions", []):
                if entry.get("external_id") == ext_id:
                    return str(group["id"])
        return None

    def store(self, tx: dict[str, Any], mapping: dict[str, Any], ext_id: str) -> str:
        value = Decimal(str(tx["amount"]))
        if value == 0:
            raise SyncError("Zero-value transactions require an explicit mapping rule")
        account_id = str(mapping["firefly_account_id"])
        counterparty = display_name(tx)
        currency = tx.get("iso_currency_code") or mapping.get("currency")
        if not currency:
            raise SyncError("Missing supported ISO currency; configure account currency")
        part: dict[str, Any] = {
            "type": "withdrawal" if value > 0 else "deposit",
            "date": tx["date"], "amount": amount_string(tx["amount"]),
            "currency_code": currency, "description": counterparty,
            "external_id": ext_id,
        }
        if value > 0:
            part.update(source_id=account_id, destination_name=counterparty)
        else:
            part.update(source_name=counterparty, destination_id=account_id)
        try:
            response = self.session.post(f"{self.url}/api/v1/transactions", headers=self.headers,
                                         json={"transactions": [part], "apply_rules": True,
                                               "error_if_duplicate_hash": True}, timeout=45)
        except requests.RequestException as exc:
            # The write might already have reached Firefly. Caller must re-check by external_id.
            raise SyncError("Firefly write outcome uncertain; inspect/reconcile before retry") from exc
        if not response.ok:
            raise SyncError(f"Firefly rejected a transaction with HTTP {response.status_code}")
        try:
            return str(response.json()["data"]["id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise SyncError("Firefly write response could not be verified") from exc


# Strict field allow-list avoids accidental attempts to read arbitrary private Plaid data.
CSV_FIELDS = {
    "date", "authorized_date", "description", "name", "merchant_name", "original_description",
    "amount", "amount_absolute", "debit", "credit", "external_id", "currency_code",
    "account_id", "account_name", "transaction_id", "payment_channel", "check_number",
    "constant", "blank",
}


def check_importer_profile(profile: dict[str, Any]) -> None:
    """Fail closed on invalid/incomplete bank-specific importer settings."""
    if not isinstance(profile, dict) or not isinstance(profile.get("importer_config"), dict) or not profile["importer_config"]:
        raise SyncError("Each importer profile needs a full exported importer_config JSON")
    config = profile.get("csv")
    if not isinstance(config, dict):
        raise SyncError("Each importer profile must contain csv settings")
    columns = config.get("columns")
    if not isinstance(columns, list) or not columns:
        raise SyncError("Each CSV profile needs a nonempty ordered columns array")
    if any(not isinstance(col, dict) or col.get("field") not in CSV_FIELDS or
           not isinstance(col.get("header"), str) or not col["header"] for col in columns):
        raise SyncError("CSV columns must have a nonempty header and an allowed field")
    if sum(col["field"] == "external_id" for col in columns) != 1:
        raise SyncError("CSV profile must have exactly one external_id column for safe verification")
    if len({col["header"] for col in columns}) != len(columns):
        raise SyncError("CSV headers must be unique")
    if any(col["field"] == "constant" and not isinstance(col.get("value"), str) for col in columns):
        raise SyncError("CSV constant fields need a string value")
    if config.get("amount_sign", "inflow_positive") not in ("inflow_positive", "outflow_positive", "absolute"):
        raise SyncError("CSV amount_sign must be inflow_positive, outflow_positive, or absolute")
    if config.get("encoding", "utf-8") not in ("utf-8", "utf-8-sig", "cp1252", "iso-8859-1"):
        raise SyncError("CSV encoding must be utf-8, utf-8-sig, cp1252 or iso-8859-1")
    delimiter = config.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in ('\r', '\n', '"'):
        raise SyncError("CSV delimiter must be one valid character")
    if config.get("decimal_separator", ".") not in (".", ","):
        raise SyncError("CSV decimal_separator must be '.' or ','")
    if config.get("thousands_separator", "") not in ("", ".", ",", " "):
        raise SyncError("CSV thousands_separator must be blank, '.', ',' or space")
    if config.get("thousands_separator", "") and config.get("thousands_separator") == config.get("decimal_separator", "."):
        raise SyncError("Thousands and decimal separators cannot be the same")
    if not isinstance(config.get("date_format", "%Y-%m-%d"), str):
        raise SyncError("CSV date_format must be a Python strftime format")
    if not isinstance(config.get("include_header", True), bool):
        raise SyncError("CSV include_header must be boolean")
    if config.get("line_ending", "crlf") not in ("crlf", "lf"):
        raise SyncError("CSV line_ending must be 'crlf' or 'lf'")


def format_csv_number(amount: Decimal, config: dict[str, Any]) -> str:
    # Use Decimal all the way through to preserve the bank's exact money values.
    raw = format(amount, "f")
    negative = raw.startswith("-")
    integral, dot, fractional = raw.lstrip("-").partition(".")
    separator = config.get("thousands_separator", "")
    if separator:
        integral = separator.join(reversed([integral[max(i - 3, 0):i]
                                              for i in range(len(integral), 0, -3)]))
    formatted = integral + (config.get("decimal_separator", ".") + fractional if dot else "")
    return ("-" if negative else "") + formatted


def csv_cell(field: dict[str, str], tx: dict[str, Any], account: dict[str, Any],
             config: dict[str, Any]) -> str:
    key = field["field"]
    value = Decimal(str(tx["amount"]))  # Plaid: positive = funds leaving account.
    if key in ("date", "authorized_date"):
        raw = tx.get(key) or tx.get("date")
        return date.fromisoformat(str(raw)[:10]).strftime(config.get("date_format", "%Y-%m-%d"))
    if key in ("amount", "amount_absolute", "debit", "credit"):
        if key == "debit" and value <= 0:
            return ""
        if key == "credit" and value >= 0:
            return ""
        if key == "debit" or key == "credit" or key == "amount_absolute":
            converted = abs(value)
        elif config.get("amount_sign", "inflow_positive") == "inflow_positive":
            converted = -value
        elif config.get("amount_sign") == "absolute":
            converted = abs(value)
        else:
            converted = value
        return format_csv_number(converted, config)
    if key == "description":
        return display_name(tx)
    if key == "original_description":
        return str(tx.get("original_description") or tx.get("name") or "")
    if key == "external_id":
        return str(tx["_external_id"])
    if key == "currency_code":
        return str(tx.get("iso_currency_code") or tx.get("_currency") or "")
    if key == "account_name":
        return str(account.get("name") or "")
    if key == "constant":
        return field["value"]
    if key == "blank":
        return ""
    return str(tx.get(key) or "")


def render_importer_csv(rows: list[dict[str, Any]], account: dict[str, Any],
                        profile: dict[str, Any]) -> bytes:
    check_importer_profile(profile)
    config = profile["csv"]
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer, delimiter=config.get("delimiter", ","),
                        lineterminator="\r\n" if config.get("line_ending", "crlf") == "crlf" else "\n")
    if config.get("include_header", True):
        writer.writerow([col["header"] for col in config["columns"]])
    for tx in rows:
        writer.writerow([csv_cell(col, tx, account, config) for col in config["columns"]])
    try:
        return csv_buffer.getvalue().encode(config.get("encoding", "utf-8"))
    except UnicodeEncodeError as exc:
        raise SyncError("CSV contains data not supported by the selected bank encoding") from exc


class Importer:
    def __init__(self, session: requests.Session, url: str, auto_secret: str, token: str,
                 profiles: dict[str, dict]):
        self.session = session
        self.url = url.rstrip("/")
        self.auto_secret = auto_secret
        self.token = token
        if not isinstance(profiles, dict) or not profiles:
            raise SyncError("IMPORTER_PROFILES_JSON must be a nonempty profile mapping")
        for profile_name, profile in profiles.items():
            try:
                check_importer_profile(profile)
            except SyncError as exc:
                raise SyncError(f"Invalid importer profile {profile_name!r}: {exc}") from exc
        self.profiles = profiles

    def send(self, account_id: str, rows: list[dict[str, Any]], account: dict[str, Any]) -> None:
        # The exact account mapping selects the profile; no bank-wide inference.
        profile_name = account.get("importer_profile")
        if not profile_name or profile_name not in self.profiles:
            raise SyncError("A mapped Plaid account lacks a valid importer_profile; nothing uploaded")
        profile = self.profiles[profile_name]
        csv_bytes = render_importer_csv(rows, account, profile)
        files = {
            "importable": (f"plaid-{profile_name}.csv", csv_bytes, "text/csv"),
            "json": (f"plaid-{profile_name}.json", json.dumps(profile["importer_config"]).encode("utf-8"),
                     "application/json"),
        }
        try:
            response = self.session.post(f"{self.url}/autoupload",
                                         params={"secret": self.auto_secret},
                                         headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"},
                                         files=files, timeout=300)
        except requests.RequestException as exc:
            raise SyncError("Data Importer upload outcome is uncertain") from exc
        if not response.ok:
            raise SyncError(f"Data Importer upload failed with HTTP {response.status_code}")
        # HTTP success is not per-row confirmation. Caller verifies every external ID in Firefly.


def process_item(item: dict[str, Any], store: Store, session: requests.Session,
                 firefly: Firefly, importer: Importer | None, client_id: str,
                 secret: str, host: str, dry_run: bool) -> tuple[int, int]:
    item_key = item["key"]
    if not item_key.replace("-", "").replace("_", "").isalnum():
        raise SyncError("Item key should contain only letters, numbers, '_' or '-'")
    old_cursor = store.cursor(item_key)
    added, modified, removed, new_cursor = fetch_updates(session, item, client_id, secret, host, old_cursor)
    LOG.info("Item %s: %d added, %d modified, %d removed events", item_key,
             len(added), len(modified), len(removed))
    accounts = item.get("accounts", {})
    ignored = set(item.get("ignore_accounts", []))
    # Detect final posted state for each ID. Added and modified may overlap pages.
    cutoff = os.getenv("IMPORT_START_DATE", "").strip()
    if cutoff:
        try:
            date.fromisoformat(cutoff)
        except ValueError as exc:
            raise SyncError("IMPORT_START_DATE must be YYYY-MM-DD") from exc
    current = {tx["transaction_id"]: tx for tx in added}
    current.update({tx["transaction_id"]: tx for tx in modified})
    removed_ids = {r["transaction_id"] for r in removed}
    for tx_id in removed_ids:
        current.pop(tx_id, None)
    candidate: dict[str, list[dict]] = {}
    review = 0
    for tx in current.values():
        if tx.get("pending") or (cutoff and tx["date"] < cutoff):
            continue
        a_id = tx["account_id"]
        if a_id in ignored:
            continue
        if a_id not in accounts:
            raise SyncError("Plaid returned a posted transaction from an unmapped account; cursor not advanced")
        if tx.get("unofficial_currency_code"):
            raise SyncError("Unofficial currency requires an explicit conversion rule")
        ext_id = external_id(item_key, tx["transaction_id"])
        fp = fingerprint(tx)
        known = store.seen(ext_id)
        if known:
            if known[0] != fp and known[1] in ("imported", "review_modified", "review_removed"):
                LOG.warning("An already-imported transaction changed; manual reconciliation required")
                review += 1
                if not dry_run:
                    store.mark(ext_id, fp, "review_modified")
            continue
        # Authoritative search makes recovery safe when an earlier POST succeeded but sqlite did not update.
        found = None if dry_run else firefly.find(ext_id)
        if found:
            if not dry_run:
                store.mark(ext_id, fp, "imported", found)
            continue
        tx_copy = dict(tx, _external_id=ext_id, _currency=accounts[a_id].get("currency"))
        candidate.setdefault(a_id, []).append(tx_copy)

    imported = 0
    for account_id, txs in candidate.items():
        if dry_run:
            LOG.info("DRY RUN: would import %d transactions into mapped account", len(txs))
            imported += len(txs)
            continue
        if importer is None:
            for tx in txs:
                ext_id = tx["_external_id"]
                # Check again when doing writes to survive partial previous attempts.
                existing = firefly.find(ext_id)
                group = existing if existing else firefly.store(tx, accounts[account_id], ext_id)
                store.mark(ext_id, fingerprint(tx), "imported", group)
                imported += 1
        else:
            importer.send(account_id, txs, accounts[account_id])
            for tx in txs:
                group = None
                for attempt in range(5):
                    group = firefly.find(tx["_external_id"])
                    if group:
                        break
                    time.sleep(2 ** attempt)
                if not group:
                    raise SyncError("Data Importer did not verify all rows; cursor preserved for retry")
                store.mark(tx["_external_id"], fingerprint(tx), "imported", group)
                imported += 1

    for r in removed:
        if r["account_id"] in ignored:
            continue
        ext_id = external_id(item_key, r["transaction_id"])
        known = store.seen(ext_id)
        if known and known[1] != "review_removed":
            review += 1
            if not dry_run:
                store.mark(ext_id, known[0], "review_removed")
    if not dry_run:
        store.advance(item_key, new_cursor)
    LOG.info("Item %s finished: %d imported, %d review flags, cursor %s",
             item_key, imported, review, "preserved (dry-run)" if dry_run else "advanced")
    return imported, review


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    plaid_env = os.getenv("PLAID_ENV", "production")
    if plaid_env not in PLAID_HOSTS:
        raise SyncError("PLAID_ENV must be sandbox, development, or production")
    mode = os.getenv("MODE", "direct")
    if mode not in ("direct", "importer"):
        raise SyncError("MODE must be direct or importer")
    dry_run = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
    items = get_json_env("PLAID_ITEMS_JSON", {}).get("items", [])
    if not items:
        raise SyncError("PLAID_ITEMS_JSON must define at least one linked Item")
    keys = [item["key"] for item in items]
    if len(set(keys)) != len(keys):
        raise SyncError("Item keys must be unique")
    with requests.Session() as session:
        firefly = Firefly(session, required("FIREFLY_URL"), required("FIREFLY_TOKEN"))
        importer = None
        if mode == "importer":
            importer = Importer(session, required("IMPORTER_URL"),
                                required("IMPORTER_AUTO_IMPORT_SECRET"), required("FIREFLY_TOKEN"),
                                get_json_env("IMPORTER_PROFILES_JSON", {}))
        store = Store(os.getenv("STATE_DB", "/config/state.sqlite"))
        for item in items:
            process_item(item, store, session, firefly, importer,
                         required("PLAID_CLIENT_ID"), required("PLAID_SECRET"),
                         PLAID_HOSTS[plaid_env], dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SyncError, KeyError, ValueError, sqlite3.Error) as exc:
        # Do not log HTTP bodies, credentials, or full Plaid transactions.
        LOG.error("Sync aborted: %s", exc)
        sys.exit(1)
