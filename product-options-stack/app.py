#!/usr/bin/env python3
"""
OpenSearch Infrastructure CDK App
Entry point for CDK application
"""
import json
import os
from aws_cdk import App, Environment, Tags
from opensearch_stack.opensearch_stack import OpenSearchStack
from opensearch_stack.data_stores_stack import DataStoresStack

def tag_stack(stk, stage: str) -> None:
    Tags.of(stk).add("Project", "aws-personalize")
    Tags.of(stk).add("Owner", "product-recommender")
    Tags.of(stk).add("CostCenter", "recommendation-model")
    Tags.of(stk).add("Environment", stage)
    Tags.of(stk).add("ManagedBy", "CDK")


app = App()

# Get stage from context (dev or prd)
stage = app.node.try_get_context("stage") or "dev"

stack_suffix = app.node.try_get_context("suffix")

account = os.environ.get("CDK_DEFAULT_ACCOUNT")
region = app.node.try_get_context("region") or "eu-west-1"

env = Environment(account=account, region=region)

config_raw = (app.node.try_get_context("config") or "").strip()
config_override: dict = json.loads(config_raw) if config_raw else {}

# The scoring pipeline will push to s3 location of the following format:
# s3://{s3_bucket_prefix}-{stage}/fmX/{version}/users_embeddings_dynamodb_json/
# s3://{s3_bucket_prefix}-{stage}/fmX/{version}/items_embeddings_dynamodb_json/
# s3://{s3_bucket_prefix}-{stage}/fmX/{version}/options_embeddings_opensearch_json/
# Version is the only thing that changes per deployment — pass via CI:
#   cdk deploy --context version=v1
version: str = app.node.try_get_context("version") or "v0"

# Build the resolved config — can be fully overridden by passing --context config='{"s3_embedding_store":...}'
if config_override:
    # Full config supplied via context (e.g. from CI), use as-is
    config: dict = config_override
else:
    # Derive defaults: bucket name = <prefix>-<stage>, paths contain version
    s3_bucket_prefix: str = app.node.try_get_context("s3_bucket_prefix") or "fmx-precs-product-recommendation"
    index_name_prefix: str = app.node.try_get_context("ingestion_index_name_prefix") or "fmx-option-index"
    config = {
        "s3_embedding_store": f"{s3_bucket_prefix}-{stage}",
        "user_s3_prefix": f"fmX/{version}/users_embeddings_dynamodb_json/",
        "item_s3_prefix": f"fmX/{version}/items_embeddings_dynamodb_json/",
        "ingestion": {
            "option_s3_prefix": f"fmX/{version}/options_embeddings_opensearch_json/",
            "index_name_prefix": index_name_prefix,
        },
    }

access_roles: list[str] = app.node.try_get_context("opensearch_access_roles") or []

# ── Stack 1: OpenSearch domain ───────────────────────────────
opensearch_stack = OpenSearchStack(
    app,
    f"opensearch-infrastructure-{stage}",
    stage=stage,
    access_roles=access_roles,
    env=env,
    description=f"OpenSearch infrastructure for option embeddings ({stage})",
)


# ── Stack 2: Data stores for fmx ──────────────────────────────────────
# Only synthesised when a suffix AND the minimum required config keys are
# present.  This lets `cdk deploy opensearch-infrastructure-<stage>` run
# without providing data-store context.
if stack_suffix and config.get("s3_embedding_store") and config.get("user_s3_prefix"):
    data_stores_stack = DataStoresStack(
        app,
        f"fmx-data-stores-{stage}-{stack_suffix}",
        config=config,
        domain=opensearch_stack.domain,
        s3_embedding_store=config.get("s3_embedding_store"),
        stage=stage,
        suffix=stack_suffix,
        env=env,
        description=f"DynamoDB embedding tables ({stage})",
    )

    # ── Tags ─────────────────────────────────────────────────────────────────────
    for stk in [opensearch_stack, data_stores_stack]:
        tag_stack(stk, stage)
else:
    tag_stack(opensearch_stack, stage)

app.synth()
