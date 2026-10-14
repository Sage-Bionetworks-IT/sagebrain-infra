"""Edge stack (src/api_edge_stack.py): routes match the contract, every threshold comes from it."""

import json
import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_sqs as sqs
from aws_cdk.assertions import Match, Template

from src.api_edge_stack import ApiEdgeStack
from src.contract import LIMITS, operations, public_operations
from src.utils import load_context_config

ALARMS = load_context_config("dev")["API_EDGE_ALARMS"]
ALARM_EMAIL = load_context_config("dev")["ALARM_EMAIL"]


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    jobs_stack = cdk.Stack(app, "TestJobsStack")

    def table(name):
        return dynamodb.Table(
            jobs_stack,
            name,
            partition_key=dynamodb.Attribute(
                name="job_id", type=dynamodb.AttributeType.STRING
            ),
        )

    stack = ApiEdgeStack(
        app,
        "TestApiEdgeStack",
        query_job_table=table("QueryJobs"),
        query_job_queue=sqs.Queue(jobs_stack, "QueryQueue"),
        ask_job_table=table("AskJobs"),
        ask_job_queue=sqs.Queue(jobs_stack, "AskQueue"),
        synapse_team_id="273957",
        stage_name="dev",
        alarm_thresholds=ALARMS,
        alarm_email=ALARM_EMAIL,
    )
    return Template.from_stack(stack)


def _resource_paths(template) -> dict:
    """Logical id → full path for every API Gateway resource, root included as ''."""
    resources = template.find_resources("AWS::ApiGateway::Resource")
    paths = {}

    def path_of(ref):
        if "Fn::GetAtt" in ref:  # the RestApi's RootResourceId
            return ""
        logical_id = ref["Ref"]
        if logical_id not in paths:
            props = resources[logical_id]["Properties"]
            paths[logical_id] = f"{path_of(props['ParentId'])}/{props['PathPart']}"
        return paths[logical_id]

    return path_of


def _methods(template) -> dict:
    """(path, METHOD) → method properties, for every route (not OPTIONS or not-found ANY)."""
    path_of = _resource_paths(template)
    methods = {}
    for method in template.find_resources("AWS::ApiGateway::Method").values():
        props = method["Properties"]
        if props["HttpMethod"] in ("OPTIONS", "ANY"):
            continue
        methods[(path_of(props["ResourceId"]) or "/", props["HttpMethod"])] = props
    return methods


def _function_logical_id(template, handler) -> str:
    (logical_id,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler}}
    )
    return logical_id


# ---------------------------------------------------------------------------
# Routes vs the contract (R-14, FR-9)
# ---------------------------------------------------------------------------


def test_stage_is_named_after_the_env(template):
    template.has_resource_properties("AWS::ApiGateway::Stage", {"StageName": "dev"})


def test_routes_equal_the_contract(template):
    assert set(_methods(template)) == operations()


def test_every_other_path_and_method_is_a_mock_404(template):
    # D-3: with a Bearer header API Gateway answers unrouted requests with a 403 SigV4
    # parse error that no gateway response type can remap, so they need a route
    path_of = _resource_paths(template)
    resource_paths = {
        path_of({"Ref": r}) or "/"
        for r in template.find_resources("AWS::ApiGateway::Resource")
    }
    any_methods = {}
    for method in template.find_resources("AWS::ApiGateway::Method").values():
        props = method["Properties"]
        if props["HttpMethod"] == "ANY":
            any_methods[path_of(props["ResourceId"]) or "/"] = props
    assert set(any_methods) == resource_paths | {"/"}
    assert "/{proxy+}" in any_methods
    for path, props in any_methods.items():
        assert props["AuthorizationType"] == "NONE", path
        integration = props["Integration"]
        assert integration["Type"] == "MOCK", path
        (response,) = integration["IntegrationResponses"]
        assert response["StatusCode"] == "404", path
        assert response["ResponseTemplates"] == {
            "application/json": '{"message":"Not Found"}'
        }, path
        assert (
            response["ResponseParameters"][
                "method.response.header.Access-Control-Allow-Origin"
            ]
            == "'*'"
        ), path


