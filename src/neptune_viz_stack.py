import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct


class NeptuneVizStack(cdk.Stack):
    """
    Open-source Graph Explorer (https://github.com/aws/graph-explorer) for
    visually browsing the Neptune knowledge graph.

    Runs the official `public.ecr.aws/neptune/graph-explorer` container as a
    single Fargate task in private subnets. The task signs its Neptune requests
    with SigV4 (the cluster has IAM DB auth enabled), so a least-privilege task
    role with read-only `neptune-db` actions is enough.

    The UI is fronted by an internet-facing Application Load Balancer whose
    security group only admits Sage's network egress IP — non-technical users
    reach it over the VPN at a plain `http://<alb>/explorer` URL, and nobody
    else on the internet can connect. Graph Explorer has no auth of its own, so
    the security-group IP allow-list IS the access control.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_security_group: ec2.SecurityGroup,
        neptune_cluster_resource_id: str,
        neptune_endpoint: str,
        viz_config: dict,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        allowed_cidrs = viz_config.get("allowed_cidrs", [])
        if not allowed_cidrs:
            raise ValueError(
                "NEPTUNE_VIZ.allowed_cidrs must be non-empty when NEPTUNE_VIZ.enabled is true"
            )
        graph_connection_url = f"https://{neptune_endpoint}:8182"
        # -------------------
        # Security groups
        # -------------------
        # ALB: only Sage's network egress IP(s) may reach the UI on port 80.
        alb_security_group = ec2.SecurityGroup(
            self,
            "VizAlbSecurityGroup",
            vpc=vpc,
            description="Graph Explorer ALB — Sage VPN/egress access only",
            allow_all_outbound=True,
        )
        for cidr in allowed_cidrs:
            alb_security_group.add_ingress_rule(
                peer=ec2.Peer.ipv4(cidr),
                connection=ec2.Port.tcp(80),
                description=f"Graph Explorer UI from {cidr}",
            )

        # Fargate task SG: ingress only from the ALB (added below by add_targets),
        # egress open so it can reach Neptune, public ECR, and CloudWatch.
        service_security_group = ec2.SecurityGroup(
            self,
            "VizServiceSecurityGroup",
            vpc=vpc,
            description="Security group for the Graph Explorer Fargate task",
            allow_all_outbound=True,
        )

        # Neptune ingress from the Graph Explorer task. Rule lives in this stack
        # (not the Neptune stack) to avoid cross-stack cyclic references — same
        # pattern as the Lambda and SageMaker stacks.
        ec2.CfnSecurityGroupIngress(
            self,
            "NeptuneIngressFromViz",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=service_security_group.security_group_id,
            description="Neptune access from Graph Explorer",
        )

        # -------------------
        # Task role — read-only Neptune via SigV4
        # -------------------
        # AWS's reference policy grants neptune-db:*, but Graph Explorer only
        # reads, so we scope to the same read actions the query Lambda uses.
        task_role = iam.Role(
            self,
            "VizTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Graph Explorer task role — read-only Neptune access",
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:ReadDataViaQuery",
                    "neptune-db:GetEngineStatus",
                    "neptune-db:GetQueryStatus",
                ],
                resources=[
                    f"arn:{self.partition}:neptune-db:{self.region}:{self.account}:{neptune_cluster_resource_id}/*"
                ],
            )
        )

        # -------------------
        # ECS cluster + Fargate task
        # -------------------
        cluster = ecs.Cluster(self, "VizCluster", vpc=vpc, container_insights=True)

        task_definition = ecs.FargateTaskDefinition(
            self,
            "VizTaskDef",
            cpu=viz_config.get("cpu", 1024),
            memory_limit_mib=viz_config.get("memory_mib", 3072),
            task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                # Match the official guide / published image architecture.
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        container = task_definition.add_container(
            "graph-explorer",
            # Configurable so environments can pin a known-good tag or digest
            # for reproducible deploys (e.g. `...graph-explorer@sha256:...`).
            # Defaults to `:latest`.
            image=ecs.ContainerImage.from_registry(
                viz_config.get("image", "public.ecr.aws/neptune/graph-explorer:latest")
            ),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="graph-explorer",
                log_retention=logs.RetentionDays.ONE_MONTH,
            ),
            environment={
                # Serve plain HTTP — TLS terminates nowhere; traffic stays on the
                # Sage network between the VPN client and the ALB.
                "GRAPH_EXP_HTTPS_CONNECTION": "false",
                "PROXY_SERVER_HTTPS_CONNECTION": "false",
                # App is served under /explorer.
                "GRAPH_EXP_ENV_ROOT_FOLDER": "/explorer",
                # Default connection to our Neptune cluster, signed with SigV4.
                "USING_PROXY_SERVER": "true",
                "GRAPH_CONNECTION_URL": graph_connection_url,
                "GRAPH_TYPE": "sparql",
                "IAM": "true",
                "SERVICE_TYPE": "neptune-db",
                "AWS_REGION": self.region,
                # Lock the proxy so it will ONLY forward to our cluster.
                "PROXY_SERVER_ALLOWED_DB_ORIGINS": graph_connection_url,
                # Generous timeout for slow queries on the large graph.
                "GRAPH_EXP_FETCH_REQUEST_TIMEOUT": "240000",
                # Structured logs for CloudWatch.
                "LOG_STYLE": "cloudwatch",
                # PUBLIC_OR_PROXY_ENDPOINT is set below — it needs the ALB DNS name.
            },
        )
        container.add_port_mappings(ecs.PortMapping(container_port=80))

        # -------------------
        # Internet-facing ALB (IP-restricted) → Fargate
        # -------------------
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "VizAlb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        # The browser talks to Graph Explorer's proxy via this URL, so it must be
        # the externally reachable ALB address. Resolved by CloudFormation.
        container.add_environment(
            "PUBLIC_OR_PROXY_ENDPOINT", f"http://{alb.load_balancer_dns_name}"
        )
        # HOST is used for the self-signed cert; harmless over plain HTTP but set
        # it to the ALB name for consistency.
        container.add_environment("HOST", alb.load_balancer_dns_name)

        service = ecs.FargateService(
            self,
            "VizService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=viz_config.get("desired_count", 1),
            security_groups=[service_security_group],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            assign_public_ip=False,
            # Allows `aws ecs execute-command` for shell debugging of the task.
            enable_execute_command=True,
        )

        listener = alb.add_listener(
            "VizListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,  # ingress is controlled by alb_security_group, not 0.0.0.0/0
        )
        listener.add_targets(
            "VizTarget",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service],
            health_check=elbv2.HealthCheck(
                path="/explorer/",
                healthy_http_codes="200",
            ),
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "GraphExplorerUrl",
            value=f"http://{alb.load_balancer_dns_name}/explorer",
            description="Graph Explorer URL (reachable from the Sage VPN only)",
        )
        cdk.CfnOutput(
            self,
            "VizClusterName",
            value=cluster.cluster_name,
            description="ECS cluster running Graph Explorer",
        )
        cdk.CfnOutput(
            self,
            "VizServiceName",
            value=service.service_name,
            description="ECS service running Graph Explorer",
        )
