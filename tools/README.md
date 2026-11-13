# Environment configuration generator

[`env-config-generator.html`](./env-config-generator.html) is a standalone browser tool for creating and updating the JSON values used by this project’s `.env` file.

It does not require Node.js, Python, a webserver, or network access.

## Open the tool

Open [`env-config-generator.html`](./env-config-generator.html) directly in a browser. For example:

```text
file:///path/to/Plaid-FireflyDataImporter-Connector/tools/env-config-generator.html
```

The tool keeps entered values in the current browser tab. It does not send them to Plaid, Firefly III, or any other service.

## Load existing configuration

Paste your complete `.env` block into **ENV block**, then click **Load JSON**. The tool extracts the runtime settings and JSON mappings used by the connector. Block values take precedence over individual fields.

Under **Load individual JSON values**, the tool also accepts:

- Raw JSON, such as `{"bank-one":"/exports/bank-one"}`.
- A generated environment line, such as `IMPORTER_DESTINATIONS_JSON={...}`.
- The complete generated `.env` block.

Paste one or more values into the matching fields and click **Load JSON**. The tool populates the bank, account, destination, and profile editors. Existing profile fields that are not represented by the form are preserved when the configuration is regenerated.

The tool can also export a complete editor snapshot with **Export state JSON**. This snapshot is useful for troubleshooting or later editing, but it may contain access tokens and importer configuration data.

## Configure banks and accounts

Each bank has:

- A unique bank key. This is the Plaid Item `key` and is also used by `IMPORTER_DESTINATIONS_JSON`.
- A Plaid access token.
- A writable container destination, such as `/exports/bank-one`.
- One or more Plaid account mappings.

Each account mapping includes:

- The Plaid account ID.
- The Firefly account ID.
- A three-letter ISO currency code, such as `CAD` or `USD`.
- An importer profile selected from the profile dropdown.

The generated structure is equivalent to:

```json
{
  "items": [
    {
      "key": "bank-one",
      "access_token": "replace-me",
      "accounts": {
        "plaid-account-id": {
          "firefly_account_id": 1,
          "currency": "CAD",
          "importer_profile": "bank-one-chequing"
        }
      }
    }
  ]
}
```

## Configure importer profiles

You do not need a real bank CSV to create the first profile. Define the desired CSV headers and settings in the tool, then click **Download mock CSV** on the profile card. Import that mock file into Firefly III Data Importer, configure the column roles and duplicate detection, then export the Data Importer configuration JSON. Paste that exported JSON into the profile’s **Exported Data Importer JSON** field.

The mock file contains the configured headers and two representative rows, including an outflow and an inflow. It is only for configuring the Data Importer; the connector will later replace those rows with Plaid transactions.

The CSV settings in the tool must produce the same column order, delimiter, and header behavior expected by the exported Data Importer configuration. Every profile must contain exactly one `external_id` column.

Each generated profile has this shape:

```json
{
  "bank-one-chequing": {
    "importer_config": {
      "version": 3,
      "flow": "file",
      "content_type": "csv"
    },
    "csv": {
      "delimiter": ",",
      "include_header": true,
      "columns": [
        {"header": "Date", "field": "date"},
        {"header": "Description", "field": "description"},
        {"header": "Amount", "field": "amount"},
        {"header": "Currency", "field": "currency_code"},
        {"header": "External ID", "field": "external_id"}
      ]
    }
  }
}
```

The `importer_config` object should be the complete exported configuration, not just the abbreviated example above. The generated CSV and JSON sidecar use the same filename stem so Firefly III Data Importer can process them as a pair.

## Generate and troubleshoot

1. Fill in the banks, accounts, destinations, and profiles.
2. Click **Generate JSON**.
3. Review the copy-ready `.env` block.
4. Click **Copy .env block** and paste it into `.env`.

If generation fails:

- Read the error shown above the output.
- Click **Copy logs** to copy the diagnostic log.
- Click **Export state JSON** to save all current fields, including incomplete values.
- Share the logs and state only through a secure channel. Redact access tokens before sharing.

The tool generates these environment variables:

```text
PLAID_ENV=...
PLAID_CLIENT_ID=...
PLAID_SECRET=...
PLAID_ITEMS_JSON=...
FIREFLY_URL=...
FIREFLY_TOKEN=...
MODE=...
DRY_RUN=...
LOG_LEVEL=...
IMPORTER_DESTINATIONS_JSON=...
IMPORTER_PROFILES_JSON=...
# IMPORT_START_DATE=... (when set)
```

## Load Firefly accounts

Enter `FIREFLY_URL` and `FIREFLY_TOKEN` in **Runtime settings**, then click
**Load Firefly accounts**. The tool requests the Firefly asset accounts and
shows each account's ID, name, and currency in the account selector. Selecting
an account fills its Firefly account ID and currency code. The Firefly instance
must allow browser requests from the page's origin; otherwise the browser's
CORS policy will block the lookup.

## Load Plaid accounts

Enter `PLAID_ENV`, `PLAID_CLIENT_ID`, and `PLAID_SECRET` in **Runtime
settings**, then start the local proxy from the repository root:

```powershell
python tools/account_lookup_proxy.py
```

Enter or load a bank's Plaid access token and click **Load Plaid accounts** on
that bank card. The browser sends the credentials to the localhost proxy; the
proxy sends the request to Plaid over HTTPS and returns only account metadata.
The tool shows each Plaid account ID, name, and currency. Selecting an account
fills the Plaid account ID and currency code. Keep the page and proxy private.

For the runtime and Compose volume setup, see the repository [README](../README.md) and the configuration comments in [`../.env.example`](../.env.example).
