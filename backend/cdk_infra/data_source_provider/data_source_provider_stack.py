#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The database provider service: engine providers and the connection registry.

Deploys, against databases that already exist (this stack creates none):
  - one provider function per engine, which opens a session as the caller's own
    login and runs one operation in it
  - the connection registry: the connections table (each user's logins), the
    Connections page's API function, and the registry function other services
    call instead of reading the table
  - one scanner function per engine, logged in as a database's scanner account:
    it answers the schema service's scan requests on the scan bus, writing the
    DDL to this stack's scan-results bucket, and is registered with the schema
    service on that bus when this stack deploys (shared/scan_events.py)

Only this stack's functions have IAM on the connections table and on scanner
logins (/<stack>/scanners/).
"""

from aws_cdk import (
    Aspects,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_destinations as destinations,
    aws_s3 as s3,
    aws_ssm as ssm,
    custom_resources as cr,
)
from cdk_nag import RegexAppliesTo
from constructs import Construct

from cdk_infra.data_source_provider.secret_reader import add_secret_reader
from cdk_infra.shared import cfn_nag, checkov, database_tls, nag, paths
from cdk_infra.shared.bundling import (
    SOURCE_EXCLUDES,
    bundling_options,
    connections_api_steps,
    deleted_users_steps,
    disabled_version_steps,
    provider_steps,
    registration_steps,
    registry_steps,
    scanner_steps,
)
from cdk_infra.shared.data_source_engines import ENGINES
from cdk_infra.shared.imported_vpc import import_vpc
from cdk_infra.shared.origin_allowlist import origin_allowlist_read
from cdk_infra.shared.scan_bus import (
    DATABASE_PROVIDER_SOURCE,
    REGISTRATION_DISABLED,
    SCAN_REQUESTED,
    SCAN_TIMEOUT,
    SCHEMA_SERVICE_SOURCE,
)
from cdk_infra.shared.stage import ASYNC_DESTINATION, SYNCHRONOUS, Stage

ACCESS_LOG_BUCKET = ("The access-log bucket itself: logging it would log into itself or into another bucket "
                     "that would need the same, without end.")

class DataSourceProviderStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        stage: Stage,
        vpc_id: str,
        app_sg_id: str,
        private_subnet_ids: str,
        availability_zones: list[str],
        # The schema service's scan bus, and its functions this stack's scanners serve.
        scan_bus: events.IEventBus,
        schema_admin_function: lambda_.IFunction,
        schema_ingestion_function: lambda_.IFunction,
        # The schema service's function, which the providers read each connection's
        # registered database from.
        schema_function: lambda_.IFunction,
        # The user pool whose users own the connections: a user it no longer has loses theirs.
        user_pool_id: str,
        dynamodb_endpoint_url: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = import_vpc(self, vpc_id=vpc_id, availability_zones=availability_zones,
                         private_subnet_ids=private_subnet_ids)
        app_sg = ec2.SecurityGroup.from_security_group_id(self, "AppSG", app_sg_id)

        lambda_vpc_config = {
            "vpc": vpc,
            "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            "security_groups": [app_sg],
        }

        # DynamoDB Interface endpoints don't support private DNS, so the SDK
        # needs an explicit endpoint URL. Only set when provided (external VPCs
        # without a NAT gateway or DynamoDB Gateway endpoint).
        ddb_env = {"AWS_ENDPOINT_URL_DYNAMODB": f"https://{dynamodb_endpoint_url}"} if dynamodb_endpoint_url else {}

        # ── Connections table (the connection registry) ─────────────────
        self.connections_table = dynamodb.Table(self, "ConnectionsTable",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="connectionId", type=dynamodb.AttributeType.STRING),
            **stage.table_settings(self),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        )
        # Every user's connection to one database version, for deleting their logins when an
        # administrator disables it (connection_registry.TARGET_INDEX).
        self.connections_table.add_global_secondary_index(
            index_name="targetId-index",
            partition_key=dynamodb.Attribute(name="targetId", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.INCLUDE, non_key_attributes=["secretName"],
        )
        registry_env = {"CONNECTIONS_TABLE_NAME": self.connections_table.table_name}

        # ── Login attempts table (refused typed logins per user, per window) ──
        login_attempts_table = dynamodb.Table(self, "LoginAttemptsTable",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="window", type=dynamodb.AttributeType.NUMBER),
            time_to_live_attribute="expiresAt",
            **stage.table_settings(self),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        )
        # Where each connection's database is registered: the schema service's function.
        schema_env = {"SCHEMA_PROVIDER_FUNCTION_NAME": schema_function.function_name}

        # ── One provider function per engine ───────────────────────────
        self.provider_functions = {
            engine: stage.python_function(self, f"{build.provider_id}ProviderFunction",
                failures=SYNCHRONOUS.format("the MCP dispatcher and the connections registry"),
                handler="adam.data_source_provider.provider_handler.handler",
                code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                    bundling=bundling_options(provider_steps(engine), pip=build.driver,
                                           pip_alone=build.driver_alone)),
                timeout=Duration.minutes(5),
                memory_size=512,
                **lambda_vpc_config,
                environment={
                    "STACK_NAME": parent_stack_name,
                    # Where each connection's login comes from; see credential_provider.py.
                    "CREDENTIAL_PROVIDER": "secrets_manager_user_scoped",
                    **schema_env,
                    **(database_tls.ENVIRONMENT if build.rds_tls else {}),
                    **registry_env,
                    **ddb_env,
                },
            )
            for engine, build in ENGINES.items()
        }
        providers = list(self.provider_functions.values())

        # No direct secret read: every provider assumes one role limited to its caller's secrets.
        add_secret_reader(self, parent_stack_name, region=self.region, account=self.account,
                          functions=providers)

        # Each reads the one row it is asked to use, by key, and that row's database as
        # registered now, from the schema service.
        for fn in providers:
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["dynamodb:GetItem"], resources=[self.connections_table.table_arn]))
            schema_function.grant_invoke(fn)
            nag.function_versions(fn)

        # Other services and the providers find each other by tag and SSM name.
        def ssm_read():
            return iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{parent_stack_name}/*"],
            )

        def invoke_providers():
            return iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:*"],
                conditions={"StringEquals": {"aws:ResourceTag/mcp-service-provider": parent_stack_name}},
            )

        # ── Connections API Lambda (the Connections page) ───────────────
        # In the VPC: it checks a host against the VPC's own DNS before a login is
        # sent there, and the providers it asks to probe are in the VPC.
        self.connections_api_function = stage.python_function(self, "ConnectionsApiFunction",
            failures=SYNCHRONOUS.format("the HTTP API"),
            handler="adam.data_source_provider.connections_api_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(connections_api_steps())),
            timeout=Duration.minutes(2),
            memory_size=256,
            **lambda_vpc_config,
            environment={"STACK_NAME": parent_stack_name, **registry_env, **schema_env, **ddb_env,
                         "LOGIN_ATTEMPTS_TABLE_NAME": login_attempts_table.table_name},
        )
        connections_api = self.connections_api_function
        connections_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"], resources=[login_attempts_table.table_arn]))
        schema_function.grant_invoke(connections_api)
        connections_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
                     "dynamodb:Query", "dynamodb:UpdateItem"],
            resources=[self.connections_table.table_arn],
        ))
        stage.data_key(self).grant_encrypt_decrypt(connections_api)
        connections_api.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:UpdateSecret"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:/{parent_stack_name}/connections/*"],
        ))
        # Probes a login through the engine's provider; reads the registered databases from the schema service.
        connections_api.add_to_role_policy(invoke_providers())
        connections_api.add_to_role_policy(ssm_read())
        nag.function_versions(connections_api)
        nag.tagged_providers(connections_api, parent_stack_name)
        nag.own_parameters(connections_api, parent_stack_name)
        nag.acknowledge(connections_api, "IAM5", "Creates, updates and deletes users' connection secrets, only "
                        f"under /{parent_stack_name}/connections/; each request is for the caller's verified sub.",
                        findings=[nag.arn("secretsmanager", f"secret:/{parent_stack_name}/connections/*")])
        connections_api.add_to_role_policy(origin_allowlist_read(self, parent_stack_name))

        # ── Connection registry Lambda (other services' way in) ─────────
        # Outside the VPC: it reads its own table and asks the schema service for
        # names and limits, and connects to no database.
        self.registry_function = stage.python_function(self, "ConnectionRegistryFunction",
            failures=SYNCHRONOUS.format("the MCP dispatcher"),
            outside_vpc="Calls only AWS APIs (DynamoDB, Lambda, SSM) and holds no database login.",
            handler="adam.data_source_provider.registry_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(registry_steps())),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"STACK_NAME": parent_stack_name, **registry_env},
        )
        self.registry_function.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Query"], resources=[self.connections_table.table_arn]))
        self.registry_function.add_to_role_policy(invoke_providers())
        self.registry_function.add_to_role_policy(ssm_read())
        nag.tagged_providers(self.registry_function, parent_stack_name)
        nag.own_parameters(self.registry_function, parent_stack_name)

        # The dispatcher may invoke only functions carrying this tag.
        for fn in (*providers, self.registry_function):
            Tags.of(fn).add("mcp-service-provider", parent_stack_name)

        # ── Schema scanners ────────────────────────────────────────────
        self._add_scanners(parent_stack_name, stage, lambda_vpc_config, scan_bus,
                           schema_admin_function, schema_ingestion_function)

        # ── Disabled versions: their users' saved logins are deleted ────
        self._add_disabled_version_cleanup(parent_stack_name, stage, scan_bus)

        # ── Deleted users: their saved logins are deleted, daily ────────
        self._add_deleted_users_cleanup(parent_stack_name, stage, user_pool_id)

        # ── SSM + Outputs ──────────────────────────────────────────────
        for engine, build in ENGINES.items():
            fn = self.provider_functions[engine]
            ssm.StringParameter(self, f"{build.provider_id}FnParam",
                parameter_name=f"/{parent_stack_name}/{build.ssm_key}",
                string_value=fn.function_name,
            ).apply_removal_policy(RemovalPolicy.DESTROY)
            CfnOutput(self, f"{build.provider_id}FunctionName", value=fn.function_name)

        ssm.StringParameter(self, "RegistryFnParam",
            parameter_name=f"/{parent_stack_name}/connection_registry_function_name",
            string_value=self.registry_function.function_name,
        ).apply_removal_policy(RemovalPolicy.DESTROY)

    def _add_deleted_users_cleanup(self, parent_stack_name: str, stage: Stage, user_pool_id: str) -> None:
        """A daily run that deletes the connections and secrets of users the pool no longer has."""
        cleanup = stage.python_function(self, "DeletedUsersFunction",
            failures=ASYNC_DESTINATION,
            outside_vpc="Calls only AWS APIs (Cognito, DynamoDB, Secrets Manager) and holds no database login.",
            handler="adam.data_source_provider.deleted_users_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(deleted_users_steps())),
            timeout=Duration.minutes(15),
            memory_size=256,
            environment={"CONNECTIONS_TABLE_NAME": self.connections_table.table_name, "USER_POOL_ID": user_pool_id},
        )
        cleanup.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Scan", "dynamodb:DeleteItem"], resources=[self.connections_table.table_arn]))
        cleanup.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:DeleteSecret"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:/{parent_stack_name}/connections/*"]))
        cleanup.add_to_role_policy(iam.PolicyStatement(
            actions=["cognito-idp:ListUsers"],
            resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/{user_pool_id}"]))
        stage.data_key(self).grant_encrypt_decrypt(cleanup)
        nag.function_versions(cleanup)
        nag.acknowledge(cleanup, "IAM5", "Deletes saved logins only under "
                        f"/{parent_stack_name}/connections/, for users the user pool no longer has.",
                        findings=[nag.arn("secretsmanager", f"secret:/{parent_stack_name}/connections/*")])
        dlq = stage.failure_queue(self, "DeletedUsersDeadLetterQueue", from_event_rules=True)
        cleanup.configure_async_invoke(retry_attempts=0, on_failure=destinations.SqsDestination(dlq))
        events.Rule(self, "DeletedUsersSchedule", schedule=events.Schedule.rate(Duration.days(1)),
                    targets=[targets.LambdaFunction(cleanup, dead_letter_queue=dlq, retry_attempts=0)])

    def _add_disabled_version_cleanup(self, parent_stack_name: str, stage: Stage,
                                      scan_bus: events.IEventBus) -> None:
        """The function the schema service's RegistrationDisabled reaches: it deletes the secrets of
        that version's connections and keeps the rows as markers."""
        cleanup = stage.python_function(self, "DisabledVersionFunction",
            failures=ASYNC_DESTINATION,
            outside_vpc="Calls only AWS APIs (DynamoDB, Secrets Manager) and holds no database login.",
            handler="adam.data_source_provider.disabled_version_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(disabled_version_steps())),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={"STACK_NAME": parent_stack_name, "CONNECTIONS_TABLE_NAME": self.connections_table.table_name},
        )
        cleanup.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Query"], resources=[f"{self.connections_table.table_arn}/index/targetId-index"]))
        # Only a marker's own two attributes: it drops the secret's name and records when.
        cleanup.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"], resources=[self.connections_table.table_arn],
            conditions={"ForAllValues:StringEquals": {
                "dynamodb:Attributes": ["userId", "connectionId", "secretName", "versionDisabledAt"]}}))
        cleanup.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:DeleteSecret"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:/{parent_stack_name}/connections/*"]))
        stage.data_key(self).grant_encrypt_decrypt(cleanup)
        nag.function_versions(cleanup)
        nag.acknowledge(cleanup, "IAM5", "Deletes users' saved logins only under "
                        f"/{parent_stack_name}/connections/, for the connections to a version an administrator "
                        "disabled.", findings=[nag.arn("secretsmanager", f"secret:/{parent_stack_name}/connections/*")])
        dlq = stage.failure_queue(self, "DisabledVersionDeadLetterQueue", from_event_rules=True)
        cleanup.configure_async_invoke(retry_attempts=2, on_failure=destinations.SqsDestination(dlq))
        events.Rule(self, "RegistrationDisabledRule",
            event_bus=scan_bus,
            event_pattern=events.EventPattern(
                source=[f"{parent_stack_name}.{SCHEMA_SERVICE_SOURCE}"], detail_type=[REGISTRATION_DISABLED]),
            targets=[targets.LambdaFunction(cleanup, dead_letter_queue=dlq, retry_attempts=2)],
        )

    def _add_scanners(self, parent_stack_name: str, stage: Stage, lambda_vpc_config: dict,
                      scan_bus: events.IEventBus,
                      schema_admin_function: lambda_.IFunction, schema_ingestion_function: lambda_.IFunction):
        """One scanner function per engine, its rule on the scan bus, and its registration."""
        # A scan's DDL, for the schema service's ingestion function to read once.
        # Who read or wrote which scan's output: S3's access log, kept as the stage keeps logs.
        # S3 delivers access logs only to a bucket with S3-managed encryption.
        scan_access_logs = s3.Bucket(self, "ScanResultsAccessLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(stage.log_retention_days))],
            removal_policy=stage.data_removal_policy,
            auto_delete_objects=not stage.production,
        )
        checkov.skip(scan_access_logs, "CKV_AWS_18", ACCESS_LOG_BUCKET)
        cfn_nag.skip(scan_access_logs, "W35", ACCESS_LOG_BUCKET)
        checkov.skip(scan_access_logs, "CKV_AWS_21", "Holds only S3's access-log objects, each written once and "
                     "never changed; versions would only take up space.")
        self.scan_results_bucket = s3.Bucket(self, "ScanResultsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=scan_access_logs,
        )
        results = self.scan_results_bucket
        checkov.skip(results, "CKV_AWS_21", "A pass-through location: each scan's output is read once by schema "
                     "ingestion, expires in seven days and is written again by the next scan; versions would "
                     "keep nothing anyone needs.")
        results.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ArnPrincipal(schema_ingestion_function.role.role_arn)],
            actions=["s3:GetObject"], resources=[results.arn_for_objects("scans/*")],
        ))
        own_source = f"{parent_stack_name}.{DATABASE_PROVIDER_SOURCE}"
        publish = iam.PolicyStatement(
            actions=["events:PutEvents"], resources=[scan_bus.event_bus_arn],
            conditions={"StringEquals": {"events:source": own_source}},
        )
        # Requests the schema service could not deliver, or a scanner could not answer.
        dlq = stage.failure_queue(self, "ScanRequestsDeadLetterQueue", from_event_rules=True)

        self.scanner_functions = {}
        for engine, build in ENGINES.items():
            name = build.scanner_id
            fn = stage.python_function(self, f"{name}ScannerFunction",
                failures=ASYNC_DESTINATION,
                handler="adam.data_source_provider.scanner_handler.handler",
                code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                    bundling=bundling_options(scanner_steps(engine), pip=build.driver,
                                           pip_alone=build.driver_alone)),
                # Describing every table of a large schema is many dictionary queries.
                timeout=SCAN_TIMEOUT,
                memory_size=512,
                **lambda_vpc_config,
                environment={
                    "STACK_NAME": parent_stack_name,
                    "ENGINE": engine,
                    "SCAN_BUS_NAME": scan_bus.event_bus_name,
                    "SCAN_RESULTS_BUCKET": results.bucket_name,
                    **(database_tls.ENVIRONMENT if build.rds_tls else {}),
                },
            )
            # Named in the error a scan gives when a customer-managed key refuses the read.
            fn.add_environment("SCANNER_ROLE_NAME", fn.role.role_name)
            # Scanner logins: this service's own path, and nothing else.
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:CreateSecret",
                         "secretsmanager:DeleteSecret"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:/{parent_stack_name}/scanners/*"],
            ))
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["s3:PutObject"], resources=[results.arn_for_objects("scans/*")]))
            fn.add_to_role_policy(publish)
            nag.acknowledge(fn, "IAM5", "Its scanner logins, only under this service's "
                            f"/{parent_stack_name}/scanners/ path.",
                            findings=[nag.arn("secretsmanager", f"secret:/{parent_stack_name}/scanners/*")])
            nag.acknowledge(fn, "IAM5", "Writes each scan's output, only under the results bucket's scans/ prefix; "
                            "each scan names its own object.",
                            findings=[RegexAppliesTo(regex=r"/^Resource::<ScanResultsBucket[A-Za-z0-9]+\.Arn>\/scans\/\*$/")])
            # The schema service's admin routes check logins and store them through it.
            fn.add_permission("SchemaAdminInvoke",
                principal=iam.ArnPrincipal(schema_admin_function.role.role_arn),
                action="lambda:InvokeFunction")
            # A failed scan is not run again: retried logins with a wrong password can
            # lock the scanner account, and the next request comes anyway.
            fn.configure_async_invoke(retry_attempts=0, on_failure=destinations.SqsDestination(dlq))
            events.Rule(self, f"{name}ScanRequestsRule",
                event_bus=scan_bus,
                event_pattern=events.EventPattern(
                    source=[f"{parent_stack_name}.{SCHEMA_SERVICE_SOURCE}"], detail_type=[SCAN_REQUESTED],
                    detail={"engine": [engine]}),
                targets=[targets.LambdaFunction(fn, dead_letter_queue=dlq, retry_attempts=2)],
            )
            self.scanner_functions[engine] = fn

        # Registers each scanner with the schema service on deploy, and removes it on delete.
        registration = stage.python_function(self, "ScannerRegistrationFunction",
            failures=SYNCHRONOUS.format("CloudFormation, through its custom resource provider"),
            outside_vpc=("Publishes the scanners' registrations to EventBridge during a deploy;"
                         " reaches nothing in the VPC."),
            handler="adam.data_source_provider.registration_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(registration_steps())),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={"STACK_NAME": parent_stack_name, "SCAN_BUS_NAME": scan_bus.event_bus_name},
        )
        registration.add_to_role_policy(publish)
        # CDK's handlers for the provider framework and the bucket's emptying state no reasons of their own.
        Aspects.of(self).add(checkov.DeployHandlerReasons())
        registrar = cr.Provider(self, "ScannerRegistrationProvider", on_event_handler=registration,
                                log_group=stage.log_group(self, "ScannerRegistrationProviderLogs"),
                                provider_function_env_encryption=stage.data_key(self))
        nag.function_versions(registrar)
        for engine, build in ENGINES.items():
            CustomResource(self, f"{build.scanner_id}ScannerRegistration",
                service_token=registrar.service_token,
                properties={"Engine": engine,
                            "ScannerFunctionArn": self.scanner_functions[engine].function_arn,
                            "ResultsBucket": results.bucket_name},
            )