def test_only_the_docs_routes_skip_the_authorizer(template):
    for operation, props in _methods(template).items():
        if operation in public_operations():
            assert props["AuthorizationType"] == "NONE", operation
        else:
            assert props["AuthorizationType"] == "CUSTOM", operation
            assert "AuthorizerId" in props, operation


@pytest.mark.parametrize(
    "operation,handler",
    [
        (("/query", "POST"), "jobs.submit_query"),
        (("/query/{job_id}", "GET"), "jobs.query_status"),
        (("/ask", "POST"), "jobs.submit_question"),
        (("/ask/{job_id}", "GET"), "jobs.ask_status"),
        (("/api", "GET"), "docs.handler"),
        (("/api/openapi.json", "GET"), "docs.handler"),
        (("/api/openapi.yaml", "GET"), "docs.handler"),
    ],
)
def test_each_route_invokes_its_handler(template, operation, handler):
    uri = _methods(template)[operation]["Integration"]["Uri"]
    assert _function_logical_id(template, handler) in str(uri)


def test_every_integration_times_out_at_the_request_timeout(template):
    for operation, props in _methods(template).items():
        assert (
            props["Integration"]["TimeoutInMillis"]
            == LIMITS["request_timeout_seconds"] * 1000
        ), operation


ALLOW_HEADERS = "'Content-Type,Authorization,X-Source,x-api-key'"


def test_cors_preflight_matches_the_old_apis(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "OPTIONS",
            "Integration": Match.object_like(
                {
                    "IntegrationResponses": [
                        Match.object_like(
                            {
                                "StatusCode": "204",
                                "ResponseParameters": {
                                    "method.response.header.Access-Control-Allow-Headers": ALLOW_HEADERS,
                                    "method.response.header.Access-Control-Allow-Origin": "'*'",
                                    "method.response.header.Access-Control-Allow-Methods": "'POST,GET,OPTIONS'",
                                },
                            }
                        )
                    ]
                }
            ),
        },
    )


# ---------------------------------------------------------------------------
# Stage: throttles (parity rows 1, 1s), metrics, logs (rows 12–15)
# ---------------------------------------------------------------------------


def _method_settings(template) -> dict:
    (stage,) = template.find_resources("AWS::ApiGateway::Stage").values()
    props = stage["Properties"]
    return {
        (s["ResourcePath"], s["HttpMethod"]): s for s in props["MethodSettings"]
    }, props


def test_stage_throttle_applies_to_every_method(template):
    settings, _ = _method_settings(template)
    default = settings[("/*", "*")]
    assert default["ThrottlingRateLimit"] == LIMITS["method_rate_rps"]
    assert default["ThrottlingBurstLimit"] == LIMITS["method_burst"]
    assert default["MetricsEnabled"] is True
    assert default["LoggingLevel"] == "ERROR"


@pytest.mark.parametrize("path", ["/~1query~1{job_id}", "/~1ask~1{job_id}"])
def test_status_methods_get_their_own_throttle(template, path):
    settings, _ = _method_settings(template)
    status = settings[(path, "GET")]
    assert status["ThrottlingRateLimit"] == LIMITS["status_rate_rps"]
    assert status["ThrottlingBurstLimit"] == LIMITS["status_burst"]
    assert status["MetricsEnabled"] is True
    assert status["LoggingLevel"] == "ERROR"


def test_only_the_status_methods_are_overridden(template):
    settings, _ = _method_settings(template)
    assert set(settings) == {
        ("/*", "*"),
        ("/~1query~1{job_id}", "GET"),
        ("/~1ask~1{job_id}", "GET"),
    }


