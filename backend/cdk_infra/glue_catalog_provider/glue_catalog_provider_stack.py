#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The schema service (Glue Data Catalog implementation).

Independent stack. Deploys:
  - The schema index: one Glue database this stack owns
  - The scan-target table: the databases an administrator registered, which
    only this stack's functions may use (schema_provider/scan_targets.py)
  - The schema provider function: search_schema over the index, applying the
    caller's visibility the dispatcher obtains from the caller's own databases,
    and the registered databases (list_databases, get_database) other services read
  - The scan bus: an EventBridge bus the database providers register on and
    answer scan requests on (shared/scan_events.py), and the provider table that
    records which engine has a provider
  - The ingestion Lambda: requests scans on the bus and turns the providers'
    results into index entries. The index's only writer. It connects to no
    database and reads no secret: each engine's provider scans.
  - The admin routes' function (/api/admin on the web console's HTTP API)
  - The schedule group and the role EventBridge Scheduler uses for scheduled
    scans. The admin routes put one schedule per target in the group.

No Lake Formation and no per-caller IAM roles: the databases decide what each
caller may see. The index database gives its new tables to IAM_ALLOWED_PRINCIPALS,
so IAM governs them. In an account whose Lake Formation default for new databases
is not IAM_ALLOWED_PRINCIPALS, an LF administrator grants it on the index database
once (aws lakeformation grant-permissions ... --permissions ALL).
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_glue as glue,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_destinations as destinations,
    aws_scheduler as scheduler,
    aws_ssm as ssm,
)
from cdk_nag import RegexAppliesTo
from constructs import Construct

from cdk_infra.shared import cfn_nag, nag, paths
from cdk_infra.shared.bundling import (
    BOTO3,
    SOURCE_EXCLUDES,
    admin_steps,
    bundling_options,
    ingestion_steps,
    search_steps,
)
from cdk_infra.shared.imported_vpc import import_vpc
from cdk_infra.shared.origin_allowlist import origin_allowlist_read
from cdk_infra.shared.scan_bus import (
    DATABASE_PROVIDER_SOURCE,
    PROVIDER_EVENTS,
    SCAN_TIMEOUT,
    SCHEMA_SERVICE_SOURCE,
)
from cdk_infra.shared.stage import ASYNC_DESTINATION, SYNCHRONOUS, Stage

SEARCH_ASSETS = ("glue:SearchAssets searches the whole catalog by text and is not yet in AWS's Service "
                 "Authorization Reference, so it names no resource; the search's own filter keeps results to "
                 "Glue tables.")
UPDATE_ASSET = ("glue:UpdateAsset is not yet in AWS's Service Authorization Reference, and granted on the index "
                "tables' own ARNs, which are what it is called with, IAM denies it (tested 2026-10-24).")
# Reviewed 2026-10-24: every statement is used, and none can be removed or merged without widening it.
LEAST_PRIVILEGE_SPLIT = ("Its size is the least-privilege split: each table and Glue scope is granted apart, one "
                         "table's UpdateItem only on the attributes it writes, and every statement is used.")
INDEX_TABLES = ("The schema index database's tables only: one per scanned table or view, named by what "
                "each scan finds.")


class GlueCatalogProviderStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        stage: Stage,
        # The admin function checks database hosts against the VPC's DNS, so it sits in the VPC.
        vpc_id: str,
        app_sg_id: str,
        private_subnet_ids: str,
        availability_zones: list[str],
        dynamodb_endpoint_url: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        index_database_name = f"{parent_stack_name.lower()}_schema_index"
        index_database = glue.CfnDatabase(self, "SchemaIndexDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=index_database_name,
                description=(
                    "Schema index for search_schema: DDL scanned from each "
                    "registered database. Written only by the schema ingestion "
                    "function."
                ),
                # IAM alone governs its tables, whatever the account's Lake Formation
                # default was when the database was created.
                create_table_default_permissions=[glue.CfnDatabase.PrincipalPrivilegesProperty(
                    principal=glue.CfnDatabase.DataLakePrincipalProperty(
                        data_lake_principal_identifier="IAM_ALLOWED_PRINCIPALS"),
                    permissions=["ALL"],
                )],
            ),
        )
        index_arns = [
            f"arn:aws:glue:{self.region}:{self.account}:catalog",
            f"arn:aws:glue:{self.region}:{self.account}:database/{index_database_name}",
            f"arn:aws:glue:{self.region}:{self.account}:table/{index_database_name}/*",
        ]
        index_tables = nag.arn("glue", f"table/{index_database_name}/*")
        index_env = {
            "SCHEMA_INDEX_DATABASE": index_database_name,
            "CATALOG_ID": self.account,
        }
        # Asset operations are not addressed by catalog/database/table ARNs, so
        # listing them alongside ARN-scoped Glue actions silently denies them.
        asset_read = iam.PolicyStatement(actions=["glue:SearchAssets"], resources=["*"])

        # ── Scan-target table (this service's own) ──────────────────────
        # Only this stack's functions have IAM on it: the admin routes write it, the
        # ingestion function reads it and records scan progress, and the schema
        # provider function serves the registered databases from it to other services.
        self.scan_targets_table = dynamodb.Table(self, "ScanTargetsTable",
            partition_key=dynamodb.Attribute(name="targetId", type=dynamodb.AttributeType.STRING),
            **stage.table_settings(self),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        )
        targets_arn = self.scan_targets_table.table_arn
        ddb_env = {"AWS_ENDPOINT_URL_DYNAMODB": f"https://{dynamodb_endpoint_url}"} if dynamodb_endpoint_url else {}

        # ── Search Lambda ──────────────────────────────────────────────
        # Not VPC-attached: it reads Glue over the public endpoint and holds no
        # database connection, and an external VPC may have neither NAT nor a
        # Glue endpoint.
        # Lambda Python 3.13 ships a boto3 that predates the Glue business-context
        # preview, so `search_assets` / `update_asset` do not exist there at all.
        self.function = stage.python_function(self, "GlueCatalogProviderFunction",
            failures=SYNCHRONOUS.format("the data source providers and the connections API"),
            outside_vpc="Calls only AWS APIs (Glue, DynamoDB, S3) and holds no database login.",
            handler="adam.schema_provider.glue_data_catalog_schema_provider.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(search_steps(), pip=(BOTO3,))),
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={"STACK_NAME": parent_stack_name, **index_env,
                         "SCAN_TARGETS_TABLE": self.scan_targets_table.table_name},
        )
        Tags.of(self.function).add("mcp-service-provider", parent_stack_name)
        self.function.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:GetTable"], resources=index_arns,
        ))
        self.function.add_to_role_policy(asset_read)
        nag.acknowledge(self.function, "IAM5", SEARCH_ASSETS, findings=["Resource::*"])
        cfn_nag.skip_role_policies(self.function.role, "W12", SEARCH_ASSETS)
        nag.acknowledge(self.function, "IAM5", INDEX_TABLES, findings=[index_tables])
        # Serves the registered databases (list_databases, get_database): read only.
        self.function.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Scan"], resources=[targets_arn]))

        # ── Scan bus and the provider table ────────────────────────────
        # Providers register on the bus and answer scan requests there; the provider
        # stacks grant their own functions PutEvents with their source only.
        self.scan_bus = events.EventBus(self, "ScanBus")
        self.scan_providers_table = dynamodb.Table(self, "ScanProvidersTable",
            partition_key=dynamodb.Attribute(name="engine", type=dynamodb.AttributeType.STRING),
            **stage.table_settings(self),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        )
        providers_arn = self.scan_providers_table.table_arn
        # Events the ingestion function could not take, kept for an operator.
        self.scan_events_dlq = stage.failure_queue(self, "ScanEventsDeadLetterQueue", from_event_rules=True)

        # ── Ingestion Lambda ───────────────────────────────────────────
        # Not VPC-attached: it reads a provider's scan result from S3 and writes Glue,
        # DynamoDB and the bus, and connects to no database.
        self.ingestion_function = stage.python_function(self, "SchemaIngestionFunction",
            failures=ASYNC_DESTINATION,
            outside_vpc="Calls only AWS APIs (Glue, DynamoDB, EventBridge, S3) and holds no database login.",
            handler="adam.schema_provider.schema_ingestion_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(ingestion_steps(), pip=(BOTO3,))),
            # Publishing a large schema is many Glue calls, each retried while throttled.
            timeout=Duration.minutes(15),
            memory_size=512,
            environment={
                "STACK_NAME": parent_stack_name,
                "SCAN_TARGETS_TABLE": self.scan_targets_table.table_name,
                "SCAN_PROVIDERS_TABLE": self.scan_providers_table.table_name,
                "SCAN_BUS_NAME": self.scan_bus.event_bus_name,
                **index_env,
            },
        )
        self.ingestion_function.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetTable", "glue:GetTables",
                     "glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable"],
            resources=index_arns,
        ))
        # Describes the index's own tables. Tried scoped to those tables' ARNs, which are the
        # identifiers it passes, on 2026-10-24: IAM denied it, so the action is not checked
        # against the table.
        self.ingestion_function.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:UpdateAsset"], resources=["*"],
        ))
        nag.acknowledge(self.ingestion_function, "IAM5", INDEX_TABLES, findings=[index_tables])
        nag.acknowledge(self.ingestion_function, "IAM5", UPDATE_ASSET, findings=["Resource::*"])
        cfn_nag.skip_role_policies(self.ingestion_function.role, "W12", UPDATE_ASSET)
        cfn_nag.skip_role_policies(self.ingestion_function.role, "W76", LEAST_PRIVILEGE_SPLIT)
        self.ingestion_function.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Scan"], resources=[targets_arn]))
        # Writes a scan's progress on its target, and no other attribute, for the admin page.
        self.ingestion_function.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[targets_arn],
            conditions={"ForAllValues:StringEquals": {
                "dynamodb:Attributes": ["targetId", "scanStatus", "scanMessage", "scanUpdatedAt", "scanRequestId"],
            }},
        ))
        # Records the providers' registrations.
        self.ingestion_function.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:DeleteItem"],
            resources=[providers_arn]))
        stage.data_key(self).grant_encrypt_decrypt(self.ingestion_function)
        # Requests scans, as the schema service and nothing else. Reading a provider's
        # result is granted by that provider's bucket policy.
        self.ingestion_function.add_to_role_policy(iam.PolicyStatement(
            actions=["events:PutEvents"], resources=[self.scan_bus.event_bus_arn],
            conditions={"StringEquals": {"events:source": f"{parent_stack_name}.{SCHEMA_SERVICE_SOURCE}"}},
        ))
        # A failed scan request is not run again (the next schedule comes anyway), and
        # what could not be taken is kept for an operator.
        self.ingestion_function.configure_async_invoke(
            retry_attempts=0, on_failure=destinations.SqsDestination(self.scan_events_dlq))
        events.Rule(self, "ProviderEventsRule",
            event_bus=self.scan_bus,
            event_pattern=events.EventPattern(
                source=[f"{parent_stack_name}.{DATABASE_PROVIDER_SOURCE}"], detail_type=PROVIDER_EVENTS),
            targets=[targets.LambdaFunction(self.ingestion_function,
                                            dead_letter_queue=self.scan_events_dlq, retry_attempts=2)],
        )

        # ── Scheduled scans ────────────────────────────────────────────
        # The admin routes create, change and delete schedules in this group at
        # run time; the role they pass may only invoke the ingestion function.
        # DESTROY: the default, RETAIN, leaves the group behind on teardown, and its
        # name then blocks the next deploy of the stage.
        self.scan_schedule_group = scheduler.ScheduleGroup(self, "ScanScheduleGroup",
                                                           removal_policy=RemovalPolicy.DESTROY)
        self.scan_scheduler_role = iam.Role(self, "ScanSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {"aws:SourceArn": f"arn:aws:scheduler:{self.region}:{self.account}:schedule/"
                                             f"{self.scan_schedule_group.schedule_group_name}/*"},
            }),
        )
        self.ingestion_function.grant_invoke(self.scan_scheduler_role)
        nag.function_versions(self.scan_scheduler_role)
        for fn in (self.function, self.ingestion_function):
            fn.node.add_dependency(index_database)

        # ── Admin API Lambda (the /api/admin routes) ────────────────────
        # In the VPC: it checks a database host against the VPC's own DNS before the
        # scanner is sent there. The web console's HTTP API routes /api/admin here.
        vpc = import_vpc(self, vpc_id=vpc_id, availability_zones=availability_zones,
                         private_subnet_ids=private_subnet_ids)
        app_sg = ec2.SecurityGroup.from_security_group_id(self, "AppSG", app_sg_id)
        self.admin_function = stage.python_function(self, "AdminApiFunction",
            failures=SYNCHRONOUS.format("the HTTP API"),
            handler="admin_handler.handler",
            code=lambda_.Code.from_asset(paths.SRC, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(admin_steps())),
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[app_sg],
            environment={
                "STACK_NAME": parent_stack_name,
                "SCAN_TARGETS_TABLE": self.scan_targets_table.table_name,
                "SCAN_PROVIDERS_TABLE": self.scan_providers_table.table_name,
                # Read only to show how many tables each registered database has in the index.
                "SCHEMA_INDEX_DATABASE": index_database_name,
                "SCAN_SCHEDULE_GROUP": self.scan_schedule_group.schedule_group_name,
                "SCAN_SCHEDULER_ROLE_ARN": self.scan_scheduler_role.role_arn,
                "SCHEMA_INGESTION_FUNCTION_ARN": self.ingestion_function.function_arn,
                # A scan still queued or scanning past this was cut off and will not answer.
                "SCAN_TIMEOUT_SECONDS": str(int(SCAN_TIMEOUT.to_seconds())),
                "DEPLOY_REGION": self.region,
                **ddb_env,
            },
        )
        admin = self.admin_function
        admin.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Scan",
                     "dynamodb:UpdateItem"],
            resources=[targets_arn],
        ))
        stage.data_key(self).grant_encrypt_decrypt(admin)
        # Starts scans and purges index entries. The providers' scanner functions grant
        # this role invoke on themselves; it holds no secret permission of its own.
        self.ingestion_function.grant_invoke(admin)
        admin.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Scan"], resources=[providers_arn]))
        # Scan schedules: only in this stack's group, running only as the role made for them.
        admin.add_to_role_policy(iam.PolicyStatement(
            actions=["scheduler:CreateSchedule", "scheduler:UpdateSchedule",
                     "scheduler:DeleteSchedule", "scheduler:GetSchedule"],
            resources=[f"arn:aws:scheduler:{self.region}:{self.account}:schedule/"
                       f"{self.scan_schedule_group.schedule_group_name}/*"],
        ))
        admin.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[self.scan_scheduler_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
        ))
        admin.add_to_role_policy(iam.PolicyStatement(actions=["glue:GetTables"], resources=index_arns))
        admin.add_to_role_policy(origin_allowlist_read(self, parent_stack_name))
        nag.function_versions(admin)
        cfn_nag.skip_role_policies(admin.role, "W76", LEAST_PRIVILEGE_SPLIT)
        nag.own_parameters(admin, parent_stack_name)
        nag.acknowledge(admin, "IAM5", INDEX_TABLES, findings=[index_tables])
        nag.acknowledge(admin, "IAM5", "Manages scan schedules only in this stack's schedule group; each "
                        "scan target names its own schedule.",
                        findings=[RegexAppliesTo(regex=r"/^Resource::arn:aws:scheduler:.*:schedule\/[A-Za-z0-9]+-ScanScheduleGroup-[A-Za-z0-9]+\/\*$/")])

        # ── SSM + Outputs ──────────────────────────────────────────────
        ssm.StringParameter(self, "FunctionNameParam",
            parameter_name=f"/{parent_stack_name}/schema_provider_function_name",
            string_value=self.function.function_name,
        ).apply_removal_policy(RemovalPolicy.DESTROY)

        CfnOutput(self, "FunctionName", value=self.function.function_name)
        CfnOutput(self, "IngestionFunctionName", value=self.ingestion_function.function_name)
        CfnOutput(self, "SchemaIndexDatabaseName", value=index_database_name)
        CfnOutput(self, "ScanBusName", value=self.scan_bus.event_bus_name)
