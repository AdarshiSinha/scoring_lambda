"""
Lambda construct for creating OpenSearch indices
"""
from aws_cdk import (
    Duration,
    CustomResource,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_opensearchservice as opensearch,
    custom_resources as cr,
)
from constructs import Construct


class IndexCreatorConstruct(Construct):
    """
    Creates a Lambda function that creates an OpenSearch index
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        domain: opensearch.Domain,
        index_name: str,
        stage: str,
        function_name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.domain = domain
        self.index_name = index_name
        self.stage = stage
        self.function_name = function_name

        # Create Lambda function
        self.function = self._create_lambda_function()

        # Grant Lambda permissions to access OpenSearch
        self._grant_opensearch_permissions()

        # Create custom resource to trigger Lambda after stack deployment
        self._create_custom_resource()

    def _create_lambda_function(self) -> lambda_.Function:
        """
        Create Lambda function for index creation
        """
        function = lambda_.Function(
            self,
            "Function",
            function_name=self.function_name,
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/index_creator"),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "OPENSEARCH_ENDPOINT": self.domain.domain_endpoint,
                "INDEX_NAME": self.index_name,
                "REGION": self.domain.stack.region,
            },
        )

        return function

    def _grant_opensearch_permissions(self):
        """
        Grant Lambda permissions to access OpenSearch
        """
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "es:ESHttpGet",
                    "es:ESHttpPost",
                    "es:ESHttpPut",
                    "es:ESHttpDelete",
                    "es:ESHttpHead",
                ],
                resources=[
                    f"{self.domain.domain_arn}/*",
                ],
            )
        )

    def _create_custom_resource(self):
        """
        Create custom resource to trigger Lambda after domain is ready
        """
        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=self.function,
        )

        CustomResource(
            self,
            "CustomResource",
            service_token=provider.service_token,
            properties={
                "DomainEndpoint": self.domain.domain_endpoint,
                "IndexName": self.index_name,
                # No Timestamp — avoids a spurious UPDATE event on every deploy
            },
        )