def test_access_log_fields(template):
    _, props = _method_settings(template)
    log_format = props["AccessLogSetting"]["Format"]
    for field in (
        "requestId",
        "caller",
        "httpMethod",
        "ip",
        "protocol",
        "requestTime",
        "resourcePath",
        "responseLength",
        "status",
        "user",
    ):
        assert f'"{field}"' in log_format, field


def test_access_log_retention_is_one_month(template):
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})


# ---------------------------------------------------------------------------
# Authorizer (parity rows 5–7)
# ---------------------------------------------------------------------------


def test_authorizer_cache_is_off_and_reads_authorization(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Authorizer",
        {
            "Type": "REQUEST",
            "AuthorizerResultTtlInSeconds": 0,
            "IdentitySource": "method.request.header.Authorization",
        },
    )


def test_authorizer_lambda_timeout_and_team(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "authorizer.handler",
            "Timeout": LIMITS["auth_timeout_seconds"],
            "Environment": {"Variables": {"SYNAPSE_TEAM_ID": "273957"}},
        },
    )


def test_authorizer_has_no_machine_api_key(template):
    """D-4: Synapse bearer tokens are the only credential."""
    fn = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "authorizer.handler"}}
    )
    (props,) = [f["Properties"] for f in fn.values()]
    assert set(props["Environment"]["Variables"]) == {"SYNAPSE_TEAM_ID"}


# ---------------------------------------------------------------------------
# Gateway responses (parity rows 8–10, 1t; D-3, D-10, D-13, D-18)
# ---------------------------------------------------------------------------


def _gateway_responses(template) -> dict:
    return {
        r["Properties"]["ResponseType"]: r["Properties"]
        for r in template.find_resources("AWS::ApiGateway::GatewayResponse").values()
    }


@pytest.mark.parametrize(
    "response_type,status,message",
    [
        ("ACCESS_DENIED", "401", "Unauthorized"),
        ("UNAUTHORIZED", "401", "Unauthorized"),
        ("MISSING_AUTHENTICATION_TOKEN", "404", "Not Found"),
        ("AUTHORIZER_FAILURE", "503", "Service Unavailable"),
        ("AUTHORIZER_CONFIGURATION_ERROR", "503", "Service Unavailable"),
        ("THROTTLED", "429", "Too Many Requests"),
    ],
)
def test_gateway_response_status_and_body(template, response_type, status, message):
    response = _gateway_responses(template)[response_type]
    assert response["StatusCode"] == status
    assert response["ResponseTemplates"] == {
        "application/json": f'{{"message":"{message}"}}'
    }


def test_every_gateway_response_carries_cors(template):
    responses = _gateway_responses(template)
    assert {"DEFAULT_4XX", "DEFAULT_5XX"} <= set(responses)
    for response_type, props in responses.items():
        header = props["ResponseParameters"][
            "gatewayresponse.header.Access-Control-Allow-Origin"
        ]
        assert header == "'*'", response_type


def test_throttled_retry_after(template):
    params = _gateway_responses(template)["THROTTLED"]["ResponseParameters"]
    assert (
        params["gatewayresponse.header.Retry-After"]
        == f"'{LIMITS['throttled_retry_after_seconds']}'"
    )


@pytest.mark.parametrize(
    "response_type", ["AUTHORIZER_FAILURE", "AUTHORIZER_CONFIGURATION_ERROR"]
)
def test_authorizer_failure_retry_after(template, response_type):
    params = _gateway_responses(template)[response_type]["ResponseParameters"]
    assert (
        params["gatewayresponse.header.Retry-After"]
        == f"'{LIMITS['auth_failure_retry_after_seconds']}'"
    )


# ---------------------------------------------------------------------------
# Lambdas and least-privilege grants (constitution IV)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        "jobs.submit_query",
        "jobs.query_status",
        "jobs.submit_question",
        "jobs.ask_status",
        "docs.handler",
    ],
)
def test_handler_lambdas(template, handler):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": handler,
            "Runtime": "python3.11",
            "Architectures": ["arm64"],
            "Timeout": LIMITS["request_timeout_seconds"],
        },
    )


