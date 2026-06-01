OpenSearch Infrastructure - CDK Project
This project manages the AWS OpenSearch infrastructure for option embeddings using AWS CDK (Cloud Development Kit).
Project Structure
product-options-stack/
├── README.md                        # This file
├── QUICKSTART.md                    # Quick reference for common deploy commands
├── .gitlab-ci.yml                   # GitLab CI/CD pipeline definition
├── cdk.json                         # CDK configuration and default context
├── requirements.txt                 # Python dependencies (CDK app)
├── app.py                           # CDK app entry point (two stacks)
├── opensearch_stack/
│   ├── __init__.py
│   ├── opensearch_stack.py          # Stack 1 — OpenSearch domain
│   ├── data_stores_stack.py         # Stack 2 — DynamoDB tables + OSIS pipeline
│   └── index_creator.py             # Reusable construct: creates OpenSearch index via Lambda
├── lambda/
│   └── index_creator/
│       ├── requirements.txt         # Lambda dependencies
│       └── handler.py               # Lambda: creates / deletes the OpenSearch index
└── scripts/
    ├── requirements.txt             # Dependencies for CI ingestion-wait script
    ├── wait_for_ingestion.py        # Polls OSIS pipeline + OpenSearch doc count until ready
    └── wait_for_ingestion.sh        # Shell wrapper: DynamoDB import poller → Python wait script
Stacks



Stack
Name pattern
Purpose




OpenSearchStack
opensearch-infrastructure-{stage}
OpenSearch domain + initial index


DataStoresStack
fmx-data-stores-{stage}-{suffix}
DynamoDB import tables + OpenSearch index + optional OSIS pipeline




Prerequisites

Python 3.9+
AWS CDK CLI: npm install -g aws-cdk
AWS credentials configured
GitLab Runner with AWS access (for CI/CD)

Local Development
Setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

Deploying Stack 1 — OpenSearch Domain
# Dev
cdk deploy opensearch-infrastructure-dev --context stage=dev

# Production
cdk deploy opensearch-infrastructure-prd --context stage=prd

Deploying Stack 2 — Data Stores (DynamoDB + OSIS)
All parameters are controlled via CDK context. The defaults live in cdk.json; override any of them on the command line with --context key=value.
How configuration is resolved
app.py builds the full stack config at synth time using the following priority:

--context config='{...}' — Full JSON override. When supplied, it is used as-is and all other config context keys are ignored.
--context version=<v> — Embedding model version (e.g. v0, v1). The bucket name is derived as {s3_bucket_prefix}-{stage} and all S3 prefix paths are built as fmX/{version}/.... This is the normal path.
cdk.json defaults — version defaults to v0; s3_bucket_prefix defaults to fmx-precs-product-recommendation.

Required context keys



Key
Description
Example




stage
Environment name (dev / prd)
dev


suffix
Unique deployment suffix — appended to all resource names to allow multiple deployments side-by-side
20260410-test2


version
Embedding model version — controls all S3 prefix paths (fmX/{version}/...)
v0



Optional context keys



Key
Description
Default (cdk.json)




s3_bucket_prefix
Prefix of the S3 bucket; stage is appended automatically ({prefix}-{stage})
fmx-precs-product-recommendation


ingestion_index_name_prefix
Prefix for the OpenSearch index name
fmx-option-index


config
Full CDK config JSON — overrides all of the above entirely
—



Deploy with version (recommended)
# Dev — version v0 (uses cdk.json defaults)
cdk deploy fmx-data-stores-dev-20260410-test2 \
  --context stage=dev \
  --context suffix=20260410-test2 \
  --context version=v0

# Production — version v1
cdk deploy fmx-data-stores-prd-20260417 \
  --context stage=prd \
  --context suffix=20260417 \
  --context version=v1
Deploy with full config override (advanced)
Use when you need to point at a non-standard bucket or custom S3 paths:
cdk deploy fmx-data-stores-dev-20260410-test2 \
  --context stage=dev \
  --context suffix=20260410-test2 \
  --context config='{
    "s3_embedding_store": "fmx-precs-product-recommendation-dev",
    "user_s3_prefix": "fmX/v0/users_embeddings_dynamodb_json/",
    "item_s3_prefix": "fmX/v0/items_embeddings_dynamodb_json/",
    "ingestion": {
      "option_s3_prefix": "fmX/v0/options_embeddings_opensearch_json/",
      "index_name_prefix": "fmx-option-index"
    }
  }'
