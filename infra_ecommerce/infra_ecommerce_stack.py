import json
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
    aws_iam as iam,
)
from constructs import Construct


class InfraEcommerceStack(Stack):
    """Infraestructura simplificada sin CodeDeploy (solo Rolling ECS)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- VPC ---
        vpc = ec2.Vpc(
            self,
            "EcommerceVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        # Endpoints (ECR, S3, Logs)
        ec2.InterfaceVpcEndpoint(self, "EcrApiEndpoint", vpc=vpc, service=ec2.InterfaceVpcEndpointAwsService.ECR)
        ec2.InterfaceVpcEndpoint(self, "EcrDkrEndpoint", vpc=vpc, service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER)
        ec2.InterfaceVpcEndpoint(self, "LogsEndpoint", vpc=vpc, service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS)
        vpc.add_gateway_endpoint("S3GatewayEndpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        cluster = ecs.Cluster(self, "EcommerceCluster", vpc=vpc)

        # --- ECR repos ---
        fe_repo = ecr.Repository(self, "FrontendRepo", repository_name="ecommerce-frontend")
        be_repo = ecr.Repository(self, "BackendRepo", repository_name="ecommerce-backend")

        # --- Secrets DB ---
        db_secret = secretsmanager.Secret(
            self,
            "DbSecret",
            secret_name="ecommerce/mysql",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "appuser"}),
                generate_string_key="password",
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- SGs ---
        fe_alb_sg = ec2.SecurityGroup(self, "FeAlbSg", vpc=vpc, description="FE ALB", allow_all_outbound=True)
        fe_alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP")

        be_alb_sg = ec2.SecurityGroup(self, "BeAlbSg", vpc=vpc, description="BE ALB", allow_all_outbound=True)
        be_alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP")

        fe_svc_sg = ec2.SecurityGroup(self, "FeSvcSg", vpc=vpc, description="FE service", allow_all_outbound=True)
        fe_svc_sg.add_ingress_rule(fe_alb_sg, ec2.Port.tcp(3000), "ALB to FE")

        be_svc_sg = ec2.SecurityGroup(self, "BeSvcSg", vpc=vpc, description="BE service", allow_all_outbound=True)
        be_svc_sg.add_ingress_rule(be_alb_sg, ec2.Port.tcp(4000), "ALB to BE")

        db_sg = ec2.SecurityGroup(self, "DbSg", vpc=vpc, description="DB SG", allow_all_outbound=True)
        db_sg.add_ingress_rule(be_svc_sg, ec2.Port.tcp(3306), "BE to MySQL")

        # --- ALBs ---
        fe_alb = elbv2.ApplicationLoadBalancer(self, "FeAlb", vpc=vpc, internet_facing=True, security_group=fe_alb_sg)
        fe_listener = fe_alb.add_listener("FeHttp", port=80, open=True)

        be_alb = elbv2.ApplicationLoadBalancer(self, "BeAlb", vpc=vpc, internet_facing=True, security_group=be_alb_sg)
        be_listener = be_alb.add_listener("BeHttp", port=80, open=True)

        # --- DB ---
        db = rds.DatabaseInstance(
            self,
            "EcommerceDb",
            engine=rds.DatabaseInstanceEngine.mysql(version=rds.MysqlEngineVersion.VER_8_0_43),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            credentials=rds.Credentials.from_secret(db_secret, username="appuser"),
            database_name="ecommerce",
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO),
            allocated_storage=20,
            max_allocated_storage=100,
            security_groups=[db_sg],
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )

        # --- Roles de ejecución ---
        fe_exec = iam.Role(
            self,
            "FeExecRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        fe_exec.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
        )

        be_exec = iam.Role(
            self,
            "BeExecRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        be_exec.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
        )

        # --- Task definitions ---
        fe_task = ecs.FargateTaskDefinition(
            self, "FeTask", cpu=512, memory_limit_mib=1024, execution_role=fe_exec
        )
        fe_container = fe_task.add_container(
            "FrontendContainer",
            image=ecs.ContainerImage.from_ecr_repository(fe_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Frontend"),
            environment={"API_BASE_URL": f"http://{be_alb.load_balancer_dns_name}"},
        )
        fe_container.add_port_mappings(ecs.PortMapping(container_port=3000))

        be_task = ecs.FargateTaskDefinition(
            self, "BeTask", cpu=512, memory_limit_mib=1024, execution_role=be_exec
        )
        be_container = be_task.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_ecr_repository(be_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Backend"),
            environment={
                "DB_HOST": db.instance_endpoint.hostname,
                "DB_PORT": str(db.instance_endpoint.port),
                "DB_NAME": "ecommerce",
            },
            secrets={
                "DB_USERNAME": ecs.Secret.from_secrets_manager(db_secret, field="username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, field="password"),
            },
        )
        be_container.add_port_mappings(ecs.PortMapping(container_port=4000))

        # --- Services (Rolling update) ---
        fe_service = ecs.FargateService(
            self,
            "FrontendService",
            cluster=cluster,
            task_definition=fe_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[fe_svc_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        be_service = ecs.FargateService(
            self,
            "BackendService",
            cluster=cluster,
            task_definition=be_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[be_svc_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        # --- ALB Targets ---
        fe_listener.add_targets(
            "FeTargets",
            port=3000,
            protocol=elbv2.ApplicationProtocol.HTTP,  # <-- agrega esto
            targets=[fe_service],
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200-399",
            ),
        )

        # BE: listener -> targets
        be_listener.add_targets(
            "BeTargets",
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,  # <-- agrega esto
            targets=[be_service],
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200-499",
            ),
        )

        # --- Outputs ---
        CfnOutput(self, "FrontendUrl", value=f"http://{fe_alb.load_balancer_dns_name}")
        CfnOutput(self, "BackendUrl", value=f"http://{be_alb.load_balancer_dns_name}")
        CfnOutput(self, "FrontendEcrUri", value=fe_repo.repository_uri)
        CfnOutput(self, "BackendEcrUri", value=be_repo.repository_uri)
        CfnOutput(self, "DatabaseSecretArn", value=db_secret.secret_arn)
        CfnOutput(self, "DatabaseEndpoint", value=db.instance_endpoint.socket_address)
        # --- Outputs adicionales: Security Groups (para usarlos en CI) ---
        CfnOutput(
            self,
            "FrontendSecurityGroupId",
            value=fe_svc_sg.security_group_id,
            description="Security Group ID for the Frontend ECS Service",
        )
        CfnOutput(
            self,
            "BackendSecurityGroupId",
            value=be_svc_sg.security_group_id,
            description="Security Group ID for the Backend ECS Service",
        )
        CfnOutput(
            self,
            "DatabaseSecurityGroupId",
            value=db_sg.security_group_id,
            description="Security Group ID for the MySQL Database",
        )

