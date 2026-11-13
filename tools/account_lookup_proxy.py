"""Local proxy for Plaid account lookups from the file-based generator.

The proxy binds only to localhost and forwards account lookup requests to the
selected Plaid environment over HTTPS. It intentionally returns account
metadata only and never logs request bodies or credentials.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HOST = "127.0.0.1"
PORT = int(os.environ.get("PLAID_PROXY_PORT", "8765"))
PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
ALLOWED_ORIGINS = {"null", "http://localhost", "http://127.0.0.1"}


def json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def plaid_error(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        return str(payload.get("display_message") or payload.get("error_message") or payload.get("error_code") or fallback)
    return fallback


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "PlaidAccountLookupProxy/1.0"

    def log_message(self, format: str, *args: object) -> None:
        # Do not log request bodies, headers, URLs, or credentials.
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def origin(self) -> str:
        return self.headers.get("Origin", "")

    def cors_headers(self) -> dict[str, str]:
        origin = self.origin()
        return {"Access-Control-Allow-Origin": origin if origin in ALLOWED_ORIGINS else "null",
                "Vary": "Origin"}

    def send_json(self, status: int, payload: object) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        for name, value in self.cors_headers().items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/plaid/accounts":
            self.send_json(404, {"error": "Not found."})
            return
        self.send_response(204)
        for name, value in self.cors_headers().items():
            self.send_header(name, value)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/plaid/accounts":
            self.send_json(404, {"error": "Not found."})
            return
        if self.origin() not in ALLOWED_ORIGINS:
            self.send_json(403, {"error": "Origin is not allowed."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("Request body is missing or too large.")
            request = json.loads(self.rfile.read(length))
            environment = str(request.get("environment", "")).strip().lower()
            client_id = str(request.get("client_id", "")).strip()
            secret = str(request.get("secret", "")).strip()
            access_token = str(request.get("access_token", "")).strip()
            if environment not in PLAID_HOSTS:
                raise ValueError("environment must be sandbox, development, or production.")
            if not client_id or not secret or not access_token:
                raise ValueError("client_id, secret, and access_token are required.")

            plaid_request = Request(
                f"{PLAID_HOSTS[environment]}/accounts/get",
                data=json_bytes({"client_id": client_id, "secret": secret, "access_token": access_token}),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            context = ssl.create_default_context()
            with urlopen(plaid_request, timeout=30, context=context) as response:
                payload = json.loads(response.read())
            accounts = []
            for account in payload.get("accounts", []):
                balances = account.get("balances") or {}
                accounts.append({
                    "account_id": account.get("account_id", ""),
                    "name": account.get("name", ""),
                    "official_name": account.get("official_name", ""),
                    "iso_currency_code": balances.get("iso_currency_code"),
                    "unofficial_currency_code": balances.get("unofficial_currency_code"),
                })
            self.send_json(200, {"accounts": accounts})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except HTTPError as error:
            try:
                payload = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            self.send_json(error.code, {"error": plaid_error(payload, f"Plaid returned HTTP {error.code}.")})
        except (URLError, TimeoutError) as error:
            self.send_json(502, {"error": f"Could not reach Plaid: {error.reason if isinstance(error, URLError) else error}"})
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            self.send_json(400, {"error": f"Invalid request: {error}"})
        except Exception:
            self.send_json(502, {"error": "The Plaid account lookup failed."})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    print(f"Plaid account proxy listening at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping proxy.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
