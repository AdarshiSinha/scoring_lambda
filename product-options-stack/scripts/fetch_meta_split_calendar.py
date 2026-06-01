#!/usr/bin/env python3
"""fetch_meta_split_calendar.py — Fetches product_options rows from
DNA.PERSONALIZATION.CONF_META_SPLIT_CALENDAR in Snowflake and writes a
meta_split_calendars JSON object to stdout, keyed by country.

Authentication:
  Reads Cimpress OAuth client_id and client_secret from AWS Secrets Manager
  at the secret ARN defined by SNOWFLAKE_SECRET_ARN (or the default below).
  AWS credentials are sourced from the ambient environment (OIDC role in CI).

Required environment variables:
  AWS_DEFAULT_REGION  — AWS region for Secrets Manager lookup

Optional environment variables:
  SNOWFLAKE_SECRET_ARN — override the default Secrets Manager ARN
  SNOWFLAKE_ROLE       — override the default Snowflake role
  SNOWFLAKE_WAREHOUSE  — override the default Snowflake warehouse (default: DATA_PORTAL)

Output:
  JSON object written to stdout, e.g.:
  {
    "CA": { "use_case": "product_options", "meta_name": "...", ... },
    "US": { "use_case": "product_options", "meta_name": "...", ... }
  }

  Exits non-zero on any error (secret lookup, Snowflake connection, no rows).
"""

import json
import os
import sys

import boto3
import snowflake.connector
from dpp.connect import get_snowflake_options

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_SECRET_ARN = (
    "arn:aws:secretsmanager:eu-west-1:905666450942:"
    "secret:recommendations-api-tracking-oauth-v2-prd-yr91TP"
)
DEFAULT_SNOWFLAKE_ROLE = "ADMIN_46F619D0_8CF2_4EC5_9711_6F543A1BF0FB"
DEFAULT_SNOWFLAKE_WAREHOUSE = "DATA_PORTAL"
SNOWFLAKE_DATABASE = "DNA"
SNOWFLAKE_SCHEMA = "PERSONALIZATION"
SNOWFLAKE_TABLE = "DNA.PERSONALIZATION.CONF_META_SPLIT_CALENDAR"
USE_CASE_FILTER = "product_options"

QUERY = f"""
    SELECT
        USE_CASE,
        COUNTRY,
        META_NAME,
        TEST_PERCENT,
        PROD_MODEL_PERCENT,
        META_SPLIT,
        TEST_POLICY,
        START_DATE,
        END_DATE,
        META_RESHUFFLE_FREQUENCY_DAYS,
        META_TEST_RESHUFFLE_FREQUENCY_DAYS
    FROM {SNOWFLAKE_TABLE}
    WHERE USE_CASE = '{USE_CASE_FILTER}'
      AND CURRENT_DATE() BETWEEN START_DATE AND END_DATE
    ORDER BY COUNTRY
"""


def _err(msg: str) -> None:
    print(f"❌ ERROR: {msg}", file=sys.stderr)


def fetch_secret(secret_arn: str, region: str) -> dict:
    """Retrieve the secret JSON from AWS Secrets Manager."""
    try:
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_arn)
        return json.loads(response["SecretString"])
    except Exception as exc:
        _err(f"Failed to retrieve secret '{secret_arn}': {exc}")
        sys.exit(1)


def build_entry(row: dict) -> dict:
    """Map a Snowflake row dict to the meta_split_calendars entry shape."""

    def _parse_json_field(value, field_name: str) -> dict:
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            _err(f"Could not parse '{field_name}' as JSON: {value!r} — {exc}")
            sys.exit(1)

    def _to_date_str(value) -> str:
        # Snowflake DATE columns come back as datetime.date objects
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return {
        "use_case": row["USE_CASE"],
        "meta_name": row["META_NAME"],
        "test_percent": float(row["TEST_PERCENT"]),
        "prod_model_percent": float(row["PROD_MODEL_PERCENT"]),
        "meta_split": _parse_json_field(row["META_SPLIT"], "META_SPLIT"),
        "test_policy": _parse_json_field(row["TEST_POLICY"], "TEST_POLICY"),
        "start_date": _to_date_str(row["START_DATE"]),
        "end_date": _to_date_str(row["END_DATE"]),
        "meta_reshuffle_frequency_days": int(row["META_RESHUFFLE_FREQUENCY_DAYS"]),
        "meta_test_reshuffle_frequency_days": int(
            row["META_TEST_RESHUFFLE_FREQUENCY_DAYS"]
        ),
    }


