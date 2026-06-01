include:
  - project: 'vistaprint-org/internal-open-source/shared-templates/gitlab-ci'
    ref: main
    file: '/aws-oidc-auth/.aws-oidc-auth.yml'

image: $AWS_RUNTIME

default:
  tags:
    - ballista-francolin

# ── Shared variables forwarded to both child pipelines ───────────────────────
variables:
  AWS_DEFAULT_REGION: eu-west-1
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"
  AWS_ACCOUNT_ID: '905666450942'
  AWS_ROLE_ARN: arn:aws:iam::905666450942:role/product-options-role
  VALIDATE_SUFFIX: 'ci-suffix'

  # ── OpenSearch pipeline variables ────────────────────────────────────────
  # (only relevant when RUN_PIPELINE=opensearch or trigger:opensearch is used)

  # ── FMX data pipeline variables ──────────────────────────────────────
  SUFFIX:
    value: ""
    description: "Deployment suffix for the fmx-data-stores stack, e.g. 20260519. REQUIRED for fmx-data pipeline."
  VERSION:
    value: ""
    description: >-
      REQUIRED for fmx-data and update-appconfig pipelines. Embedding model version in format v{N}/{YYYYMMDD}, e.g. v9/20260519.
      Controls all S3 prefix paths (fmX/v9/...) and the model_version in AppConfig (fmX_v9). Used unless CONFIG is also provided.
  CONFIG:
    value: ""
    description: >-
      OPTIONAL override. Full CDK config JSON — overrides VERSION-derived defaults entirely. e.g.:
      {"s3_embedding_store":"my-bucket","user_s3_prefix":"fmX/v9/users_embeddings_dynamodb_json/","item_s3_prefix":"fmX/v9/items_embeddings_dynamodb_json/","ingestion":{"option_s3_prefix":"fmX/v9/options_embeddings_opensearch_json/","index_name_prefix":"fmx-option-index"}}
  DESTROY_STACK_SUFFIX:
    value: ""
    description: "Suffix of the fmx-data-stores stack to destroy. REQUIRED for destroy jobs in fmx-data pipeline."
  POPULAR_OPTIONS_LIMIT:
    value: "5"
    description: "OPTIONAL. Number of popular options to surface in AppConfig recommendations. Defaults to 5."
  TARGET_ENV:
    value: ""
    description: "Set to 'dev' or 'prd' to run only that environment's jobs in the fmx-data pipeline. Leave blank to run both."

  # ── Pipeline selector (for API-triggered runs) ────────────────────────────
  # Pass RUN_PIPELINE=opensearch        → triggers only the OpenSearch pipeline
  # Pass RUN_PIPELINE=fmx-data         → triggers the full FMX data pipeline
  # Pass RUN_PIPELINE=update-appconfig → triggers ci/update-appconfig.gitlab-ci.yml (appconfig only, no infra jobs)
  # Leave blank                        → all trigger jobs appear as manual buttons in the UI
  RUN_PIPELINE:
    value: ""
    description: "Set to 'opensearch', 'fmx-data', or 'update-appconfig' to trigger a specific child pipeline via API. Leave blank for manual UI use."

stages:
  - trigger

# ── OpenSearch infrastructure pipeline ───────────────────────────────────────
# One-time / on-demand: provisions or destroys the shared OpenSearch cluster.
# Trigger via UI (manual button) or API with RUN_PIPELINE=opensearch.
trigger:opensearch:
  stage: trigger
  trigger:
    include: ci/opensearch.gitlab-ci.yml
    strategy: depend   # parent job status mirrors child pipeline result
  variables:
    PARENT_PIPELINE_ID: $CI_PIPELINE_ID
  rules:
    - if: '$RUN_PIPELINE == "opensearch"'   # API-triggered
      when: always
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
      when: manual                           # MR — manual dev button
      variables:
        STAGE: dev
    - if: '$CI_COMMIT_BRANCH == "main" && $RUN_PIPELINE == ""'
      when: manual                           # UI manual button
    - when: never

# ── FMX data pipeline ─────────────────────────────────────────────────────────
# Bi-weekly: deploy fmx-data-stores → wait for ingestion → update AppConfig → destroy old stack.
# Trigger via UI (manual button) or API with RUN_PIPELINE=fmx-data.
trigger:fmx-data:
  stage: trigger
  trigger:
    include: ci/fmx-data.gitlab-ci.yml
    strategy: depend
  variables:
    PARENT_PIPELINE_ID: $CI_PIPELINE_ID
    SUFFIX: $SUFFIX
    VERSION: $VERSION
    CONFIG: $CONFIG
    DESTROY_STACK_SUFFIX: $DESTROY_STACK_SUFFIX
    POPULAR_OPTIONS_LIMIT: $POPULAR_OPTIONS_LIMIT
    TARGET_ENV: $TARGET_ENV
  rules:
    - if: '$RUN_PIPELINE == "fmx-data"'     # API-triggered
      when: always
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
      when: manual                           # MR — manual dev button
      variables:
        STAGE: dev
    - if: '$CI_COMMIT_BRANCH == "main" && $RUN_PIPELINE == ""'
      when: manual                           # UI manual button
    - when: never

# ── Standalone update-appconfig pipeline ─────────────────────────────────────
# Re-publishes AppConfig for an already-deployed data-stores stack.
# Required: SUFFIX (the suffix of the existing stack), STAGE (forwarded to child).
# Trigger via API: RUN_PIPELINE=update-appconfig
trigger:update-appconfig:
  stage: trigger
  trigger:
    include: ci/update-appconfig.gitlab-ci.yml
    strategy: depend
  variables:
    PARENT_PIPELINE_ID: $CI_PIPELINE_ID
    SUFFIX: $SUFFIX
    VERSION: $VERSION
    POPULAR_OPTIONS_LIMIT: $POPULAR_OPTIONS_LIMIT
  rules:
    - if: '$RUN_PIPELINE == "update-appconfig"'   # API-triggered standalone run
      when: always
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
      when: manual                                 # MR — manual dev button
      variables:
        STAGE: dev
    - if: '$CI_COMMIT_BRANCH == "main" && $RUN_PIPELINE == ""'
      when: manual                                 # UI manual button on main
    - when: never
