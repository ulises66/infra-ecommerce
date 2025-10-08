import aws_cdk as core
import aws_cdk.assertions as assertions

from infra_ecommerce.infra_ecommerce_stack import InfraEcommerceStack


def _synth_template() -> assertions.Template:
    app = core.App()
    stack = InfraEcommerceStack(app, "infra-ecommerce")
    return assertions.Template.from_stack(stack)


def test_database_secret_is_configured_with_expected_name():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::SecretsManager::Secret",
        {
            "Name": "ecommerce/mysql",
            "GenerateSecretString": assertions.Match.object_like(
                {
                    "GenerateStringKey": "password",
                    "SecretStringTemplate": assertions.Match.any_value(),
                }
            ),
        },
    )


def test_database_instance_uses_mysql_with_expected_configuration():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "DBName": "ecommerce",
            "Engine": "mysql",
            "EngineVersion": "8.0.43",
            "DBInstanceClass": "db.t3.micro",
            "AllocatedStorage": "20",
            "MaxAllocatedStorage": 100,
            "PubliclyAccessible": False,
            "DeletionProtection": False,
        },
    )


def test_two_fargate_services_are_created():
    template = _synth_template()

    template.resource_count_is("AWS::ECS::Service", 2)


def test_frontend_task_definition_exposes_http_port_and_api_env():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        assertions.Match.object_like(
            {
                "Cpu": "512",
                "Memory": "1024",
                "ContainerDefinitions": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Name": "FrontendContainer",
                                "PortMappings": [
                                    assertions.Match.object_like(
                                        {
                                            "ContainerPort": 3000,
                                            "Protocol": "tcp",
                                        }
                                    )
                                ],
                                "Environment": assertions.Match.array_with(
                                    [
                                        assertions.Match.object_like(
                                            {
                                                "Name": "API_BASE_URL",
                                            }
                                        )
                                    ]
                                ),
                            }
                        )
                    ]
                ),
            }
        ),
    )


def test_backend_task_definition_exposes_http_port_and_secrets():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        assertions.Match.object_like(
            {
                "Cpu": "512",
                "Memory": "1024",
                "ContainerDefinitions": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Name": "BackendContainer",
                                "PortMappings": [
                                    assertions.Match.object_like(
                                        {
                                            "ContainerPort": 4000,
                                            "Protocol": "tcp",
                                        }
                                    )
                                ],
                                "Environment": assertions.Match.array_with(
                                    [
                                        assertions.Match.object_like(
                                            {"Name": "DB_NAME", "Value": "ecommerce"}
                                        )
                                    ]
                                ),
                                "Secrets": assertions.Match.array_with(
                                    [
                                        assertions.Match.object_like(
                                            {"Name": "DB_USERNAME"}
                                        ),
                                        assertions.Match.object_like(
                                            {"Name": "DB_PASSWORD"}
                                        ),
                                    ]
                                ),
                            }
                        )
                    ]
                ),
            }
        ),
    )


def test_internet_facing_load_balancer_created():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {
            "Scheme": "internet-facing",
            "Type": "application",
        },
    )


def test_stack_outputs_expose_useful_information():
    template = _synth_template()

    template.has_output(
        "LoadBalancerUrl",
        assertions.Match.object_like(
            {
                "Value": assertions.Match.any_value(),
                "Description": "Public endpoint for the ecommerce frontend",
            }
        ),
    )

    template.has_output(
        "FrontendDockerContext",
        assertions.Match.object_like(
            {
                "Value": "container_images/frontend",
            }
        ),
    )

    template.has_output(
        "BackendDockerContext",
        assertions.Match.object_like(
            {
                "Value": "container_images/backend",
            }
        ),
    )

    template.has_output(
        "DatabaseSecretArn",
        assertions.Match.object_like(
            {
                "Value": assertions.Match.any_value(),
            }
        ),
    )

    template.has_output(
        "DatabaseEndpoint",
        assertions.Match.object_like(
            {
                "Value": assertions.Match.any_value(),
            }
        ),
    )
