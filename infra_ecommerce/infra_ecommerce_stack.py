import json

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
)
from aws_cdk.aws_ecr_assets import Platform
from constructs import Construct


class InfraEcommerceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Networking
        vpc = ec2.Vpc(
            self,
            "EcommerceVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        cluster = ecs.Cluster(self, "EcommerceCluster", vpc=vpc)

        # Secrets for database credentials
        database_secret = secretsmanager.Secret(
            self,
            "DatabaseCredentials",
            secret_name="ecommerce/mysql",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "appuser"}),
                generate_string_key="password",
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Security groups
        load_balancer_sg = ec2.SecurityGroup(
            self,
            "LoadBalancerSecurityGroup",
            vpc=vpc,
            description="Allow HTTP access to the load balancer",
            allow_all_outbound=True,
        )
        load_balancer_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "Allow HTTP traffic from the internet",
        )

        frontend_service_sg = ec2.SecurityGroup(
            self,
            "FrontendServiceSecurityGroup",
            vpc=vpc,
            description="Allow traffic from the load balancer to the frontend service",
            allow_all_outbound=True,
        )
        frontend_service_sg.add_ingress_rule(
            load_balancer_sg,
            ec2.Port.tcp(3000),
            "Allow ALB to reach the frontend containers",
        )

        backend_service_sg = ec2.SecurityGroup(
            self,
            "BackendServiceSecurityGroup",
            vpc=vpc,
            description="Allow traffic from the load balancer to the backend service",
            allow_all_outbound=True,
        )
        backend_service_sg.add_ingress_rule(
            load_balancer_sg,
            ec2.Port.tcp(4000),
            "Allow ALB to reach the backend containers",
        )

        database_security_group = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=vpc,
            description="Restrict database access to backend service",
            allow_all_outbound=True,
        )
        database_security_group.add_ingress_rule(
            backend_service_sg,
            ec2.Port.tcp(3306),
            "Allow backend containers to connect to MySQL",
        )

        # Public load balancer to expose frontend and backend
        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "PublicLoadBalancer",
            vpc=vpc,
            internet_facing=True,
            security_group=load_balancer_sg,
        )

        listener = load_balancer.add_listener("HttpListener", port=80, open=True)

        # Database instance (dev-friendly settings)
        database = rds.DatabaseInstance(
            self,
            "EcommerceDatabase",
            engine=rds.DatabaseInstanceEngine.mysql(
                version=rds.MysqlEngineVersion.VER_8_0_43
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            credentials=rds.Credentials.from_secret(
                database_secret, username="appuser"
            ),
            database_name="ecommerce",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize.MICRO,
            ),
            allocated_storage=20,
            max_allocated_storage=100,
            multi_az=False,
            security_groups=[database_security_group],
            publicly_accessible=False,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            delete_automated_backups=True,
        )

        # Task definitions
        frontend_task = ecs.FargateTaskDefinition(
            self,
            "FrontendTaskDefinition",
            cpu=512,
            memory_limit_mib=1024,
        )
        frontend_container = frontend_task.add_container(
            "FrontendContainer",
            image=ecs.ContainerImage.from_asset(
                "container_images/frontend", platform=Platform.LINUX_AMD64
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Frontend"),
        )
        frontend_container.add_port_mappings(
            ecs.PortMapping(container_port=3000)
        )
        frontend_container.add_environment(
            "API_BASE_URL", f"http://{load_balancer.load_balancer_dns_name}/api"
        )

        backend_task = ecs.FargateTaskDefinition(
            self,
            "BackendTaskDefinition",
            cpu=512,
            memory_limit_mib=1024,
        )
        backend_container = backend_task.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_asset(
                "container_images/backend", platform=Platform.LINUX_AMD64
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Backend"),
            environment={
                "DB_HOST": database.instance_endpoint.hostname,
                "DB_PORT": str(database.instance_endpoint.port),
                "DB_NAME": "ecommerce",
            },
            secrets={
                "DB_USERNAME": ecs.Secret.from_secrets_manager(
                    database_secret, field="username"
                ),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(
                    database_secret, field="password"
                ),
            },
        )
        backend_container.add_port_mappings(
            ecs.PortMapping(container_port=4000)
        )

        # ECS services
        frontend_service = ecs.FargateService(
            self,
            "FrontendService",
            cluster=cluster,
            task_definition=frontend_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[frontend_service_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            ),
        )

        backend_service = ecs.FargateService(
            self,
            "BackendService",
            cluster=cluster,
            task_definition=backend_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[backend_service_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            ),
        )

        # Target groups and routing
        frontend_target_group = elbv2.ApplicationTargetGroup(
            self,
            "FrontendTargetGroup",
            vpc=vpc,
            port=3000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200-399",
                interval=Duration.seconds(30),
            ),
        )
        frontend_service.attach_to_application_target_group(frontend_target_group)

        backend_target_group = elbv2.ApplicationTargetGroup(
            self,
            "BackendTargetGroup",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200-399",
                interval=Duration.seconds(30),
            ),
        )
        backend_service.attach_to_application_target_group(backend_target_group)

        listener.add_target_groups(
            "DefaultFrontendTarget",
            target_groups=[frontend_target_group],
        )

        listener.add_target_groups(
            "BackendPathTarget",
            priority=10,
            conditions=[
                elbv2.ListenerCondition.path_patterns(["/api*", "/api/*"])
            ],
            target_groups=[backend_target_group],
        )

        # Outputs
        CfnOutput(
            self,
            "LoadBalancerUrl",
            value=f"http://{load_balancer.load_balancer_dns_name}",
            description="Public endpoint for the ecommerce frontend",
        )

        CfnOutput(
            self,
            "FrontendDockerContext",
            value="container_images/frontend",
            description="Path to the Docker context used to build the frontend container",
        )

        CfnOutput(
            self,
            "BackendDockerContext",
            value="container_images/backend",
            description="Path to the Docker context used to build the backend container",
        )

        CfnOutput(
            self,
            "DatabaseSecretArn",
            value=database_secret.secret_arn,
            description="Secrets Manager ARN storing the database credentials",
        )

        CfnOutput(
            self,
            "DatabaseEndpoint",
            value=database.instance_endpoint.socket_address,
            description="Endpoint and port for the MySQL database",
        )
