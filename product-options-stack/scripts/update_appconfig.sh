#!/usr/bin/env bash
# update_appconfig.sh — Reads CloudFormation outputs and deploys an AppConfig
# configuration for FMX product-options recommendations.
#
# Required environment variables:
#   STAGE               — dev | prd
#   SUFFIX              — suffix of the deployed fmx-data-stores stack
#   VERSION             — embedding model version, e.g. v9/20260505
#                         The version prefix (e.g. "v9") is extracted and formatted
#                         as "fmX_v9" for the model_version field in AppConfig.
#   AWS_DEFAULT_REGION  — AWS region
# Optional environment variables:
#   POPULAR_OPTIONS_LIMIT — number of popular options to surface (default: 5)
#
# Reads from CloudFormation stacks:
#   aws-personalize-score-lambda-{STAGE}  — AppConfig IDs
#   fmx-data-stores-{STAGE}-{SUFFIX}      — DynamoDB table names + OpenSearch index name
#   opensearch-infrastructure-{STAGE}     — OpenSearch endpoint
#
# Reads meta_split_calendars from:
#   Snowflake: DNA.PERSONALIZATION.CONF_META_SPLIT_CALENDAR (via scripts/fetch_meta_split_calendar.py)

set -euo pipefail

# ── Validate inputs ──────────────────────────────────────────────────────────

# Guard against unexpanded GitLab variable references
[[ "${SUFFIX:-}"               == \$* ]] && SUFFIX=""
[[ "${VERSION:-}"              == \$* ]] && VERSION=""
[[ "${POPULAR_OPTIONS_LIMIT:-}" == \$* ]] && POPULAR_OPTIONS_LIMIT=""

if [[ -z "${SUFFIX:-}" ]]; then
  echo "❌ ERROR: SUFFIX is required to look up the fmx-data-stores stack."
  exit 1
fi

if [[ -z "${VERSION:-}" ]]; then
  echo "❌ ERROR: VERSION is required (e.g. v9/20260505) to derive the model version."
  exit 1
fi

# Extract "v9" from "v9/20260505", then format as "fmX_v9"
VERSION_TAG=$(echo "${VERSION}" | cut -d'/' -f1)
MODEL_VERSION="fmX_${VERSION_TAG}"
echo "  VERSION          = $VERSION"
echo "  MODEL_VERSION    = $MODEL_VERSION"

POPULAR_OPTIONS_LIMIT="${POPULAR_OPTIONS_LIMIT:-5}"
echo "  POPULAR_OPTIONS_LIMIT = $POPULAR_OPTIONS_LIMIT"

SCORING_STACK="aws-personalize-score-lambda-${STAGE}"
DATA_STORES_STACK="fmx-data-stores-${STAGE}-${SUFFIX}"
OPENSEARCH_STACK="opensearch-infrastructure-${STAGE}"

# ── Helper ───────────────────────────────────────────────────────────────────

get_cf_output() {
  local stack="$1" key="$2"
  aws cloudformation describe-stacks \
    --stack-name "$stack" \
    --region "$AWS_DEFAULT_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text
}

# ── Fetch AppConfig IDs from scoring stack ───────────────────────────────────

echo "Fetching AppConfig IDs from: $SCORING_STACK"
APP_ID=$(get_cf_output "$SCORING_STACK" AppConfigApplicationId)
ENV_ID=$(get_cf_output "$SCORING_STACK" AppConfigEnvironmentId)
PROFILE_ID=$(get_cf_output "$SCORING_STACK" AppConfigFmxOptionsProfileId)
STRATEGY_ID=$(get_cf_output "$SCORING_STACK" AppConfigDeploymentStrategyId)

echo "  APP_ID      = $APP_ID"
echo "  ENV_ID      = $ENV_ID"
echo "  PROFILE_ID  = $PROFILE_ID"
echo "  STRATEGY_ID = $STRATEGY_ID"

if [[ -z "$APP_ID" || -z "$ENV_ID" || -z "$PROFILE_ID" || -z "$STRATEGY_ID" ]]; then
  echo "❌ ERROR: One or more AppConfig IDs could not be resolved from $SCORING_STACK outputs."
  exit 1
