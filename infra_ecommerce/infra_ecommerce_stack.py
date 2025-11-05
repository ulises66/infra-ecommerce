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
)
from constructs import Construct


class InfraEcommerceStack(Stack):
    """
    Un solo entorno (prod) con CodeDeploy ECS blue/green:
    - VPC públicas + privadas aisladas
    - RDS MySQL + Secrets Manager
    - ECR (frontend y backend)
    - ECS Fargate (servicios con DeploymentController=CODE_DEPLOY)
    - ALB público :80 (prod) + listener de prueba :9000 (cerrado)
    - TGs prod/test para FE y BE
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

        cluster = ecs.Cluster(self, "EcommerceCluster", vpc=vpc)

        # ---------------------------
        # ECR repos (uno por servicio)
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
        lb_sg = ec2.SecurityGroup(
            self,
            "LoadBalancerSecurityGroup",
            vpc=vpc,
            description="Allow HTTP access to the load balancer",
            allow_all_outbound=True,
        )
        # Solo puerto 80 público; 9000 será listener de prueba NO expuesto
        lb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "Allow HTTP 80"
        )

        fe_sg = ec2.SecurityGroup(
            self,
            "FrontendServiceSecurityGroup",
            vpc=vpc,
            description="Allow ALB to reach the frontend",
            allow_all_outbound=True,
        )
        fe_sg.add_ingress_rule(
            lb_sg, ec2.Port.tcp(3000), "ALB to Frontend containers"
        )

        be_sg = ec2.SecurityGroup(
            self,
            "BackendServiceSecurityGroup",
            vpc=vpc,
            description="Allow ALB to reach the backend",
            allow_all_outbound=True,
        )
        be_sg.add_ingress_rule(
            lb_sg, ec2.Port.tcp(4000), "ALB to Backend containers"
        )

        db_sg = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=vpc,
            description="Restrict DB to backend only",
            allow_all_outbound=True,
        )
        db_sg.add_ingress_rule(
            be_sg, ec2.Port.tcp(3306), "Backend to MySQL 3306"
        )

        # ---------------------------
        # ALB + Listeners (prod/test)
        # ---------------------------
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "PublicLoadBalancer",
            vpc=vpc,
            internet_facing=True,
            security_group=lb_sg,
        )

        prod_listener = alb.add_listener(
            "HttpProdListener",
            port=80,
            open=True,
            protocol=elbv2.ApplicationProtocol.HTTP,
        )
        # Listener de test CERRADO (no expuesto públicamente)
        test_listener = alb.add_listener(
            "HttpTestListener",
            port=9000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,
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
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            credentials=rds.Credentials.from_secret(
                database_secret, username="appuser"
            ),
            database_name="ecommerce",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO
            ),
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
        # Task definitions (Fargate)
        # ---------------------------
        frontend_task = ecs.FargateTaskDefinition(
            self, "FrontendTask", cpu=512, memory_limit_mib=1024
        )
        frontend_container = frontend_task.add_container(
            "FrontendContainer",
            image=ecs.ContainerImage.from_ecr_repository(frontend_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Frontend"),
            environment={
                # Si en Next usas fetch relativo '/api', puedes omitir esta env
                "API_BASE_URL": f"http://{alb.load_balancer_dns_name}/api"
            },
        )
        frontend_container.add_port_mappings(
            ecs.PortMapping(container_port=3000)
        )

        backend_task = ecs.FargateTaskDefinition(
            self, "BackendTask", cpu=512, memory_limit_mib=1024
        )
        backend_container = backend_task.add_container(
            "BackendContainer",
            image=ecs.ContainerImage.from_ecr_repository(backend_repo, tag="latest"),
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

        # ---------------------------
        # ECS Services (CodeDeploy controller)
        # ---------------------------
        frontend_service = ecs.FargateService(
            self,
            "FrontendService",
            cluster=cluster,
            task_definition=frontend_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[fe_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            ),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
        )

        backend_service = ecs.FargateService(
            self,
            "BackendService",
            cluster=cluster,
            task_definition=backend_task,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[be_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            ),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
        )

        # ---------------------------
        # Target Groups (prod/test)
        # ---------------------------
        # FRONTEND
        fe_tg_prod = elbv2.ApplicationTargetGroup(
            self,
            "FeTgProd",
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
        fe_tg_test = elbv2.ApplicationTargetGroup(
            self,
            "FeTgTest",
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
        frontend_service.attach_to_application_target_group(fe_tg_prod)

        # BACKEND
        # Aceptamos 2xx–4xx por si la raíz devuelve 401/403;
        # ideal: crear /health que devuelva 200 y cambiar a 200-399.
        be_tg_prod = elbv2.ApplicationTargetGroup(
            self,
            "BeTgProd",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200-499",
                interval=Duration.seconds(30),
            ),
        )
        be_tg_test = elbv2.ApplicationTargetGroup(
            self,
            "BeTgTest",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200-499",
                interval=Duration.seconds(30),
            ),
        )
        backend_service.attach_to_application_target_group(be_tg_prod)

        # ---------------------------
        # Reglas de enrutamiento
        # ---------------------------
        # PROD listener: raíz -> FE, /api -> BE
        prod_listener.add_target_groups(
            "FrontendProdRule",
            target_groups=[fe_tg_prod],
        )
        prod_listener.add_target_groups(
            "BackendProdRule",
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/api*", "/api/*"])],
            target_groups=[be_tg_prod],
        )

        # TEST listener: mismas rutas pero TGs de test (no público)
        test_listener.add_target_groups(
            "FrontendTestRule",
            target_groups=[fe_tg_test],
        )
        test_listener.add_target_groups(
            "BackendTestRule",
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/api*", "/api/*"])],
            target_groups=[be_tg_test],
        )

        # ---------------------------
        # CodeDeploy ECS Blue/Green
        # ---------------------------
        fe_app = codedeploy.EcsApplication(self, "FrontendEcsApp")
        be_app = codedeploy.EcsApplication(self, "BackendEcsApp")

        fe_dg = codedeploy.EcsDeploymentGroup(
            self,
            "FrontendDeploymentGroup",
            application=fe_app,
            service=frontend_service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                blue_target_group=fe_tg_prod,   # TG actual (blue)
                green_target_group=fe_tg_test,  # TG nuevo (green)
                listener=prod_listener,         # listener prod :80
                test_listener=test_listener,    # listener test :9000 (cerrado)
                termination_wait_time=Duration.minutes(5),
            ),
            deployment_config=codedeploy.EcsDeploymentConfig.ALL_AT_ONCE,
            auto_rollback=codedeploy.AutoRollbackConfig(
                failed_deployment=True, stopped_deployment=True
            ),
        )

        be_dg = codedeploy.EcsDeploymentGroup(
            self,
            "BackendDeploymentGroup",
            application=be_app,
            service=backend_service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                blue_target_group=be_tg_prod,
                green_target_group=be_tg_test,
                listener=prod_listener,
                test_listener=test_listener,
                termination_wait_time=Duration.minutes(5),
            ),
            deployment_config=codedeploy.EcsDeploymentConfig.ALL_AT_ONCE,
            auto_rollback=codedeploy.AutoRollbackConfig(
                failed_deployment=True, stopped_deployment=True
            ),
        )

        # ---------------------------
        # Outputs
        # ---------------------------
        CfnOutput(
            self,
            "LoadBalancerUrl",
            value=f"http://{alb.load_balancer_dns_name}",
            description="Public endpoint for the ecommerce frontend",
        )
        CfnOutput(
            self,
            "FrontendEcrUri",
            value=frontend_repo.repository_uri,
            description="ECR URI (frontend)",
        )
        CfnOutput(
            self,
            "BackendEcrUri",
            value=backend_repo.repository_uri,
            description="ECR URI (backend)",
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
        CfnOutput(
            self,
            "FrontendCodeDeployApp",
            value=fe_app.application_name,
            description="CodeDeploy ECS application (frontend)",
        )
        CfnOutput(
            self,
            "BackendCodeDeployApp",
            value=be_app.application_name,
            description="CodeDeploy ECS application (backend)",
        )
        CfnOutput(
            self,
            "FrontendDeploymentGroupName",
            value=fe_dg.deployment_group_name,
            description="CodeDeploy deployment group (frontend)",
        )
        CfnOutput(
            self,
            "BackendDeploymentGroupName",
            value=be_dg.deployment_group_name,
            description="CodeDeploy deployment group (backend)",
        )
