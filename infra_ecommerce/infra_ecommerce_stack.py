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
    aws_codedeploy as codedeploy,
    aws_iam as iam,
)
from constructs import Construct


class InfraEcommerceStack(Stack):
    """
    Infra con despliegues independientes FE/BE (CodeDeploy ECS Blue/Green):
      - FE: ALB público (:80 prod, :9000 test)
      - BE: ALB público (:80 prod, :9000 test)  <-- expuesto para Postman/E2E
      - RDS MySQL privado + Secrets Manager
      - VPC Endpoints para Fargate privado sin NAT (ECR/S3/Logs)
      - ECR repos (frontend y backend)
      - ECS Fargate por servicio con DeploymentController=CODE_DEPLOY
      - Roles de ejecución de tareas (executionRole) creados explícitamente
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------
        # Networking
        # ---------------------------
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

        # --- VPC Endpoints para Fargate en subred privada (sin NAT) ---
        ec2.InterfaceVpcEndpoint(
            self,
            "EcrApiEndpoint",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )
        ec2.InterfaceVpcEndpoint(
            self,
            "EcrDkrEndpoint",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )
        ec2.InterfaceVpcEndpoint(
            self,
            "LogsEndpoint",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )
        vpc.add_gateway_endpoint(
            "S3GatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)],
        )
        # --- fin VPC Endpoints ---

        cluster = ecs.Cluster(self, "EcommerceCluster", vpc=vpc)

        # ---------------------------
        # ECR repos
        # ---------------------------
        frontend_repo = ecr.Repository(
            self, "FrontendRepo", repository_name="ecommerce-frontend"
        )
        backend_repo = ecr.Repository(
            self, "BackendRepo", repository_name="ecommerce-backend"
        )

        # ---------------------------
        # Secrets para DB
        # ---------------------------
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

        # ---------------------------
        # Security Groups
        # ---------------------------
        # FE ALB SG (público)
        fe_alb_sg = ec2.SecurityGroup(
            self,
            "FeAlbSg",
            vpc=vpc,
            description="Public ALB for Frontend",
            allow_all_outbound=True,
        )
        fe_alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP 80 public")

        # FE service SG
        fe_svc_sg = ec2.SecurityGroup(
            self,
            "FeServiceSg",
            vpc=vpc,
            description="Frontend service SG",
            allow_all_outbound=True,
        )
        fe_svc_sg.add_ingress_rule(fe_alb_sg, ec2.Port.tcp(3000), "ALB to FE 3000")

        # BE ALB SG (público para E2E/Postman)
        be_alb_sg = ec2.SecurityGroup(
            self,
            "BeAlbSg",
            vpc=vpc,
            description="Public ALB for Backend (E2E and Postman)",
            allow_all_outbound=True,
        )
        be_alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP 80 public")
        # Nota: no abrimos 9000; el listener test queda cerrado (open=False)

        # BE service SG
        be_svc_sg = ec2.SecurityGroup(
            self,
            "BeServiceSg",
            vpc=vpc,
            description="Backend service SG",
            allow_all_outbound=True,
        )
        be_svc_sg.add_ingress_rule(be_alb_sg, ec2.Port.tcp(4000), "BE ALB to BE 4000")

        # DB SG
        db_sg = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=vpc,
            description="DB for backend only",
            allow_all_outbound=True,
        )
        db_sg.add_ingress_rule(be_svc_sg, ec2.Port.tcp(3306), "Backend to MySQL 3306")

        # ---------------------------
        # ALBs + Listeners (separados por servicio)
        # ---------------------------
        # FE ALB (público)
        fe_alb = elbv2.ApplicationLoadBalancer(
            self,
            "FeAlb",
            vpc=vpc,
            internet_facing=True,
            security_group=fe_alb_sg,
        )
        fe_listener_prod = fe_alb.add_listener(
            "FeListenerProd", port=80, open=True, protocol=elbv2.ApplicationProtocol.HTTP
        )
        fe_listener_test = fe_alb.add_listener(
            "FeListenerTest", port=9000, open=False, protocol=elbv2.ApplicationProtocol.HTTP
        )

        # BE ALB (público)
        be_alb = elbv2.ApplicationLoadBalancer(
            self,
            "BeAlb",
            vpc=vpc,
            internet_facing=True,
            security_group=be_alb_sg,
        )
        be_listener_prod = be_alb.add_listener(
            "BeListenerProd", port=80, open=True, protocol=elbv2.ApplicationProtocol.HTTP
        )
        be_listener_test = be_alb.add_listener(
            "BeListenerTest", port=9000, open=False, protocol=elbv2.ApplicationProtocol.HTTP
        )

        # ---------------------------
        # RDS MySQL
        # ---------------------------
        database = rds.DatabaseInstance(
            self,
            "EcommerceDatabase",
            engine=rds.DatabaseInstanceEngine.mysql(
                version=rds.MysqlEngineVersion.VER_8_0_43
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            credentials=rds.Credentials.from_secret(database_secret, username="appuser"),
            database_name="ecommerce",
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO),
            allocated_storage=20,
            max_allocated_storage=100,
            multi_az=False,
            security_groups=[db_sg],
            publicly_accessible=False,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            delete_automated_backups=True,
        )

        # ---------------------------
        # IAM: Execution roles explícitos para FE y BE
        # ---------------------------
        fe_exec_role = iam.Role(
            self,
            "FeTaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Execution role for FE tasks (pull from ECR, write logs)",
        )
        fe_exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
        )

        be_exec_role = iam.Role(
            self,
            "BeTaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Execution role for BE tasks (pull from ECR, write logs)",
        )
        be_exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
        )

        # ---------------------------
        # Task definitions
        # ---------------------------
        # FRONTEND
        fe_task = ecs.FargateTaskDefinition(
            self,
            "FeTask",
            cpu=512,
            memory_limit_mib=1024,
            execution_role=fe_exec_role,  # <- rol explícito
        )
        fe_container = fe_task.add_container(
            "FrontendContainer",
            image=ecs.ContainerImage.from_ecr_repository(frontend_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Frontend"),
            environment={
                # FE llama al BE por su ALB público
                "API_BASE_URL": f"http://{be_alb.load_balancer_dns_name}"
            },
        )
        fe_container.add_port_mappings(ecs.PortMapping(container_port=3000))

        # BACKEND
        be_task = ecs.FargateTaskDefinition(
            self,
            "BeTask",
            cpu=512,
            memory_limit_mib=1024,
            execution_role=be_exec_role,  # <- rol explícito
        )
        be_container = be_task.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_ecr_repository(backend_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Backend"),
            environment={
                "DB_HOST": database.instance_endpoint.hostname,
                "DB_PORT": str(database.instance_endpoint.port),
                "DB_NAME": "ecommerce",
            },
            secrets={
                "DB_USERNAME": ecs.Secret.from_secrets_manager(database_secret, field="username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(database_secret, field="password"),
            },
        )
        be_container.add_port_mappings(ecs.PortMapping(container_port=4000))

        # ---------------------------
        # Services (con CodeDeploy)
        # ---------------------------
        fe_service = ecs.FargateService(
            self,
            "FrontendService",
            cluster=cluster,
            task_definition=fe_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[fe_svc_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
        )

        be_service = ecs.FargateService(
            self,
            "BackendService",
            cluster=cluster,
            task_definition=be_task,
            desired_count=1,
            assign_public_ip=False,
            security_groups=[be_svc_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
        )

        # ---------------------------
        # Target Groups (IDs únicos para evitar reuso)
        # ---------------------------
        # FE TGs (puerto target=3000)
        fe_tg_prod = elbv2.ApplicationTargetGroup(
            self,
            "FeTgProdV3",
            vpc=vpc,
            port=3000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200-399", interval=Duration.seconds(30)),
        )
        fe_tg_test = elbv2.ApplicationTargetGroup(
            self,
            "FeTgTestV3",
            vpc=vpc,
            port=3000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200-399", interval=Duration.seconds(30)),
        )
        fe_service.attach_to_application_target_group(fe_tg_prod)

        # BE TGs (puerto target=4000). Health en /health (200)
        be_tg_prod = elbv2.ApplicationTargetGroup(
            self,
            "BeTgProdV3",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200-399", interval=Duration.seconds(30)),
        )
        be_tg_test = elbv2.ApplicationTargetGroup(
            self,
            "BeTgTestV3",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200-399", interval=Duration.seconds(30)),
        )
        be_service.attach_to_application_target_group(be_tg_prod)

        # ---------------------------
        # Listener rules
        # ---------------------------
        # FE listeners -> FE TGs
        fe_listener_prod.add_target_groups("FeProdRule", target_groups=[fe_tg_prod])
        fe_listener_test.add_target_groups("FeTestRule", target_groups=[fe_tg_test])

        # BE listeners -> BE TGs
        be_listener_prod.add_target_groups("BeProdRule", target_groups=[be_tg_prod])
        be_listener_test.add_target_groups("BeTestRule", target_groups=[be_tg_test])

        # ---------------------------
        # CodeDeploy ECS Blue/Green (independientes)
        # ---------------------------
        fe_app = codedeploy.EcsApplication(self, "FrontendEcsApp")
        be_app = codedeploy.EcsApplication(self, "BackendEcsApp")

        fe_dg = codedeploy.EcsDeploymentGroup(
            self,
            "FrontendDeploymentGroup",
            application=fe_app,
            service=fe_service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                blue_target_group=fe_tg_prod,
                green_target_group=fe_tg_test,
                listener=fe_listener_prod,
                test_listener=fe_listener_test,
                termination_wait_time=Duration.minutes(5),
            ),
            deployment_config=codedeploy.EcsDeploymentConfig.ALL_AT_ONCE,
            auto_rollback=codedeploy.AutoRollbackConfig(failed_deployment=True, stopped_deployment=True),
        )

        be_dg = codedeploy.EcsDeploymentGroup(
            self,
            "BackendDeploymentGroup",
            application=be_app,
            service=be_service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                blue_target_group=be_tg_prod,
                green_target_group=be_tg_test,
                listener=be_listener_prod,
                test_listener=be_listener_test,
                termination_wait_time=Duration.minutes(5),
            ),
            deployment_config=codedeploy.EcsDeploymentConfig.ALL_AT_ONCE,
            auto_rollback=codedeploy.AutoRollbackConfig(failed_deployment=True, stopped_deployment=True),
        )

        # ---------------------------
        # Outputs
        # ---------------------------
        CfnOutput(self, "FrontendUrl", value=f"http://{fe_alb.load_balancer_dns_name}", description="FE public URL")
        CfnOutput(self, "BackendUrl", value=f"http://{be_alb.load_balancer_dns_name}", description="BE public URL (Postman/E2E)")
        CfnOutput(self, "FrontendEcrUri", value=frontend_repo.repository_uri)
        CfnOutput(self, "BackendEcrUri", value=backend_repo.repository_uri)
        CfnOutput(self, "DatabaseSecretArn", value=database_secret.secret_arn)
        CfnOutput(self, "DatabaseEndpoint", value=database.instance_endpoint.socket_address)

        # Nombres físicos (para pipelines)
        CfnOutput(self, "FrontendCodeDeployApp", value=fe_app.application_name)
        CfnOutput(self, "FrontendDeploymentGroupName", value=fe_dg.deployment_group_name)
        CfnOutput(self, "BackendCodeDeployApp", value=be_app.application_name)
        CfnOutput(self, "BackendDeploymentGroupName", value=be_dg.deployment_group_name)
