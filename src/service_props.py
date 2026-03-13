from dataclasses import dataclass
from typing import List, Optional, Sequence

from aws_cdk import aws_ecs as ecs

CONTAINER_LOCATION_PATH_ID = "path://"


@dataclass
class ServiceSecret:
    """
    Holds onto configuration for the secrets to be used in the container.

    Attributes:
      secret_name: The name of the secret as stored in the AWS Secrets Manager.
      environment_key: The name of the environment variable to be set within the container.
    """

    secret_name: str
    """The name of the secret as stored in the AWS Secrets Manager."""

    environment_key: str
    """The name of the environment variable to be set within the container."""


@dataclass
class ContainerVolume:
    """
    Holds onto configuration for a volume used in the container.

    Attributes:
      path: The path on the container to mount the host volume at.
      size: The size of the volume in GiB.
      read_only: Container has read-only access to the volume, set to `false` for write access.
    """

    path: str
    """The path on the container to mount the host volume at."""

    size: int = 15
    """The size of the volume in GiB."""

    read_only: bool = False
    """Container has read-only access to the volume, set to `false` for write access."""


class ServiceProps:
    """
    ECS service properties

    container_name: the name of the container
    container_location:
      supports "path://" for building container from local (i.e. path://docker/MyContainer)
      supports docker registry references (i.e. ghcr.io/sage-bionetworks/app:latest)
    container_port: the container application port
    ecs_task_cpu: the ECS task CPU in millicores (i.e. 1024 = 1 vCPU)
    ecs_task_memory: the ECS task memory
    container_env_vars: a json dictionary of environment variables to pass into the container
      i.e. {"EnvA": "EnvValueA", "EnvB": "EnvValueB"}
    container_secrets: List of `ServiceSecret` resources to pull from AWS secrets manager
    container_volumes: List of `ContainerVolume` resources to mount into the container
    auto_scale_min_capacity: the fargate auto scaling minimum capacity
    auto_scale_max_capacity: the fargate auto scaling maximum capacity
    container_command: Optional commands to run during the container startup
    container_healthcheck: Optional health check configuration for the container
    """

    def __init__(
        self,
        container_name: str,
        container_location: str,
        container_port: int,
        container_env_vars: dict = None,
        container_secrets: List[ServiceSecret] = None,
        container_volumes: List[ContainerVolume] = None,
        ecs_task_cpu: int = 256,
        ecs_task_memory: int = 512,
        auto_scale_min_capacity: int = 1,
        auto_scale_max_capacity: int = 1,
        container_command: Optional[Sequence[str]] = None,
        container_healthcheck: Optional[ecs.HealthCheck] = None,
    ) -> None:
        self.container_name = container_name
        self.container_port = container_port
        self.ecs_task_cpu = ecs_task_cpu
        self.ecs_task_memory = ecs_task_memory
        if CONTAINER_LOCATION_PATH_ID in container_location:
            container_location = container_location.removeprefix(
                CONTAINER_LOCATION_PATH_ID
            )
        self.container_location = container_location

        if container_env_vars is None:
            self.container_env_vars = {}
        else:
            self.container_env_vars = container_env_vars

        if container_secrets is None:
            self.container_secrets = []
        else:
            self.container_secrets = container_secrets

        if container_volumes is None:
            self.container_volumes = []
        else:
            self.container_volumes = container_volumes

        self.auto_scale_min_capacity = auto_scale_min_capacity
        self.auto_scale_max_capacity = auto_scale_max_capacity
        self.container_command = container_command
        self.container_healthcheck = container_healthcheck