fi

# ── Fetch table / index names from data-stores stack ────────────────────────

echo "Fetching table/index names from: $DATA_STORES_STACK"
USER_TABLE=$(get_cf_output "$DATA_STORES_STACK" UserEmbeddingsTableName)
ITEM_TABLE=$(get_cf_output "$DATA_STORES_STACK" ItemEmbeddingsTableName)
OS_INDEX=$(get_cf_output "$DATA_STORES_STACK"   OpenSearchIndexName)

echo "Fetching OpenSearch endpoint from: $OPENSEARCH_STACK"
OS_ENDPOINT=$(get_cf_output "$OPENSEARCH_STACK" OpenSearchEndpoint)

echo "  USER_TABLE  = $USER_TABLE"
echo "  ITEM_TABLE  = $ITEM_TABLE"
echo "  OS_INDEX    = $OS_INDEX"
echo "  OS_ENDPOINT = $OS_ENDPOINT"

MISSING=""
[[ -z "$USER_TABLE"   ]] && MISSING="$MISSING UserEmbeddingsTableName"
[[ -z "$ITEM_TABLE"   ]] && MISSING="$MISSING ItemEmbeddingsTableName"
[[ -z "$OS_INDEX"     ]] && MISSING="$MISSING OpenSearchIndexName"
[[ -z "$OS_ENDPOINT"  ]] && MISSING="$MISSING OpenSearchEndpoint (from $OPENSEARCH_STACK)"

if [[ -n "$MISSING" ]]; then
  echo "❌ ERROR: Could not resolve outputs:$MISSING"
  exit 1
fi

# ── Fetch meta_split_calendars from Snowflake ────────────────────────────────

echo "Fetching meta_split_calendars from Snowflake..."
META_SPLIT_CALENDARS=$(python3 scripts/fetch_meta_split_calendar.py)
echo "  meta_split_calendars fetched ($(echo "$META_SPLIT_CALENDARS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(d.keys()))") countries)"

# ── Build config JSON ────────────────────────────────────────────────────────

CONFIG_JSON=$(python3 - <<PYEOF
import json, sys

infrastructure = {
    "opensearch": {
        "region": "${AWS_DEFAULT_REGION}",
        "endpoint": "${OS_ENDPOINT}",
        "index_name": "${OS_INDEX}",
    },
    "dynamodb": {
        "user_embedding_table": "${USER_TABLE}",
        "item_embedding_table": "${ITEM_TABLE}",
    },
}

recommendation = {
    "model_version": "${MODEL_VERSION}",
    "popular_options_limit": ${POPULAR_OPTIONS_LIMIT},
    "meta_split_calendars": json.loads(r"""${META_SPLIT_CALENDARS}"""),
}

config = {"infrastructure": infrastructure, "recommendation": recommendation}
print(json.dumps(config, indent=2))
PYEOF
)

echo "$CONFIG_JSON" > fmx_appconfig.json
echo "Config to deploy:"; cat fmx_appconfig.json

# ── Create hosted configuration version ─────────────────────────────────────

TMPFILE=$(mktemp)
VERSION_NUMBER=$(aws appconfig create-hosted-configuration-version \
  --application-id "$APP_ID" \
  --configuration-profile-id "$PROFILE_ID" \
  --content-type "application/json" \
  --content fileb://fmx_appconfig.json \
  --region "$AWS_DEFAULT_REGION" \
  --query "VersionNumber" \
  --output text \
  "$TMPFILE")
rm -f "$TMPFILE"

echo "Created hosted configuration version: $VERSION_NUMBER"

# ── Start deployment ─────────────────────────────────────────────────────────

aws appconfig start-deployment \
  --application-id "$APP_ID" \
  --environment-id "$ENV_ID" \
  --deployment-strategy-id "$STRATEGY_ID" \
  --configuration-profile-id "$PROFILE_ID" \
  --configuration-version "$VERSION_NUMBER" \
  --description "Deployed from product-options CI — pipeline ${CI_PIPELINE_ID:-local}" \
  --region "$AWS_DEFAULT_REGION"

echo "✓ AppConfig deployment started for FMX options recommendations ($STAGE)"