Deploy without OSIS pipeline (DynamoDB tables + index only)
Pass a config without the ingestion block:
cdk deploy fmx-data-stores-dev-20260410-test2 \
  --context stage=dev \
  --context suffix=20260410-test2 \
  --context config='{
    "s3_embedding_store": "fmx-precs-product-recommendation-dev",
    "user_s3_prefix": "fmX/v0/users_embeddings_dynamodb_json/",
    "item_s3_prefix": "fmX/v0/items_embeddings_dynamodb_json/"
  }'
Deploy both stacks at once
cdk deploy --all --context stage=dev --context version=v0

What Gets Deployed
Stack 1 — OpenSearch Domain (opensearch-infrastructure-{stage})

Engine: OpenSearch 2.11
Instance Type: t3.small.search (dev) / r7g.large.search (prd)
Storage: 20 GB gp3 EBS
Security: HTTPS enforced, encryption at rest, fine-grained access control (IAM)
Index Creator Lambda: automatically creates option-embedding-index-{stage} on deploy

Stack 2 — Data Stores (fmx-data-stores-{stage}-{suffix})



Resource
Name
Description




DynamoDB table
user-embeddings-{stage}-{suffix}
Imported from S3 via ImportTable API


DynamoDB table
item-embeddings-{stage}-{suffix}
Imported from S3 via ImportTable API


OpenSearch index
fmx-option-index-{stage}-{suffix}
Created by Lambda custom resource; deleted on cdk destroy


OSIS pipeline
options-{stage}-{suffix}
Ingests Parquet files from S3 → OpenSearch (optional)


EventBridge schedule
osis-stop-options-{stage}-{suffix}
One-time schedule: stops the OSIS pipeline ~30 min after deploy




OSIS Pipeline — Post-Deploy Step
Because the OpenSearch domain uses fine-grained access control, you must map the OSIS pipeline's IAM role as a backend role once after the first deploy:
# Get the role ARN from the stack output
aws cloudformation describe-stacks \
  --stack-name fmx-data-stores-dev-20260410-test2 \
  --query "Stacks[0].Outputs[?OutputKey=='OsisPipelineRoleArn'].OutputValue" \
  --output text

# Map it in OpenSearch (replace <endpoint> and <role-arn>)
curl -XPUT "https://<endpoint>/_plugins/_security/api/rolesmapping/all_access" \
  -H "Content-Type: application/json" \
  -d '{"backend_roles": ["<role-arn>"]}'
Stopping / restarting the pipeline manually
# Stop
aws osis stop-pipeline --pipeline-name options-dev-20260410-test2

# Restart
aws osis start-pipeline --pipeline-name options-dev-20260410-test2
The auto-stop EventBridge schedule fires once ~30 min after deploy and then self-deletes. If you restart the pipeline, re-deploy the stack (or create a new schedule manually) to arm the auto-stop again.

Useful Commands
# List all stacks
cdk list

# Synthesize (validate) without deploying
cdk synth

# Show what will change before deploying
cdk diff fmx-data-stores-dev-20260410-test2

# Deploy OpenSearch domain only
cdk deploy opensearch-infrastructure-dev

# Deploy data stores only
cdk deploy fmx-data-stores-dev-20260410-test2

# Deploy everything
cdk deploy --all

# Destroy data stores stack (also deletes the OpenSearch index)
cdk destroy fmx-data-stores-dev-20260410-test2

# Bootstrap CDK (first time only per account/region)
cdk bootstrap aws://ACCOUNT-ID/eu-west-1

Index Schema
{
  "settings": { "index": { "knn": true } },
  "mappings": {
    "properties": {
      "training_options_std_hash_key": { "type": "keyword" },
      "mpv_id":                         { "type": "keyword" },
      "locale":                         { "type": "keyword" },
      "training_options_std":           { "type": "keyword" },
      "option_embedding": {
        "type": "knn_vector",
        "dimension": 64,
        "method": { "name": "hnsw", "engine": "faiss", "space_type": "innerproduct" }
      }
    }
  }
}

Monitoring
CloudWatch Logs



Log group
Purpose




/aws/lambda/opensearch-index-creator-{stage}-{suffix}
Index creator Lambda (DataStoresStack)


/aws/lambda/opensearch-index-creator-{stage}
Index creator Lambda (OpenSearchStack)


/aws/vendedlogs/OpenSearchIngestion/options-{stage}-{suffix}/compute-logs
OSIS pipeline logs



CloudWatch Metrics (OpenSearch)
Key metrics to watch: ClusterStatus.green/yellow/red, SearchLatency, IndexingLatency, CPUUtilization, JVMMemoryPressure.

Cost Optimization
OSIS pipelines are billed per OCU-hour while ACTIVE. The auto-stop schedule keeps them running for ~30 min per deploy.

Troubleshooting



