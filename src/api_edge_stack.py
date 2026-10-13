import hashlib
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from src.api_edge_monitoring import ApiEdgeMonitoring
from src.contract import LIMITS, SPEC_PATH
from src.core_layer import sagebrain_core_layer

DOCS_DIR = Path(__file__).parent / "lambda_docs"

# Status methods get their own throttle (spec 001 D-17); everything else gets the stage's.
STATUS_METHODS = ("/query/{job_id}/GET", "/ask/{job_id}/GET")


def _docs_asset_hash() -> str:
    """Hash of everything the docs asset is built from: its own files plus the contract.

    Bundling mounts the contract from outside the asset dir, so the default source hash
    would miss a contract-only change and CDK would skip redeploying the docs Lambda.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in DOCS_DIR.iterdir() if p.suffix in (".py", ".txt")):
        digest.update(path.name.encode() + path.read_bytes())
    digest.update(SPEC_PATH.read_bytes())
    return digest.hexdigest()


def _json_message(message: str) -> dict:
    return {"application/json": f'{{"message":"{message}"}}'}


class ApiEdgeStack(cdk.Stack):
    """
    The unified API (spec 001): one REST API, stage named after the env (`dev`, `prod`), serving
    /query, /ask and /api.

    Flow:
      POST /query, GET /query/{job_id}  →  authorizer  →  submit/status Lambdas  →  query table/queue
      POST /ask,   GET /ask/{job_id}    →  authorizer  →  submit/status Lambdas  →  ask table/queue
      GET /api, /api/openapi.{json,yaml}  →  docs Lambda (public)

    The job tables, queues and workers stay in the API and agent stacks; this stack only
    replaces their front doors. It runs alongside the two old APIs until cutover
    (`API_EDGE.enabled`).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        query_job_table: dynamodb.ITable,
        query_job_queue: sqs.IQueue,
        ask_job_table: dynamodb.ITable,
        ask_job_queue: sqs.IQueue,
        synapse_team_id: str,
        stage_name: str,
        alarm_thresholds: dict,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        request_timeout = cdk.Duration.seconds(LIMITS["request_timeout_seconds"])
        self.core_layer = sagebrain_core_layer(self)

        # -------------------
        # Submit/status Lambdas — no VPC (DynamoDB and SQS only)
        # -------------------
        def job_function(construct_id, handler, table, queue=None):
            environment = {"JOB_TABLE_NAME": table.table_name}
            if queue is not None:
                environment["JOB_QUEUE_URL"] = queue.queue_url
            return lambda_.Function(
                self,
                construct_id,
                runtime=lambda_.Runtime.PYTHON_3_11,
                architecture=lambda_.Architecture.ARM_64,
                handler=f"jobs.{handler}",
                code=lambda_.Code.from_asset("src/lambda_edge"),
                layers=[self.core_layer],
                environment=environment,
                timeout=request_timeout,
                memory_size=256,
            )

        self.query_submit_fn = job_function(
            "QuerySubmitFunction", "submit_query", query_job_table, query_job_queue
        )
        self.query_status_fn = job_function(
            "QueryStatusFunction", "query_status", query_job_table
        )
        self.ask_submit_fn = job_function(
            "AskSubmitFunction", "submit_question", ask_job_table, ask_job_queue
        )
        self.ask_status_fn = job_function(
            "AskStatusFunction", "ask_status", ask_job_table
        )

        query_job_table.grant_write_data(self.query_submit_fn)
        query_job_queue.grant_send_messages(self.query_submit_fn)
        query_job_table.grant_read_data(self.query_status_fn)
        ask_job_table.grant_write_data(self.ask_submit_fn)
        ask_job_queue.grant_send_messages(self.ask_submit_fn)
        ask_job_table.grant_read_data(self.ask_status_fn)

        # -------------------
        # Docs Lambda — Swagger UI and the contract (public)
        # -------------------
        self.docs_fn = lambda_.Function(
            self,
            "DocsFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            architecture=lambda_.Architecture.ARM_64,
            handler="docs.handler",
            code=lambda_.Code.from_asset(
                str(DOCS_DIR),
                asset_hash_type=cdk.AssetHashType.CUSTOM,
                asset_hash=_docs_asset_hash(),
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                    platform="linux/arm64",
                    volumes=[
                        cdk.DockerVolume(
                            host_path=str(SPEC_PATH.parent),
                            container_path="/contract",
                        )
                    ],
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output"
                        " && cp docs.py /asset-output/"
                        f" && python bundle.py /contract/{SPEC_PATH.name} /asset-output",
                    ],
                ),
            ),
            timeout=request_timeout,
            memory_size=256,
        )

        # -------------------
        # Authorizer — no VPC (calls Synapse's public API)
        # Synapse bearer tokens are the only credential; x-api-key is ignored (D-4).
        # -------------------
        authorizer_fn = lambda_.Function(
            self,
            "SynapseAuthorizerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="authorizer.handler",
            code=lambda_.Code.from_asset("src/lambda_authorizer"),
            environment={"SYNAPSE_TEAM_ID": synapse_team_id},
            timeout=cdk.Duration.seconds(LIMITS["auth_timeout_seconds"]),
            memory_size=256,
        )
        # API Gateway's cache is off; the Lambda caches successes in-process for 5 min.
        authorizer = apigw.RequestAuthorizer(
            self,
            "SynapseRequestAuthorizer",
            handler=authorizer_fn,
            identity_sources=[apigw.IdentitySource.header("Authorization")],
            results_cache_ttl=cdk.Duration.seconds(0),
        )

        # -------------------
        # API Gateway
        # -------------------
        access_log_group = logs.LogGroup(
            self,
            "ApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        status_throttle = apigw.MethodDeploymentOptions(
            throttling_rate_limit=LIMITS["status_rate_rps"],
            throttling_burst_limit=LIMITS["status_burst"],
            # A method override replaces the stage defaults, so restate them
            logging_level=apigw.MethodLoggingLevel.ERROR,
            metrics_enabled=True,
        )

        self.api = apigw.RestApi(
            self,
            "SageBrainApi",
            rest_api_name="sagebrain-api",
            description="Sage Brain API: async SPARQL (/query), agent (/ask), docs (/api)",
            cloud_watch_role=True,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "GET", "OPTIONS"],
                allow_headers=[
                    "Content-Type",
                    "Authorization",
                    "X-Source",
                    "x-api-key",
                ],
            ),
            deploy_options=apigw.StageOptions(
                stage_name=stage_name,
                access_log_destination=apigw.LogGroupLogDestination(access_log_group),
                # requestId is always included
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                logging_level=apigw.MethodLoggingLevel.ERROR,
                # Stage-wide (*/*), so /query and /ask have separate per-method metrics
                metrics_enabled=True,
                throttling_rate_limit=LIMITS["method_rate_rps"],
                throttling_burst_limit=LIMITS["method_burst"],
                method_options={path: status_throttle for path in STATUS_METHODS},
            ),
        )

        # -------------------
        # Gateway responses — every one carries CORS so browser clients can read it
        # -------------------
        cors = {"Access-Control-Allow-Origin": "'*'"}
        unauthorized = _json_message("Unauthorized")  # one body for every denial (D-10)
        self.api.add_gateway_response(
            "AccessDenied",
            type=apigw.ResponseType.ACCESS_DENIED,
            status_code="401",
            response_headers=cors,
            templates=unauthorized,
        )
        self.api.add_gateway_response(
            "Unauthorized",
            type=apigw.ResponseType.UNAUTHORIZED,
            status_code="401",
            response_headers=cors,
            templates=unauthorized,
        )
        # Unknown path or method sent without an Authorization header (D-3). With one,
        # API Gateway raises IncompleteSignatureException instead, which has no response
        # type of its own; the not-found routes below catch those requests first.
        self.api.add_gateway_response(
            "MissingAuthenticationToken",
            type=apigw.ResponseType.MISSING_AUTHENTICATION_TOKEN,
            status_code="404",
            response_headers=cors,
            templates=_json_message("Not Found"),
        )
        # The authorizer raised (e.g. Synapse is down): retryable, not a bug (D-13).
        # Which of the two types API Gateway uses for a raising Lambda authorizer isn't
        # documented clearly, so both map to 503.
        auth_retry = {
            **cors,
            "Retry-After": f"'{LIMITS['auth_failure_retry_after_seconds']}'",
        }
        for construct_id, response_type in (
            ("AuthorizerFailure", apigw.ResponseType.AUTHORIZER_FAILURE),
            (
                "AuthorizerConfigurationError",
                apigw.ResponseType.AUTHORIZER_CONFIGURATION_ERROR,
            ),
        ):
            self.api.add_gateway_response(
                construct_id,
                type=response_type,
                status_code="503",
                response_headers=auth_retry,
                templates=_json_message("Service Unavailable"),
            )
        # D-18
        self.api.add_gateway_response(
            "Throttled",
            type=apigw.ResponseType.THROTTLED,
            status_code="429",
            response_headers={
                **cors,
                "Retry-After": f"'{LIMITS['throttled_retry_after_seconds']}'",
            },
            templates=_json_message("Too Many Requests"),
        )
        self.api.add_gateway_response(
            "Default4xx", type=apigw.ResponseType.DEFAULT_4_XX, response_headers=cors
        )
        self.api.add_gateway_response(
            "Default5xx", type=apigw.ResponseType.DEFAULT_5_XX, response_headers=cors
        )

        # -------------------
        # Routes — by hand; tests/unit/test_api_edge_stack.py checks them against the contract
        # -------------------
        def integration(fn):
            return apigw.LambdaIntegration(fn, timeout=request_timeout)

        def authorized(resource, method, fn):
            resource.add_method(method, integration(fn), authorizer=authorizer)

        query = self.api.root.add_resource("query")
        authorized(query, "POST", self.query_submit_fn)
        authorized(query.add_resource("{job_id}"), "GET", self.query_status_fn)

        ask = self.api.root.add_resource("ask")
        authorized(ask, "POST", self.ask_submit_fn)
        authorized(ask.add_resource("{job_id}"), "GET", self.ask_status_fn)

        docs = self.api.root.add_resource("api")
        for resource in (
            docs,
            docs.add_resource("openapi.json"),
            docs.add_resource("openapi.yaml"),
        ):
            resource.add_method(
                "GET",
                integration(self.docs_fn),
                authorization_type=apigw.AuthorizationType.NONE,
            )

        # Not found (D-3): ANY on every resource plus a root {proxy+} catch-all, all a
        # mock 404. API Gateway prefers the exact method and the more specific path, so
        # these only answer requests no route above matches.
        # A mock integration can't parse a passed-through body (500), so the common types
        # get a template and any other Content-Type is a 415
        not_found = apigw.MockIntegration(
            request_templates={
                content_type: '{"statusCode": 404}'
                for content_type in (
                    "application/json",
                    "text/plain",
                    "application/x-www-form-urlencoded",
                    "application/sparql-query",
                )
            },
            passthrough_behavior=apigw.PassthroughBehavior.NEVER,
            integration_responses=[
                apigw.IntegrationResponse(
                    status_code="404",
                    response_parameters={
                        "method.response.header.Access-Control-Allow-Origin": "'*'"
                    },
                    response_templates=_json_message("Not Found"),
                )
            ],
        )
        not_found_response = apigw.MethodResponse(
            status_code="404",
            response_parameters={
                "method.response.header.Access-Control-Allow-Origin": True
            },
        )
        for resource in (
            self.api.root,
            self.api.root.add_resource("{proxy+}"),
            *self.api.root.node.find_all(),
        ):
            if isinstance(
                resource, apigw.ResourceBase
            ) and not resource.node.try_find_child("ANY"):
                resource.add_method(
                    "ANY",
                    not_found,
                    authorization_type=apigw.AuthorizationType.NONE,
                    method_responses=[not_found_response],
                )

        ApiEdgeMonitoring(
            self,
            "Monitoring",
            api=self.api,
            stage_name=stage_name,
            access_log_group=access_log_group,
            functions={
                "query submit": self.query_submit_fn,
                "query status": self.query_status_fn,
                "ask submit": self.ask_submit_fn,
                "ask status": self.ask_status_fn,
                "authorizer": authorizer_fn,
                "docs": self.docs_fn,
            },
            queues={"query": query_job_queue, "ask": ask_job_queue},
            thresholds=alarm_thresholds,
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "ApiBaseUrl",
            value=self.api.url,
            description="Base URL for /query, /ask and /api (stage prefix included)",
        )
        cdk.CfnOutput(
            self,
            "ApiDocsUrl",
            value=f"{self.api.url}api",
            description="Swagger UI for the contract (public)",
        )