def main() -> None:
    region = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
    secret_arn = os.environ.get("SNOWFLAKE_SECRET_ARN", DEFAULT_SECRET_ARN)
    role = os.environ.get("SNOWFLAKE_ROLE", DEFAULT_SNOWFLAKE_ROLE)
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", DEFAULT_SNOWFLAKE_WAREHOUSE)

    # ── 1. Fetch credentials from Secrets Manager ──────────────────────────
    print(f"Fetching Snowflake credentials from Secrets Manager...", file=sys.stderr)
    secret = fetch_secret(secret_arn, region)

    client_id = secret.get("id")
    client_secret = secret.get("secret")

    if not client_id or not client_secret:
        _err(
            f"Secret '{secret_arn}' must contain 'id' and 'secret' keys. "
            f"Found keys: {list(secret.keys())}"
        )
        sys.exit(1)

    # ── 2. Build Snowflake connection options via dpp ──────────────────────
    print(
        f"Building Snowflake connection (database={SNOWFLAKE_DATABASE}, "
        f"schema={SNOWFLAKE_SCHEMA}, role={role}, warehouse={warehouse})...",
        file=sys.stderr,
    )
    try:
        sf_options = get_snowflake_options(
            SNOWFLAKE_DATABASE,
            schema=SNOWFLAKE_SCHEMA,
            role=role,
            warehouse=warehouse,
            client_id=client_id,
            client_secret=client_secret,
        )
    except Exception as exc:
        _err(f"dpp.get_snowflake_options failed: {exc}")
        sys.exit(1)

    # ── 3. Connect and query ───────────────────────────────────────────────
    print(f"Connecting to Snowflake...", file=sys.stderr)
    try:
        conn = snowflake.connector.connect(**sf_options)
    except Exception as exc:
        _err(f"Snowflake connection failed: {exc}")
        sys.exit(1)

    try:
        cursor = conn.cursor(snowflake.connector.DictCursor)
        print(
            f"Querying {SNOWFLAKE_TABLE} for use_case='{USE_CASE_FILTER}'...",
            file=sys.stderr,
        )
        cursor.execute(QUERY)
        rows = cursor.fetchall()
    except Exception as exc:
        _err(f"Snowflake query failed: {exc}")
        sys.exit(1)
    finally:
        conn.close()

    # ── 4. Validate rows ───────────────────────────────────────────────────
    if not rows:
        _err(
            f"No rows found in {SNOWFLAKE_TABLE} for use_case='{USE_CASE_FILTER}'. "
            "Ensure data has been loaded before running update-appconfig."
        )
        sys.exit(1)

    print(f"  Found {len(rows)} row(s) for use_case='{USE_CASE_FILTER}'.", file=sys.stderr)

    # ── 5. Transform rows → meta_split_calendars dict ─────────────────────
    meta_split_calendars: dict = {}
    for row in rows:
        country = row["COUNTRY"]
        if country in meta_split_calendars:
            print(
                f"  ⚠️  WARNING: duplicate country '{country}' — using last row "
                f"(meta_name={row['META_NAME']}).",
                file=sys.stderr,
            )
        meta_split_calendars[country] = build_entry(row)

    # ── 6. Write to stdout ─────────────────────────────────────────────────
    print(json.dumps(meta_split_calendars))


if __name__ == "__main__":
    main()
