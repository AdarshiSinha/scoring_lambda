"""
wait_for_ingestion.py
─────────────────────
Counts expected rows from Parquet files in S3, then polls an OpenSearch index
until the document count reaches that expected total. Intended to be called
from CI after the OSIS ingestion pipeline has started.

Required environment variables
───────────────────────────────
  OS_ENDPOINT          OpenSearch domain endpoint (without https://)
  INDEX_NAME           Target OpenSearch index name
  OSIS_PIPELINE_NAME   OSIS pipeline name (CloudFormation output OsisPipelineName).
                       Used to call aws osis get-pipeline and extract the S3 source.
  AWS_DEFAULT_REGION   AWS region (default: eu-west-1)

Optional environment variables
───────────────────────────────
  MAX_WAIT_SECONDS     Total timeout in seconds       (default: 3600 = 1 h)
  POLL_INTERVAL        Seconds between polls          (default: 60)
"""

import os
import sys
import time
from typing import Any

import boto3
import pyarrow.parquet as pq
import requests
import yaml
from pyarrow import fs
from requests_aws4auth import AWS4Auth


def resolve_parquet_s3_uri(region: str) -> tuple[str, str]:
    """Query the OSIS pipeline resource and return (bucket, prefix).

    Calls ``osis:GetPipeline``, parses the ``PipelineConfigurationBody`` YAML
    into a dict, then navigates:
      <pipeline_name>.source.s3.scan.buckets[0].bucket.name
      <pipeline_name>.source.s3.scan.buckets[0].bucket.filter.include_prefix[0]
    """
    pipeline_name = os.environ.get("OSIS_PIPELINE_NAME", "").strip()
    if not pipeline_name:
        raise RuntimeError("OSIS_PIPELINE_NAME env var is required but not set")

    osis_client = boto3.client("osis", region_name=region)
    resp = osis_client.get_pipeline(PipelineName=pipeline_name)
    config_body: str = resp["Pipeline"]["PipelineConfigurationBody"]
    config: dict = yaml.safe_load(config_body)

    try:
        bucket_cfg = config[pipeline_name]["source"]["s3"]["scan"]["buckets"][0]["bucket"]
        bucket: str = bucket_cfg["name"]
        prefix: str = bucket_cfg["filter"]["include_prefix"][0].lstrip("/")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Unexpected OSIS pipeline config structure for '{pipeline_name}': {exc}"
        ) from exc

    return bucket, prefix


def count_rows_in_prefix(
    s3_client: Any,
    s3_fs: fs.S3FileSystem,
    bucket: str,
    prefix: str,
) -> tuple[int, int]:
    paginator = s3_client.get_paginator("list_objects_v2")
    total_rows = 0
    parquet_files = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet"):
                continue
            parquet_files += 1
            path = f"{bucket}/{key}"
            try:
                metadata = pq.read_metadata(path, filesystem=s3_fs)
                total_rows += metadata.num_rows
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Failed reading Parquet metadata for s3://{path}: {exc}")

    return total_rows, parquet_files


def expected_docs_from_s3(region: str, bucket: str, prefix: str) -> int:
    session = boto3.Session(region_name=region)
    s3_client = session.client("s3")
    s3_fs = fs.S3FileSystem(region=region)
    rows, file_count = count_rows_in_prefix(s3_client, s3_fs, bucket, prefix)
    print(
        f"  Source s3://{bucket}/{prefix}: {rows:,} rows from {file_count} parquet file(s)",
        flush=True,
    )
    return rows


def main() -> None:
    region = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
    endpoint = os.environ["OS_ENDPOINT"]
    index_name = os.environ["INDEX_NAME"]
    bucket, prefix = resolve_parquet_s3_uri(region)
    print(f"OSIS ingestion source: s3://{bucket}/{prefix}", flush=True)
    expected_docs = expected_docs_from_s3(region, bucket, prefix)
    if expected_docs <= 0:
        raise RuntimeError("Expected row count from S3 is 0; cannot validate ingestion")

    max_wait = int(os.environ.get("MAX_WAIT_SECONDS", "3600"))
    interval = int(os.environ.get("POLL_INTERVAL", "60"))

    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()
    auth = AWS4Auth(
        creds.access_key,
        creds.secret_key,
        region,
        "es",
        session_token=creds.token,
    )

    url = f"https://{endpoint}/{index_name}/_count"
    elapsed = 0

    print(
        f"Polling {url} every {interval}s "
        f"(timeout {max_wait}s, target >= {expected_docs:,} docs)",
        flush=True,
    )

    while elapsed < max_wait:
        try:
            resp = requests.get(url, auth=auth, timeout=30)
            resp.raise_for_status()
            count = resp.json().get("count", 0)
            print(
                f"  [{elapsed}s] Document count: {count:,} (need >= {expected_docs:,})",
                flush=True,
            )
            if count >= expected_docs:
                print(
                    f"✓ OpenSearch document count requirement met: "
                    f"{count:,} >= {expected_docs:,}"
                )
                sys.exit(0)
        except Exception as exc:  # noqa: BLE001
            print(
                f"  [{elapsed}s] Warning: could not query OpenSearch — {exc}",
                flush=True,
            )

        time.sleep(interval)
        elapsed += interval

    print(
        f"❌ Timeout: document count never reached {expected_docs:,} after {max_wait}s"
    )
    sys.exit(1)


if __name__ == "__main__":
    main()

