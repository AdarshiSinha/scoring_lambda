"""
OpenSearch Infrastructure Stack
"""
from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_opensearchservice as opensearch,
    aws_iam as iam,
    aws_ec2 as ec2,
)
from constructs import Construct
from typing import List, Optional


class OpenSearchStack(Stack):
    """
    CDK Stack for OpenSearch Domain and Index Creation
    """

    def __init__(self, scope: Construct, construct_id: str, stage: str, access_roles: Optional[List[str]] = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.stage = stage
        self.domain_name = f"fmx-embedding-{stage}"
        self.access_roles: List[str] = access_roles or []

        # Stage-specific configuration
        config = self._get_stage_config(stage)

        # Create OpenSearch domain
        self.domain = self._create_opensearch_domain(config)


        # Outputs
        self._create_outputs()

    def _get_stage_config(self, stage: str) -> dict:
        """
        Get stage-specific configuration
        """
        configs = {
            "dev": {
                "instance_type": "t3.small.search",
                "instance_count": 1,
                "az_count": 1,
                "dedicated_master_enabled": False,
                "master_instance_type": None,
                "master_count": None,
                "ebs_volume_size": 20,
                "removal_policy": RemovalPolicy.DESTROY,
            },
            "prd": {
                "instance_type": "r8g.large.search",
                "instance_count": 3,
                "az_count": 3,
                "dedicated_master_enabled": True,
                "master_instance_type": "m8g.medium.search",
                "master_count": 3,
                "ebs_volume_size": 20,
                "removal_policy": RemovalPolicy.RETAIN,
            },
        }
        return configs.get(stage, configs["dev"])

    def _create_opensearch_domain(self, config: dict) -> opensearch.Domain:
        """
        Create OpenSearch domain with KNN support
        """
        # Domain configuration
        domain = opensearch.Domain(
            self,
            "OptionEmbeddingDomain",
            domain_name=self.domain_name,
            version=opensearch.EngineVersion.OPENSEARCH_3_3,
            capacity=opensearch.CapacityConfig(
                data_node_instance_type=config["instance_type"],
                data_nodes=config["instance_count"],
                master_nodes=config["master_count"],
                master_node_instance_type=config["master_instance_type"],
                multi_az_with_standby_enabled=False,
            ),
            zone_awareness=opensearch.ZoneAwarenessConfig(
                enabled=config["az_count"] > 1,
                # availability_zone_count must be 2 or 3 — omit it for single-AZ (dev)
                availability_zone_count=config["az_count"] if config["az_count"] > 1 else None,
            ),
            ebs=opensearch.EbsOptions(
                enabled=True,
                volume_type=ec2.EbsDeviceVolumeType.GP3,
                volume_size=config["ebs_volume_size"],
            ),
            # Security settings
            enforce_https=True,
            node_to_node_encryption=True,
            encryption_at_rest=opensearch.EncryptionAtRestOptions(
                enabled=True,
            ),
            # Fine-grained access control
            fine_grained_access_control=opensearch.AdvancedSecurityOptions(
                master_user_arn=None,  # Will use IAM for access
            ),
            # Access policies - allow IAM principals in account (no IP restrictions)
            access_policies=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    principals=[iam.AccountPrincipal(self.account)],
                    actions=["es:*"],
                    resources=[f"arn:aws:es:{self.region}:{self.account}:domain/{self.domain_name}/*"],
                ),
                *(
                    [iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        principals=[
                            iam.ArnPrincipal(f"arn:aws:iam::{self.account}:role/{role}")
                            for role in self.access_roles
                        ],
                        actions=[
                            "es:ESHttpGet",
                            "es:ESHttpPost",
                            "es:DescribeDomain",
                            "es:ESHttpHead",
                        ],
                        resources=[
                            f"arn:aws:es:{self.region}:{self.account}:domain/{self.domain_name}",
                            f"arn:aws:es:{self.region}:{self.account}:domain/{self.domain_name}/*",
                        ],
                    )]
                    if self.access_roles else []
                ),
            ],
            # Advanced options for KNN
            advanced_options={
                "rest.action.multi.allow_explicit_index": "true",
            },
            # Logging
            logging=opensearch.LoggingOptions(
                slow_search_log_enabled=True,
                app_log_enabled=True,
                slow_index_log_enabled=True,
            ),
            # Removal policy
            removal_policy=config["removal_policy"],
        )

        return domain

    def _create_outputs(self):
        """
        Create CloudFormation outputs
        """
        CfnOutput(
            self,
            "OpenSearchEndpoint",
            value=self.domain.domain_endpoint,
            description="OpenSearch domain endpoint",
            export_name=f"{self.stack_name}-endpoint",
        )

        CfnOutput(
            self,
            "OpenSearchDomainArn",
            value=self.domain.domain_arn,
            description="OpenSearch domain ARN",
            export_name=f"{self.stack_name}-arn",
        )

        CfnOutput(
            self,
            "OpenSearchDomainName",
            value=self.domain_name,
            description="OpenSearch domain name",
            export_name=f"{self.stack_name}-domain-name",
        )