@pytest.mark.parametrize(
    "handler",
    [
        "jobs.submit_query",
        "jobs.query_status",
        "jobs.submit_question",
        "jobs.ask_status",
    ],
)
def test_job_lambdas_use_the_core_layer(template, handler):
    (layer_id,) = template.find_resources("AWS::Lambda::LayerVersion")
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": handler, "Layers": [{"Ref": layer_id}]},
    )


def test_no_lambda_runs_in_a_vpc(template):
    for fn in template.find_resources("AWS::Lambda::Function").values():
        assert "VpcConfig" not in fn["Properties"]


def _policy_actions(template, handler) -> set:
    role = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler}}
    )
    (role_ref,) = [f["Properties"]["Role"]["Fn::GetAtt"][0] for f in role.values()]
    actions = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        props = policy["Properties"]
        if {"Ref": role_ref} not in props["Roles"]:
            continue
        for statement in props["PolicyDocument"]["Statement"]:
            stmt_actions = statement["Action"]
            actions |= (
                {stmt_actions} if isinstance(stmt_actions, str) else set(stmt_actions)
            )
    return actions


@pytest.mark.parametrize("handler", ["jobs.submit_query", "jobs.submit_question"])
def test_submit_can_write_and_send_but_not_read(template, handler):
    actions = _policy_actions(template, handler)
    assert "dynamodb:PutItem" in actions
    assert "sqs:SendMessage" in actions
    assert "dynamodb:GetItem" not in actions
    assert "sqs:ReceiveMessage" not in actions


@pytest.mark.parametrize("handler", ["jobs.query_status", "jobs.ask_status"])
def test_status_can_only_read(template, handler):
    actions = _policy_actions(template, handler)
    assert "dynamodb:GetItem" in actions
    assert not any(a.startswith("sqs:") for a in actions)
    assert (
        not {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"} & actions
    )


def test_docs_lambda_has_no_data_access(template):
    actions = _policy_actions(template, "docs.handler")
    assert not any(a.startswith(("dynamodb:", "sqs:")) for a in actions)


def test_outputs(template):
    outputs = template.find_outputs("*")
    assert {"ApiBaseUrl", "ApiDocsUrl"} <= set(outputs)


@pytest.mark.parametrize("env_name,enabled", [("dev", True), ("prod", False)])
def test_edge_stack_is_enabled_per_env(env_name, enabled):
    from src.utils import load_context_config

    assert load_context_config(env_name)["API_EDGE"]["enabled"] is enabled


# ---------------------------------------------------------------------------
# Alarms and dashboard (Phase 3, constitution VII)
# ---------------------------------------------------------------------------


def _alarms(template) -> dict:
    """AlarmDescription prefix → alarm properties."""
    return {
        props["Properties"]["AlarmDescription"]: props["Properties"]
        for props in template.find_resources("AWS::CloudWatch::Alarm").values()
    }


def _alarm(template, metric_name, **dimensions) -> dict:
    (props,) = [
        props
        for props in _alarms(template).values()
        if props["MetricName"] == metric_name
        and all(
            {"Name": k, "Value": v} in props.get("Dimensions", [])
            for k, v in dimensions.items()
        )
    ]
    return props


def test_server_errors_alarm_excludes_synapse_outages(template):
    # Not API Gateway's 5XXError: that counts the authorizer's 503 while Synapse is down
    props = _alarm(template, "ServerErrors")
    assert props["Namespace"] == "SageBrain/Api"
    assert (props["Statistic"], props["Period"]) == ("Sum", 300)
    assert props["Threshold"] == ALARMS["server_errors_per_5_minutes"]
    assert (props["DatapointsToAlarm"], props["EvaluationPeriods"]) == (2, 3)
    assert not [
        a for a in _alarms(template).values() if a.get("MetricName") == "5XXError"
    ]


def test_synapse_unavailable_alarm_outlasts_weekly_maintenance(template):
    props = _alarm(template, "AuthUnavailable")
    assert (props["Statistic"], props["Period"], props["Threshold"]) == ("Sum", 300, 1)
    periods = ALARMS["synapse_outage_minutes"] // 5
    assert props["DatapointsToAlarm"] == props["EvaluationPeriods"] == periods
    # Synapse's maintenance is ~30 min every Sunday; it must not email
    assert periods * 5 == ALARMS["synapse_outage_minutes"] > 30


def test_request_volume_alarm(template):
    props = _alarm(template, "Count", ApiName="sagebrain-api", Stage="dev")
    assert (props["Statistic"], props["Period"]) == ("Sum", 86400)
    assert props["Threshold"] == ALARMS["requests_per_day"] == 1_000_000


@pytest.mark.parametrize("path", ["/query/{job_id}", "/ask/{job_id}"])
def test_status_throttled_alarm(template, path):
    props = _alarm(template, "Throttled", ResourcePath=path)
    assert props["Namespace"] == "SageBrain/Api"
    assert (props["Statistic"], props["Period"], props["Threshold"]) == ("Sum", 300, 1)
    periods = ALARMS["status_throttled_minutes"] // 5
    assert periods * 5 == ALARMS["status_throttled_minutes"]
    assert props["DatapointsToAlarm"] == props["EvaluationPeriods"] == periods > 1


def test_queue_age_alarms(template):
    alarms = [
        props
        for props in _alarms(template).values()
        if props["MetricName"] == "ApproximateAgeOfOldestMessage"
    ]
    assert len(alarms) == 2  # query and ask
    for props in alarms:
        assert (props["Statistic"], props["Period"]) == ("Maximum", 300)
        assert props["Threshold"] == ALARMS["queue_oldest_message_seconds"] == 300


def test_alarms_ignore_missing_data(template):
    for props in _alarms(template).values():
        assert props["TreatMissingData"] == "notBreaching"
        assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


def test_throttled_metric_filter_counts_429s_per_resource_path(template):
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        {
            "FilterPattern": '{ $.status = "429" }',
            "MetricTransformations": [
                {
                    "MetricNamespace": "SageBrain/Api",
                    "MetricName": "Throttled",
                    "MetricValue": "1",
                    "Dimensions": [{"Key": "ResourcePath", "Value": "$.resourcePath"}],
                }
            ],
        },
    )


