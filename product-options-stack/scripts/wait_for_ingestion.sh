#!/usr/bin/env bash
# wait_for_ingestion.sh
# ─────────────────────
# Waits for a fmx-data-stores deployment to be fully ingested before allowing
# the old stack to be destroyed.
#
# Phase 1 — polls DynamoDB ImportTable status for both the user and item tables
#            until both reach COMPLETED (exits 1 on FAILED / CANCELLED).
# Phase 2 — computes expected rows from source Parquet files in S3, then polls
#            the OpenSearch _count endpoint until the index reaches that total.
#
# Required environment variables
# ───────────────────────────────
#   SUFFIX              Deployment suffix, e.g. 20260413-hotfix
#   STAGE               Deployment stage: dev | prd
#   AWS_DEFAULT_REGION  AWS region
#
# Optional environment variables
# ───────────────────────────────
#   MAX_DDB_WAIT      DynamoDB polling timeout in seconds (default: 7200 = 2 h)
#   DDB_INTERVAL      DynamoDB poll interval in seconds   (default: 60)
#   MAX_WAIT_SECONDS  OpenSearch polling timeout           (default: 7200)
#   POLL_INTERVAL     OpenSearch poll interval             (default: 60)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Validate required variables ───────────────────────────────────────────────
# SUFFIX / INGESTION_CONFIG come from the pipeline-level variable when set at
# "Run pipeline" time.  When triggered via job-level override they are not
# propagated automatically; the deploy job captures them in a dotenv artifact
# (DEPLOY_SUFFIX / DEPLOY_INGESTION_CONFIG) which GitLab injects here via
# needs: artifacts: true.
# Guard against unexpanded GitLab variable references (e.g. literal '$SUFFIX' or '$DEPLOY_SUFFIX')
[[ "${SUFFIX:-}"        == \$* ]] && SUFFIX=""
[[ "${DEPLOY_SUFFIX:-}" == \$* ]] && DEPLOY_SUFFIX=""
SUFFIX="${SUFFIX:-${DEPLOY_SUFFIX:-}}"

: "${SUFFIX:?SUFFIX is empty — set it via Run pipeline → Variables or it will be forwarded automatically from the deploy job artifact}"
: "${STAGE:?STAGE env var is required}"
: "${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION env var is required}"

STACK_NAME="fmx-data-stores-${STAGE}-${SUFFIX}"
OS_STACK="opensearch-infrastructure-${STAGE}"

echo "=== Waiting for ${STACK_NAME} data ingestion to complete ==="

# ── Retrieve CloudFormation outputs ───────────────────────────────────────────
cfn_output() {
  local stack="$1" key="$2"
  aws cloudformation describe-stacks \
    --stack-name  "$stack" \
    --query       "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output      text \
    --region      "$AWS_DEFAULT_REGION"
}

USER_TABLE=$(cfn_output "$STACK_NAME" "UserEmbeddingsTableName")
ITEM_TABLE=$(cfn_output "$STACK_NAME" "ItemEmbeddingsTableName")
INDEX_NAME=$(cfn_output "$STACK_NAME" "OpenSearchIndexName")
OS_ENDPOINT=$(cfn_output "$OS_STACK"  "OpenSearchEndpoint")
OSIS_PIPELINE_NAME=$(cfn_output "$STACK_NAME" "OsisPipelineName")

if [ -z "$OSIS_PIPELINE_NAME" ]; then
  echo "❌ OsisPipelineName CloudFormation output is empty — was the OSIS pipeline created?"
  exit 1
fi

echo "User table:          ${USER_TABLE}"
echo "Item table:          ${ITEM_TABLE}"
echo "OpenSearch index:    ${INDEX_NAME}"
echo "OpenSearch endpoint: ${OS_ENDPOINT}"
echo "OSIS pipeline:       ${OSIS_PIPELINE_NAME}"

# ── Phase 1: DynamoDB import status ───────────────────────────────────────────
echo ""
echo "--- Phase 1: Waiting for DynamoDB imports to complete ---"

ddb_import_arn() {
  local table="$1"
  local table_arn
  table_arn=$(aws dynamodb describe-table \
    --table-name "$table" \
    --query "Table.TableArn" \
    --output text \
    --region "$AWS_DEFAULT_REGION")
  aws dynamodb list-imports \
    --table-arn "$table_arn" \
    --query "ImportSummaryList[0].ImportArn" \
    --output text \
    --region "$AWS_DEFAULT_REGION"
}

USER_IMPORT_ARN=$(ddb_import_arn "$USER_TABLE")
ITEM_IMPORT_ARN=$(ddb_import_arn "$ITEM_TABLE")

echo "User import ARN: ${USER_IMPORT_ARN}"
echo "Item import ARN: ${ITEM_IMPORT_ARN}"

MAX_DDB_WAIT="${MAX_DDB_WAIT:-7200}"
DDB_INTERVAL="${DDB_INTERVAL:-60}"
ddb_elapsed=0

while [ "$ddb_elapsed" -lt "$MAX_DDB_WAIT" ]; do
  USER_STATUS=$(aws dynamodb describe-import \
    --import-arn "$USER_IMPORT_ARN" \
    --query "ImportTableDescription.ImportStatus" \
    --output text \
    --region "$AWS_DEFAULT_REGION")
  ITEM_STATUS=$(aws dynamodb describe-import \
    --import-arn "$ITEM_IMPORT_ARN" \
    --query "ImportTableDescription.ImportStatus" \
    --output text \
    --region "$AWS_DEFAULT_REGION")

  echo "  [${ddb_elapsed}s] User: ${USER_STATUS} | Item: ${ITEM_STATUS}"

  case "$USER_STATUS" in
    FAILED|CANCELLED|CANCELLING)
      echo "❌ User table import ended with status: ${USER_STATUS}"; exit 1 ;;
  esac
  case "$ITEM_STATUS" in
    FAILED|CANCELLED|CANCELLING)
      echo "❌ Item table import ended with status: ${ITEM_STATUS}"; exit 1 ;;
  esac

  if [ "$USER_STATUS" = "COMPLETED" ] && [ "$ITEM_STATUS" = "COMPLETED" ]; then
    echo "✓ Both DynamoDB imports completed successfully"
    break
  fi

  sleep "$DDB_INTERVAL"
  ddb_elapsed=$(( ddb_elapsed + DDB_INTERVAL ))
done

if [ "$ddb_elapsed" -ge "$MAX_DDB_WAIT" ]; then
  echo "❌ Timed out waiting for DynamoDB imports after ${MAX_DDB_WAIT}s"
  exit 1
fi

# ── Phase 2: OpenSearch document count ────────────────────────────────────────
echo ""
echo "--- Phase 2: Waiting for OpenSearch document count to match S3 parquet rows ---"

export OS_ENDPOINT INDEX_NAME OSIS_PIPELINE_NAME
python3 "${SCRIPT_DIR}/wait_for_ingestion.py"

