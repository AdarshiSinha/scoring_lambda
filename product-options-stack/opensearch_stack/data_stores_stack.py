from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from aws_cdk import Stack, CfnOutput, RemovalPolicy
from constructs import Construct
from aws_cdk import aws_iam as iam
from aws_cdk import aws_osis as osis
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import custom_resources as cr
from .index_creator import IndexCreatorConstruct


class DataStoresStack(Stack):
    """
    Creates DynamoDB tables for storing user and item embeddings by importing
    data directly from S3 using the DynamoDB ImportTable API (via a custom
    resource). Accepts an optional `config` dict (passed via App context or
    directly) to override table names, S3 prefixes, and ingestion settings.
    Optionally creates an OpenSearch index and an OSIS ingestion pipeline.

    Also schedules a one-time EventBridge Scheduler rule to automatically stop
    the OSIS pipeline after a configurable duration post-deployment.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: dict | None = None,
        domain: opensearch.Domain | None = None,
        s3_embedding_store: str | None = None,
        stage:str | None = None,
        suffix:str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Allow passing a config dict (from app.node.try_get_context("config"))
        self.config = config or {}

        # Table names (allow overrides via config)

        user_table_name = (self.config.get("user_table_name") or "fmx-user-embeddings") + f"-{stage}-{suffix}"
        item_table_name = (self.config.get("item_table_name") or "fmx-item-embeddings") + f"-{stage}-{suffix}"
        index_name = f"{self.config.get('index_name_prefix') or 'fmx-option-index'}-{stage}-{suffix}"
        ingestion_pipeline_name = f"options-{stage}-{suffix}"

        # User table import

        user_prefix = self.config.get("user_s3_prefix")
        if not (s3_embedding_store and user_prefix):
            raise ValueError("config must include 's3_embedding_store' and 'user_s3_prefix' when using import-from-s3")

        self._create_table_import(
            construct_id="StartUserImport",
            output_prefix="UserEmbeddings",
            table_name=user_table_name,
            s3_bucket=s3_embedding_store,
            s3_prefix=user_prefix,
            attribute_definitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "locale", "AttributeType": "S"},
            ],
            key_schema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "locale", "KeyType": "RANGE"},
            ],
        )
        # Item table import
        item_prefix = self.config.get("item_s3_prefix")
        if not item_prefix:
            raise ValueError("config must include 'item_s3_prefix' when using import-from-s3")

        self._create_table_import(
            construct_id="StartItemImport",
            output_prefix="ItemEmbeddings",
            table_name=item_table_name,
            s3_bucket=s3_embedding_store,
            s3_prefix=item_prefix,
            attribute_definitions=[
                {"AttributeName": "product_key", "AttributeType": "S"},
                {"AttributeName": "locale", "AttributeType": "S"},
            ],
            key_schema=[
                {"AttributeName": "product_key", "KeyType": "HASH"},
                {"AttributeName": "locale", "KeyType": "RANGE"},
            ],
        )

        ## create open search index

        self.index_creator = IndexCreatorConstruct(
            self,
            "IndexCreator",
            domain=domain,
            index_name=index_name,
            stage=stage,
            function_name=f"opensearch-index-creator-{stage}-{suffix}"
        )

        CfnOutput(
            self,
            "OpenSearchIndexName",
            value=index_name,
            description="OpenSearch index name",
        )

        # ── Optional OSIS ingestion pipeline ──────────────────────────────
        # Created when a domain is supplied AND config["ingestion"] contains
        # at least s3_bucket, s3_prefix, and index_name.
        ingestion_cfg: dict = self.config.get("ingestion", {})
        if domain is not None and ingestion_cfg.get("option_s3_prefix"):
            if not ingestion_cfg.get("index_name_prefix"):
                raise ValueError(
                    "config['ingestion']['index_name'] is required when creating an OSIS pipeline"
                )
            self.create_s3_ingestion_pipeline(
                domain=domain,
                s3_bucket=s3_embedding_store,
                s3_prefix=ingestion_cfg["option_s3_prefix"],
                index_name=index_name,
                pipeline_name=ingestion_pipeline_name,
                min_units=int(ingestion_cfg.get("min_units", 1)),
                max_units=int(ingestion_cfg.get("max_units", 4)),
            )



    def _create_table_import(
        self,
        *,
        construct_id: str,
        output_prefix: str,
        table_name: str,
        s3_bucket: str,
        s3_prefix: str,
        attribute_definitions: list | None = None,
        key_schema: list | None = None,
        input_format: str = "DYNAMODB_JSON",
        compression_type: str = "GZIP",
    ) -> cr.AwsCustomResource:
        """
        Creates a DynamoDB table via the ImportTable API (import from S3) and
        registers an on-delete handler that calls DeleteTable so the table is
        removed when the stack is destroyed.

        Parameters
        ----------
        construct_id          : CDK construct ID (must be unique within the stack).
        output_prefix         : Prefix for CloudFormation output logical IDs,
                                e.g. "UserEmbeddings" → UserEmbeddingsTableName.
        table_name            : DynamoDB table name to create.
        s3_bucket             : Source S3 bucket name.
        s3_prefix             : Source S3 key prefix.
        attribute_definitions : List of AttributeDefinition dicts.
                                Defaults to [{"AttributeName": "id", "AttributeType": "S"}].
        key_schema            : List of KeySchemaElement dicts.
                                Defaults to [{"AttributeName": "id", "KeyType": "HASH"}].
        input_format          : DynamoDB import format. Default "DYNAMODB_JSON".
                                Other valid values: "ION", "CSV".
        compression_type      : Compression of the S3 files. Default "GZIP".
                                Other valid values: "ZSTD", "NONE".
        """
        attribute_definitions = attribute_definitions or [
            {"AttributeName": "id", "AttributeType": "S"}
        ]
        key_schema = key_schema or [
            {"AttributeName": "id", "KeyType": "HASH"}
        ]
        table_import = cr.AwsCustomResource(
            self,
            construct_id,
            on_create=cr.AwsSdkCall(
                service="DynamoDB",
                action="importTable",
                parameters={
                    "S3BucketSource": {"S3Bucket": s3_bucket, "S3KeyPrefix": s3_prefix},
                    "InputFormat": input_format,
                    "InputCompressionType": compression_type,
                    "TableCreationParameters": {
                        "TableName": table_name,
                        "AttributeDefinitions": attribute_definitions,
                        "KeySchema": key_schema,
                        "BillingMode": "PAY_PER_REQUEST",
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"{table_name}-import"),
            ),
            on_delete=cr.AwsSdkCall(
                service="DynamoDB",
                action="deleteTable",
                parameters={"TableName": table_name},
                physical_resource_id=cr.PhysicalResourceId.of(f"{table_name}-delete"),
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["dynamodb:ImportTable", "dynamodb:DescribeImport", "dynamodb:DeleteTable"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[f"arn:aws:s3:::{s3_bucket}", f"arn:aws:s3:::{s3_bucket}/*"],
                ),
                iam.PolicyStatement(
                    actions=["logs:*"],
                    resources=["*"],
                ),
            ]),
        )

        CfnOutput(
            self,
            f"{output_prefix}TableName",
            value=table_name,
            description=f"DynamoDB table name for {output_prefix} (created by import)",
        )

        return table_import

    def create_s3_ingestion_pipeline(
        self,
        *,
        domain: opensearch.Domain,
        s3_bucket: str,
        s3_prefix: str,
        index_name: str,
        pipeline_name: str | None = None,
        min_units: int = 1,
        max_units: int = 4,
    ) -> osis.CfnPipeline:
        """
        Creates an OpenSearch Ingestion (OSIS) pipeline that scans Parquet files
        from an S3 location and bulk-indexes them into the given OpenSearch domain.

        Parameters
        ----------
        domain        : The OpenSearch domain construct to ingest into.
        s3_bucket     : S3 bucket name that contains the Parquet files.
        s3_prefix     : S3 key prefix (folder) to scan, e.g. "embeddings/options/".
        index_name    : Target OpenSearch index name.
        pipeline_name : OSIS resource name (3–28 chars, lowercase alphanumeric +
                        hyphens, must start with a letter).
                        Defaults to a sanitised form of the CDK stack name.
        min_units     : Minimum OCU capacity (1–96). Default 1.
        max_units     : Maximum OCU capacity (1–96). Default 4.

        Returns
        -------
        osis.CfnPipeline

        Notes
        -----
        Because the OpenSearch domain uses fine-grained access control you must
        map the pipeline role as an OpenSearch backend role after the first deploy:

            PUT _plugins/_security/api/rolesmapping/all_access
            {
              "backend_roles": ["<OsisPipelineRoleArn CloudFormation output>"]
            }
        """
        # Sanitise pipeline name — OSIS constraint: 3-28 chars, lowercase
        # alphanumeric + hyphens, must start with a letter.
        raw_name = pipeline_name or self.stack_name
        safe_name = re.sub(r"[^a-z0-9-]", "-", raw_name.lower())[:28]
        if not re.match(r"^[a-z]", safe_name):
            safe_name = "p-" + safe_name[:26]

        # ── IAM role assumed by OSIS ───────────────────────────────────────
        pipeline_role = iam.Role(
            self,
            "OsisPipelineRole",
            assumed_by=iam.ServicePrincipal("osis-pipelines.amazonaws.com"),
            description="Role assumed by OSIS to read Parquet from S3 and write to OpenSearch",
        )

        # S3 read
        pipeline_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3ReadParquet",
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    f"arn:aws:s3:::{s3_bucket}",
                    f"arn:aws:s3:::{s3_bucket}/*",
                ],
            )
        )

        # OpenSearch write (IAM level; FGAC backend-role mapping also required)
        pipeline_role.add_to_policy(
            iam.PolicyStatement(
                sid="OpenSearchWrite",
                actions=["es:DescribeDomain", "es:ESHttp*"],
                resources=[domain.domain_arn, f"{domain.domain_arn}/*"],
            )
        )

        # ── Data Prepper YAML (structure from OSIS visual builder) ───────────
        pipeline_yaml = (
            "version: '2'\n"
            "extension:\n"
            "  osis_configuration_metadata:\n"
            "    builder_type: visual\n"
            f"{safe_name}:\n"
            "  source:\n"
            "    s3:\n"
            "      acknowledgments: true\n"
            "      delete_s3_objects_on_read: false\n"
            "      scan:\n"
            "        buckets:\n"
            "          - bucket:\n"
            f"              name: \"{s3_bucket}\"\n"
            "              filter:\n"
            "                include_prefix:\n"
            f"                  - \"{s3_prefix}\"\n"
            "                exclude_suffix:\n"
            "                  - \"_SUCCESS\"\n"
            "                  - \"_metadata\"\n"
            "                  - \"_common_metadata\"\n"
            "      aws:\n"
            f"        region: \"{self.region}\"\n"
            f"        sts_role_arn: \"{pipeline_role.role_arn}\"\n"
            "      codec:\n"
            "        parquet: null\n"
            "      compression: none\n"
            "      workers: '1'\n"
            "  processor:\n"
            "    - parse_json:\n"
            "        source: option_embedding\n"
            "  sink:\n"
            "    - opensearch:\n"
            "        hosts:\n"
            f"          - \"https://{domain.domain_endpoint}\"\n"
            "        aws:\n"
            "          serverless: false\n"
            f"          region: \"{self.region}\"\n"
            f"          sts_role_arn: \"{pipeline_role.role_arn}\"\n"
            "        index_type: custom\n"
            f"        index: \"{index_name}\"\n"
            "        document_id: \"${training_options_std_hash_key}\"\n"
            "        bulk_size: 1\n"          # MiB per bulk request; smaller = less throttling on t3.small
            "        flush_timeout: 30000\n"  # ms between flushes; spreads write load over time
        )

        # ── CloudWatch log group for pipeline logs ─────────────────────────
        log_group = logs.LogGroup(
            self,
            "OsisPipelineLogs",
            log_group_name=f"/aws/vendedlogs/OpenSearchIngestion/{safe_name}/compute-logs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── OSIS pipeline resource ─────────────────────────────────────────
        pipeline = osis.CfnPipeline(
            self,
            "OsisPipeline",
            pipeline_name=safe_name,
            min_units=min_units,
            max_units=max_units,
            pipeline_configuration_body=pipeline_yaml,
            log_publishing_options=osis.CfnPipeline.LogPublishingOptionsProperty(
                is_logging_enabled=True,
                cloud_watch_log_destination=osis.CfnPipeline.CloudWatchLogDestinationProperty(
                    log_group=log_group.log_group_name,
                ),
            ),
        )

        # Force CloudFormation to wait for the IAM role AND all its attached
        # policies before creating the pipeline. Without this, CloudFormation
        # creates the pipeline in parallel with the AWS::IAM::Policy attachment,
        # causing OSIS validation to see a role with no permissions.
        pipeline.node.add_dependency(pipeline_role)

        CfnOutput(
            self,
            "OsisPipelineName",
            value=pipeline.pipeline_name,
            description=f"OSIS pipeline: s3://{s3_bucket}/{s3_prefix} → {index_name}",
        )
        CfnOutput(
            self,
            "OsisPipelineRoleArn",
            value=pipeline_role.role_arn,
            description="Map this ARN as an OpenSearch backend role to allow OSIS to write",
        )

        self._schedule_pipeline_stop(pipeline=pipeline, safe_name=safe_name, stop_after_minutes=20)

        return pipeline

    # ── Scheduled stop helper ─────────────────────────────────────────────────

    def _schedule_pipeline_stop(
        self,
        *,
        pipeline: osis.CfnPipeline,
        safe_name: str,
        stop_after_minutes: int = 30,
        deploy_buffer_minutes: int = 10,
    ) -> None:
        """
        Creates a **one-time** EventBridge Scheduler ``at()`` schedule that calls
        ``osis:StopPipeline`` directly (universal SDK target — no Lambda).

        The target timestamp is computed at CDK synth time as:

            now  +  deploy_buffer_minutes  +  stop_after_minutes

        ``deploy_buffer_minutes`` (default 10) accounts for the time between
        ``cdk synth`` and the pipeline actually becoming ACTIVE. Adjust it if
        your stack deployment takes longer.

        Because ``at()`` schedules are **one-shot**, the schedule fires exactly
        once and is automatically deleted by EventBridge Scheduler afterwards —
        no cleanup required.
        """
        # Compute the one-time fire timestamp at synth time
        fire_at = (
            datetime.now(timezone.utc)
            + timedelta(minutes=deploy_buffer_minutes + stop_after_minutes)
        )
        # EventBridge Scheduler at() format: at(yyyy-mm-ddThh:mm:ss)
        schedule_expression = f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"

        # IAM role that EventBridge Scheduler assumes to call OSIS
        scheduler_role = iam.Role(
            self,
            "OsisPipelineSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Allows EventBridge Scheduler to stop the OSIS pipeline",
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="OsisStop",
                actions=["osis:StopPipeline"],
                resources=[
                    f"arn:aws:osis:{self.region}:{self.account}:pipeline/{safe_name}"
                ],
            )
        )

        # One-time EventBridge Scheduler — universal SDK target, no Lambda
        cfn_schedule = scheduler.CfnSchedule(
            self,
            "OsisPipelineStopSchedule",
            schedule_expression=schedule_expression,         # fires once, then gone
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:osis:stopPipeline",
                role_arn=scheduler_role.role_arn,
                input=json.dumps({"PipelineName": safe_name}),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=0,
                ),
            ),
        )
        cfn_schedule.node.add_dependency(pipeline)

        CfnOutput(
            self,
            "OsisPipelineStopScheduledAt",
            value=fire_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=(
                f"One-time schedule: OSIS pipeline will be stopped at this UTC time "
                f"({deploy_buffer_minutes} min deploy buffer + {stop_after_minutes} min active time). "
                f"The schedule auto-deletes after firing."
            ),
        )