@pytest.mark.parametrize(
    "metric_name,pattern",
    [
        ("ServerErrors", '{ ($.status = "5*") && ($.status != "503") }'),
        ("AuthUnavailable", '{ $.status = "503" }'),
    ],
)
def test_status_metric_filters(template, metric_name, pattern):
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        {
            "FilterPattern": pattern,
            "MetricTransformations": [
                {
                    "MetricNamespace": "SageBrain/Api",
                    "MetricName": metric_name,
                    "MetricValue": "1",
                }
            ],
        },
    )


def test_dashboard_has_every_route(template):
    (dashboard,) = template.find_resources("AWS::CloudWatch::Dashboard").values()
    body = json.dumps(dashboard["Properties"]["DashboardBody"])
    for path, method in operations():
        assert f"{method} {path}" in body


def test_every_alarm_emails_the_topic(template):
    (topic,) = template.find_resources("AWS::SNS::Topic")
    for props in _alarms(template).values():
        assert props["AlarmActions"] == [{"Ref": topic}]
    template.has_resource_properties(
        "AWS::SNS::Subscription",
        {"Protocol": "email", "Endpoint": ALARM_EMAIL, "TopicArn": {"Ref": topic}},
    )


@pytest.mark.parametrize(
    "env_name,email",
    [
        ("dev", "aws-sagebrain-dev@sagebase.org"),
        ("prod", "aws-sagebrain-prod@sagebase.org"),
    ],
)
def test_alarm_email_per_env(env_name, email):
    assert load_context_config(env_name)["ALARM_EMAIL"] == email