Symptom
Likely cause
Fix




Domain creation fails
Account limits or IAM permissions
Check service quotas; verify deploying role has es:*


Index creation fails
Domain not yet active
Wait 15–20 min; check Lambda logs


OSIS pipeline fails to write
Lambda / OSIS role not mapped in FGAC
Follow the post-deploy mapping step above


cdk destroy fails on index deletion
OpenSearch unreachable or FGAC mapping missing
Check Lambda logs; map the Lambda role as a backend role


Stack name contains -None-
suffix context not set
Pass --context suffix=<value> or set it in cdk.json




CI/CD Pipeline
Pipeline stages
validate → deploy → wait-for-ingestion → cleanup

All deployment and cleanup jobs are manual. They will not run automatically —
you must click ▶️ on each job in the GitLab pipeline UI after the pipeline is triggered.




Stage
Job
Trigger
Description




validate
validate
automatic on MR / non-main branches
Synths CDK, verifies AWS auth


deploy
deploy:opensearch-dev / deploy:opensearch-prd
manual
Creates/updates the OpenSearch domain


deploy
deploy:fmx-data-stores-dev / deploy:fmx-data-stores-prd
manual
Deploys DynamoDB tables + OSIS pipeline


wait-for-ingestion
wait:fmx-data-stores-dev / wait:fmx-data-stores-prd
auto on_success after deploy
Polls DynamoDB import + OpenSearch doc count


cleanup
destroy:fmx-data-stores-dev / destroy:fmx-data-stores-prd
manual after wait
Destroys the old data-stores stack




Running a deployment from the GitLab UI
Step 1 — Open "Run pipeline"
Go to CI/CD → Pipelines → Run pipeline in the GitLab sidebar.
Select the branch (main for production deployments).
Step 2 — Set required variables
Set the following variables in the Variables section before clicking Run pipeline:



Variable
Required for
Description
Example




SUFFIX
deploy:fmx-data-stores-*
Unique suffix appended to all resource names
20260415-hotfix


VERSION
deploy:fmx-data-stores-*
Embedding model version (e.g. v0, v1)
v0


CONFIG
deploy:fmx-data-stores-*
Full stack config as a JSON string (see examples below)
{"s3_embedding_store":"my-bucket",...}


DESTROY_STACK_SUFFIX
destroy:fmx-data-stores-*
Suffix of the old stack to tear down
20260410-old




CONFIG is forwarded automatically to wait:fmx-data-stores-* via a dotenv artifact — you do not need to set it again for the wait job.

Step 3 — Click "Run pipeline", then trigger jobs manually
Once the pipeline starts, click ▶️ on each job you want to run. Jobs in the deploy stage are independent — you can run deploy:opensearch-dev without deploy:fmx-data-stores-dev.

CONFIG examples
Paste one of the following as the CONFIG value in the GitLab variable field (single-line JSON, no surrounding quotes needed).
With OSIS ingestion pipeline (full deployment):
{"user_table_name":"user-embeddings","item_table_name":"item-embeddings","s3_embedding_store":"fmx-precs-product-recommendation-dev","user_s3_prefix":"dynamo-db-export-for-testing/AWSDynamoDB/01775548616823-03c568a6/data/","item_s3_prefix":"dynamo-db-export-for-testing/AWSDynamoDB/01775548197701-676bc99a/data/","ingestion":{"option_s3_prefix":"dynamo-db-export-for-testing/options-fixed3/","index_name_prefix":"fmx-option-index","min_units":1,"max_units":4}}
Without OSIS pipeline (DynamoDB tables + index only):
{"user_table_name":"user-embeddings","item_table_name":"item-embeddings","s3_embedding_store":"fmx-precs-product-recommendation-dev","user_s3_prefix":"dynamo-db-export-for-testing/AWSDynamoDB/01775548616823-03c568a6/data/","item_s3_prefix":"dynamo-db-export-for-testing/AWSDynamoDB/01775548197701-676bc99a/data/"}

Typical full-deployment sequence
1.  Run pipeline  →  set SUFFIX=<new-suffix> + CONFIG=<json>
2.  ▶ deploy:opensearch-dev          (first time only, or after domain config changes)
3.  ▶ deploy:fmx-data-stores-dev     (kicks off DynamoDB import + OSIS ingestion)
4.    wait:fmx-data-stores-dev       (runs automatically — polls until ingestion is complete)
5.  ▶ destroy:fmx-data-stores-dev    (set DESTROY_STACK_SUFFIX=<old-suffix>, then click ▶)
Support
For issues or questions contact the Product Recommender team or check CloudWatch Logs.
License
Internal use only — Vistaprint/Cimpress
