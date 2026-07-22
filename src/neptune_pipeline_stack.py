import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

_BUNDLING = cdk.BundlingOptions(
    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
    command=[
        "bash",
        "-c",
        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
    ],
)

# Neptune bulk-load states that mean "still working" — the state machine loops.
_LOAD_IN_PROGRESS = ["LOAD_NOT_STARTED", "LOAD_IN_QUEUE", "LOAD_IN_PROGRESS"]


class NeptunePipelineStack(cdk.Stack):
    """
    Append-only, event-driven Neptune ingestion pipeline.

    A transform pipeline deposits a dated snapshot per portal in S3 and writes
    ``manifest.ttl`` last as the completion sentinel:

        s3://<bucket>/{portal}/YYYY-MM-DD/*.ttl
        s3://<bucket>/{portal}/YYYY-MM-DD/manifest.ttl   ← written last

    Flow:
      S3 "Object Created" (EventBridge)
        → EventBridge Rule (key suffix "manifest.ttl")
          → Step Functions: StartLoad → Wait/Check loop → Record{Success,Failure}

    Each snapshot is bulk-loaded into its own named graph
    (``urn:sagebrain:{portal}:{date}``) so every historical version stays
    isolated — the append-only model. Neptune is an ingestion/serving target
    only; it computes no diffs and performs no upserts.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        data_bucket: s3.Bucket,
        neptune_security_group: ec2.SecurityGroup,
        neptune_cluster_endpoint: str,
        neptune_cluster_resource_id: str,
        neptune_load_role_arn: str,
        pipeline_config: dict,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        graph_uri_template = pipeline_config.get(
            "graph_uri_template", "urn:sagebrain:{portal}:{date}"
        )
        parallelism = pipeline_config.get("parallelism", "HIGH")
        wait_seconds = pipeline_config.get("wait_seconds", 30)

        # -------------------
        # DynamoDB — load tracking / lineage (append-only audit trail, no TTL)
        # PK=portal, SK=snapshot(date) — descending query yields the latest load.
        # -------------------
        self.load_table = dynamodb.Table(
            self,
            "LoadTrackingTable",
            table_name=f"{construct_id}-loads",
            partition_key=dynamodb.Attribute(
                name="portal", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="snapshot", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -------------------
        # Loader Lambda (VPC — reaches the Neptune writer endpoint over 8182)
        # -------------------
        self.loader_sg = ec2.SecurityGroup(
            self,
            "NeptuneLoaderFunctionSG",
            vpc=vpc,
            description="Security group for Neptune bulk-loader Lambda",
            allow_all_outbound=True,
        )
        ec2.CfnSecurityGroupIngress(
            self,
            "LoaderToNeptuneIngress",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.loader_sg.security_group_id,
        )

        self.loader_fn = lambda_.Function(
            self,
            "NeptuneLoaderFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="loader.handler",
            code=lambda_.Code.from_asset("src/lambda_loader", bundling=_BUNDLING),
            vpc=vpc,
            security_groups=[self.loader_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                # Bulk loads must target the writer/cluster endpoint, not the reader.
                "NEPTUNE_ENDPOINT": neptune_cluster_endpoint,
                "LOAD_TABLE_NAME": self.load_table.table_name,
                "NEPTUNE_LOAD_ROLE_ARN": neptune_load_role_arn,
                "GRAPH_URI_TEMPLATE": graph_uri_template,
                "LOAD_PARALLELISM": parallelism,
            },
            timeout=cdk.Duration.seconds(30),  # each task is a single fast HTTP call
            memory_size=256,
        )
        self.load_table.grant_read_write_data(self.loader_fn)

        # IAM: Neptune bulk-loader actions scoped to this cluster. No S3 or
        # PassRole — Neptune reads S3 itself via the cluster-associated load role.
        self.loader_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:StartLoaderJob",
                    "neptune-db:GetLoaderJobStatus",
                    "neptune-db:CancelLoaderJob",
                    "neptune-db:GetEngineStatus",
                ],
                resources=[
                    f"arn:{self.partition}:neptune-db:{self.region}:{self.account}:{neptune_cluster_resource_id}/*"
                ],
            )
        )

        # -------------------
        # Step Functions — submit + poll loop
        # -------------------
        start_load = tasks.LambdaInvoke(
            self,
            "StartLoad",
            lambda_function=self.loader_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "action": "start",
                    "bucket": sfn.JsonPath.object_at("$.bucket"),
                    "object": sfn.JsonPath.object_at("$.object"),
                }
            ),
            payload_response_only=True,
            result_path="$.load",
        )

        wait = sfn.Wait(
            self,
            "WaitForLoad",
            time=sfn.WaitTime.duration(cdk.Duration.seconds(wait_seconds)),
        )

        check_load = tasks.LambdaInvoke(
            self,
            "CheckLoad",
            lambda_function=self.loader_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "action": "check",
                    "load_id": sfn.JsonPath.string_at("$.load.load_id"),
                }
            ),
            payload_response_only=True,
            result_path="$.check",
        )

        record_common = {
            "portal": sfn.JsonPath.string_at("$.load.portal"),
            "snapshot": sfn.JsonPath.string_at("$.load.snapshot"),
            "load_id": sfn.JsonPath.string_at("$.load.load_id"),
            "named_graph": sfn.JsonPath.string_at("$.load.named_graph"),
            "prefix": sfn.JsonPath.string_at("$.load.prefix"),
            "etag": sfn.JsonPath.string_at("$.load.etag"),
            "load_status": sfn.JsonPath.string_at("$.check.load_status"),
            "overall_status": sfn.JsonPath.object_at("$.check.overall_status"),
        }

        record_success = tasks.LambdaInvoke(
            self,
            "RecordSuccess",
            lambda_function=self.loader_fn,
            payload=sfn.TaskInput.from_object({"action": "record", **record_common}),
            payload_response_only=True,
            result_path="$.record",
        ).next(sfn.Succeed(self, "LoadSucceeded"))

        record_failure = tasks.LambdaInvoke(
            self,
            "RecordFailure",
            lambda_function=self.loader_fn,
            payload=sfn.TaskInput.from_object({"action": "record", **record_common}),
            payload_response_only=True,
            result_path="$.record",
        ).next(sfn.Fail(self, "LoadFailed", cause="Neptune bulk load did not complete"))

        # Malformed key or unrecoverable Lambda error before we have a load id.
        bad_input = sfn.Fail(
            self, "BadManifestKey", cause="Could not start load for manifest event"
        )
        # Load status could not be polled after retries.
        check_failed = sfn.Fail(
            self, "LoadCheckFailed", cause="Could not poll Neptune load status"
        )

        skipped = sfn.Succeed(self, "LoadSkipped", comment="Snapshot already loaded")

        choice = (
            sfn.Choice(self, "LoadComplete?")
            .when(
                sfn.Condition.string_equals("$.check.load_status", "LOAD_COMPLETED"),
                record_success,
            )
            .when(
                sfn.Condition.or_(
                    *[
                        sfn.Condition.string_equals("$.check.load_status", s)
                        for s in _LOAD_IN_PROGRESS
                    ]
                ),
                wait,
            )
            .otherwise(record_failure)
        )

        # Transient Lambda/HTTP failures retry; a bad manifest key stops fast.
        transient = [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
        ]
        start_load.add_retry(
            errors=transient,
            max_attempts=3,
            interval=cdk.Duration.seconds(3),
            backoff_rate=2.0,
        )
        start_load.add_catch(bad_input, errors=["States.ALL"])
        check_load.add_retry(
            errors=transient + ["States.TaskFailed"],
            max_attempts=4,
            interval=cdk.Duration.seconds(5),
            backoff_rate=2.0,
        )
        check_load.add_catch(check_failed, errors=["States.ALL"])

        # StartLoad → (skip? → Succeed) → Wait → CheckLoad → Choice
        definition = start_load.next(
            sfn.Choice(self, "AlreadyLoaded?")
            .when(sfn.Condition.boolean_equals("$.load.skip", True), skipped)
            .otherwise(wait.next(check_load).next(choice))
        )

        sm_log_group = logs.LogGroup(
            self,
            "PipelineStateMachineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "LoadPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=cdk.Duration.hours(12),  # very large loads can run long
            logs=sfn.LogOptions(destination=sm_log_group, level=sfn.LogLevel.ERROR),
        )

        # -------------------
        # EventBridge — S3 manifest.ttl created → start an execution
        # -------------------
        self.rule_dlq = sqs.Queue(
            self,
            "PipelineRuleDLQ",
            retention_period=cdk.Duration.days(14),
        )
        rule = events.Rule(
            self,
            "ManifestCreatedRule",
            description="Neptune load pipeline: fires when a snapshot manifest.ttl lands",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [data_bucket.bucket_name]},
                    "object": {"key": [{"suffix": "manifest.ttl"}]},
                },
            ),
        )
        rule.add_target(
            targets.SfnStateMachine(
                self.state_machine,
                # Pass the S3 detail ({bucket, object, ...}) as the execution input.
                input=events.RuleTargetInput.from_event_path("$.detail"),
                dead_letter_queue=self.rule_dlq,
                retry_attempts=3,
            )
        )

        # -------------------
        # Alarms
        # -------------------
        cw.Alarm(
            self,
            "PipelineExecutionsFailedAlarm",
            metric=self.state_machine.metric_failed(period=cdk.Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A Neptune load pipeline execution failed",
        )
        cw.Alarm(
            self,
            "PipelineRuleDLQAlarm",
            metric=self.rule_dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="EventBridge failed to start a load pipeline execution",
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "LoadPipelineStateMachineArn",
            value=self.state_machine.state_machine_arn,
            description="Step Functions state machine that loads snapshots into Neptune",
        )
        cdk.CfnOutput(
            self,
            "LoadTrackingTableName",
            value=self.load_table.table_name,
            description="DynamoDB table tracking per-snapshot load status / lineage",
        )
