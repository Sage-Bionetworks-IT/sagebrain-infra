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

    A transform pipeline deposits a snapshot per portal in S3, keyed by a tag (a
    ``YYYY-MM-DD`` date or a ``v…`` release), and writes ``manifest.ttl`` last as
    the completion sentinel:

        s3://<bucket>/{portal}/{tag}/data/*.ttl    ← the load path (Turtle only)
        s3://<bucket>/{portal}/{tag}/other/...     ← anything non-RDF
        s3://<bucket>/{portal}/{tag}/manifest.ttl  ← written last, trigger only

    Flow:
      S3 "Object Created" (EventBridge)
        → EventBridge Rule (key suffix "manifest.ttl")
          → Step Functions: Prepare → Route
              ├─ duplicate / in progress → Skip
              ├─ published tag          → Publish            (rollback / no-op, no reload)
              ├─ failed tag             → DropLoop → StartLoad ┐
              └─ new tag                → StartLoad ───────────┴→ Wait/Check loop
                  → RecordSuccess → Publish | RecordFailure → Fail

    Publish writes the portal's entry in the live registry (partition ``_live`` of the
    tracking table), which the query service reads to scope every query (spec 004).

    Only ``data/`` is loaded — Neptune's bulk loader has no include/exclude filter
    and parses everything under the prefix as Turtle, so with failOnError=TRUE a
    single non-RDF sibling would fail the whole snapshot.

    Each snapshot is bulk-loaded into its own named graph
    (``urn:sagebrain:{portal}:{tag}``) so every historical version stays
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
            "graph_uri_template", "urn:sagebrain:{portal}:{tag}"
        )
        parallelism = pipeline_config.get("parallelism", "HIGH")
        wait_seconds = pipeline_config.get("wait_seconds", 30)
        drop_batch_size = pipeline_config.get("drop_batch_size", 100000)

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
                "DROP_BATCH_SIZE": str(drop_batch_size),
            },
            # Each task is one HTTP call. A drop batch can run up to Neptune's 120 s query
            # timeout, plus its ASK (spec 004 thresholds).
            timeout=cdk.Duration.seconds(150),
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
        # Clearing a failed tag's never-published graph before its retry (spec 004 FR-8):
        # a batched DELETE plus an ASK for what remains. A write path with no user-facing
        # caller, so it runs outside the query service (constitution V exception).
        self.loader_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:ReadDataViaQuery",
                    "neptune-db:DeleteDataViaQuery",
                ],
                resources=[
                    f"arn:{self.partition}:neptune-db:{self.region}:{self.account}:{neptune_cluster_resource_id}/*"
                ],
            )
        )

        # -------------------
        # Step Functions — prepare, [drop], load + poll loop, publish
        # -------------------
        def invoke(name, payload, result_path):
            return tasks.LambdaInvoke(
                self,
                name,
                lambda_function=self.loader_fn,
                payload=sfn.TaskInput.from_object(payload),
                payload_response_only=True,
                result_path=result_path,
            )

        snap = {
            "portal": sfn.JsonPath.string_at("$.load.portal"),
            "snapshot": sfn.JsonPath.string_at("$.load.snapshot"),
            "named_graph": sfn.JsonPath.string_at("$.load.named_graph"),
            "sequencer": sfn.JsonPath.string_at("$.load.sequencer"),
            "released_at": sfn.JsonPath.string_at("$.load.released_at"),
        }

        prepare = invoke(
            "Prepare",
            {
                "action": "prepare",
                "bucket": sfn.JsonPath.object_at("$.bucket"),
                "object": sfn.JsonPath.object_at("$.object"),
                # Arrival order of this manifest (spec 004 FR-4)
                "released_at": sfn.JsonPath.string_at("$$.Execution.StartTime"),
            },
            "$.load",
        )
        drop_batch = invoke("DropBatch", {"action": "drop", **snap}, "$.drop")
        start_load = invoke(
            "StartLoad",
            {"action": "start", "snap": sfn.JsonPath.object_at("$.load")},
            "$.load",
        )

        wait = sfn.Wait(
            self,
            "WaitForLoad",
            time=sfn.WaitTime.duration(cdk.Duration.seconds(wait_seconds)),
        )
        check_load = invoke(
            "CheckLoad",
            {"action": "check", "load_id": sfn.JsonPath.string_at("$.load.load_id")},
            "$.check",
        )

        record_common = {
            **snap,
            "load_id": sfn.JsonPath.string_at("$.load.load_id"),
            "prefix": sfn.JsonPath.string_at("$.load.prefix"),
            "etag": sfn.JsonPath.string_at("$.load.etag"),
            "load_status": sfn.JsonPath.string_at("$.check.load_status"),
            "overall_status": sfn.JsonPath.object_at("$.check.overall_status"),
        }
        record_success = invoke(
            "RecordSuccess", {"action": "record", **record_common}, "$.record"
        )
        record_failure = invoke(
            "RecordFailure", {"action": "record", **record_common}, "$.record"
        )
        # The only way into the registry: after a completed load, or for an already-loaded tag.
        publish = invoke("Publish", {"action": "publish", **snap}, "$.publish")
        # A step failed after Prepare claimed the row: mark it error so the tag can retry.
        abort = invoke(
            "Abort",
            {
                "action": "abort",
                **snap,
                "error": sfn.JsonPath.object_at("$.error"),
            },
            "$.abort",
        )

        # Malformed key or unrecoverable Lambda error before a row is claimed.
        bad_input = sfn.Fail(
            self, "BadManifestKey", cause="Could not prepare load for manifest event"
        )
        aborted = sfn.Fail(
            self,
            "LoadAborted",
            cause="A pipeline step failed after the snapshot was claimed",
        )
        skipped = sfn.Succeed(
            self, "LoadSkipped", comment="Duplicate event or load already in progress"
        )
        published = sfn.Succeed(self, "LoadSucceeded")

        # Transient Lambda/HTTP failures retry; a bad manifest key stops fast.
        transient = [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
        ]

        def retry(task, *, http=False, interval=3, attempts=3):
            task.add_retry(
                errors=transient + (["States.TaskFailed"] if http else []),
                max_attempts=attempts,
                interval=cdk.Duration.seconds(interval),
                backoff_rate=2.0,
            )

        retry(prepare)
        prepare.add_catch(bad_input, errors=["States.ALL"])
        retry(start_load)
        retry(check_load, http=True, interval=5, attempts=4)
        retry(drop_batch, http=True, interval=10)
        retry(record_success)
        retry(record_failure)
        retry(publish, http=True)
        retry(abort, http=True)
        for task in (
            start_load,
            check_load,
            drop_batch,
            record_success,
            record_failure,
        ):
            task.add_catch(abort, errors=["States.ALL"], result_path="$.error")
        # A publish failure leaves a complete row: re-uploading the manifest retries the
        # publish. It must not abort, which would mark a loaded tag for drop and reload.

        abort.next(aborted)
        publish.next(published)
        record_success.next(publish)
        record_failure.next(
            sfn.Fail(self, "LoadFailed", cause="Neptune bulk load did not complete")
        )

        load_complete = (
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
        start_load.next(wait)
        wait.next(check_load).next(load_complete)

        drop_batch.next(
            sfn.Choice(self, "GraphCleared?")
            .when(sfn.Condition.boolean_equals("$.drop.remaining", True), drop_batch)
            .otherwise(start_load)
        )

        def route_is(value):
            return sfn.Condition.string_equals("$.load.route", value)

        definition = prepare.next(
            sfn.Choice(self, "Route")
            .when(route_is("skip"), skipped)
            .when(route_is("publish"), publish)
            .when(route_is("drop"), drop_batch)
            .when(route_is("load"), start_load)
            .otherwise(bad_input)
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
