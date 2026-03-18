import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_neptune as neptune
from constructs import Construct


class NeptuneStack(cdk.Stack):
    """
    Amazon Neptune graph database stack
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_config: dict,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # Security Group for Neptune
        # -------------------
        self.neptune_security_group = ec2.SecurityGroup(
            self,
            "NeptuneSecurityGroup",
            vpc=vpc,
            description="Security group for Neptune cluster",
            allow_all_outbound=False,
        )

        # No broad ingress rules here. Each consumer stack (bastion, Lambda, etc.)
        # adds a targeted SG-to-SG rule on port 8182 for least-privilege access.

        # -------------------
        # Neptune Subnet Group
        # -------------------
        self.neptune_subnet_group = neptune.CfnDBSubnetGroup(
            self,
            "NeptuneSubnetGroup",
            db_subnet_group_description="Subnet group for Neptune cluster",
            subnet_ids=[subnet.subnet_id for subnet in vpc.private_subnets],
            tags=[
                cdk.CfnTag(key="Name", value=f"{construct_id}-subnet-group"),
            ],
        )

        # -------------------
        # Neptune Parameter Group (optional)
        # -------------------
        if neptune_config.get("create_parameter_group", False):
            engine_version = neptune_config.get("engine_version", "1.3.2.1")
            # Family is derived from the major.minor version, e.g. "1.3.2.1" -> "neptune1.3"
            version_parts = engine_version.split(".")
            parameter_group_family = f"neptune{version_parts[0]}.{version_parts[1]}"

            self.parameter_group = neptune.CfnDBParameterGroup(
                self,
                "NeptuneParameterGroup",
                family=parameter_group_family,
                description="Parameter group for Neptune cluster",
                parameters=neptune_config.get("parameter_group_parameters", {}),
                tags=[
                    cdk.CfnTag(key="Name", value=f"{construct_id}-parameter-group"),
                ],
            )

        # -------------------
        # Neptune Cluster
        # -------------------
        self.neptune_cluster = neptune.CfnDBCluster(
            self,
            "NeptuneCluster",
            engine_version=neptune_config.get("engine_version", "1.3.2.1"),
            db_subnet_group_name=self.neptune_subnet_group.ref,
            vpc_security_group_ids=[self.neptune_security_group.security_group_id],
            backup_retention_period=neptune_config.get("backup_retention_days", 7),
            preferred_backup_window=neptune_config.get(
                "preferred_backup_window", "03:00-04:00"
            ),
            preferred_maintenance_window=neptune_config.get(
                "preferred_maintenance_window", "sun:04:00-sun:05:00"
            ),
            deletion_protection=neptune_config.get("deletion_protection", True),
            enable_cloudwatch_logs_exports=neptune_config.get(
                "cloudwatch_logs_exports", ["audit", "slowquery"]
            ),
            iam_auth_enabled=neptune_config.get("iam_auth_enabled", True),
            storage_encrypted=neptune_config.get("storage_encrypted", True),
            db_cluster_parameter_group_name=(
                self.parameter_group.ref
                if neptune_config.get("create_parameter_group", False)
                else None
            ),
            tags=[
                cdk.CfnTag(key="Name", value=f"{construct_id}-cluster"),
            ],
        )

        # -------------------
        # Neptune Instances
        # -------------------
        self.neptune_instances = []
        instance_count = neptune_config.get("instance_count", 1)
        instance_class = neptune_config.get("instance_class", "db.t3.medium")

        for i in range(instance_count):
            instance = neptune.CfnDBInstance(
                self,
                f"NeptuneInstance{i + 1}",
                db_instance_class=instance_class,
                db_cluster_identifier=self.neptune_cluster.ref,
                availability_zone=vpc.availability_zones[
                    i % len(vpc.availability_zones)
                ],
                auto_minor_version_upgrade=neptune_config.get(
                    "auto_minor_version_upgrade", True
                ),
                tags=[
                    cdk.CfnTag(key="Name", value=f"{construct_id}-instance-{i + 1}"),
                ],
            )
            self.neptune_instances.append(instance)

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "NeptuneClusterEndpoint",
            value=self.neptune_cluster.attr_endpoint,
            description="Neptune cluster endpoint",
        )

        cdk.CfnOutput(
            self,
            "NeptuneClusterReadEndpoint",
            value=self.neptune_cluster.attr_read_endpoint,
            description="Neptune cluster read endpoint",
        )

        cdk.CfnOutput(
            self,
            "NeptuneClusterPort",
            value=self.neptune_cluster.attr_port,
            description="Neptune cluster port",
        )

        cdk.CfnOutput(
            self,
            "NeptuneSecurityGroupId",
            value=self.neptune_security_group.security_group_id,
            description="Neptune security group ID",
        )
