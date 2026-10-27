#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CDK stack for the Agent Provider microservice.

Independent stack. Deploys:
  - AgentCore Runtime (Strands agent container with CUSTOM_JWT auth), only with
    enable_chat: the agent behind the web console's chat page. Quick needs none.
  - AgentCore Gateway (MCP protocol, CUSTOM_JWT, request interceptor)
  - MCP dispatcher Lambda (thin tool router to provider Lambdas)
  - Gateway interceptor Lambda (verifies the caller's JWT, injects its sub as
    the reserved _verified_user_id tool argument — the body is the only channel
    an MCP target has)
  - Sessions stream processor (DDB Stream → AppSync Events)
  - Chat API Lambda (chat and conversation-history routes; starts each turn on the runtime)
  - AppSync Events API (real-time streaming to frontend)
  - Sessions DDB table
"""

import json
from pathlib import Path

import jsii
from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    IgnoreMode,
    Stack,
    aws_apigateway as apigw,
    aws_apigatewayv2 as apigwv2,
    aws_appsync as appsync,
    aws_bedrockagentcore as agentcore,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
)
from aws_cdk.aws_apigatewayv2_authorizers import HttpJwtAuthorizer
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from cdk_nag import RegexAppliesTo
from constructs import Construct

from cdk_infra.agent_provider import chat_models
from cdk_infra.shared import cfn_nag, nag, paths
from cdk_infra.shared.bundling import (
    AGENT_IMAGE_FILES,
    BOTO3,
    PYJWT,
    SOURCE_EXCLUDES,
    bundling_options,
    chat_api_steps,
    copy_files,
    dispatcher_steps,
    interceptor_steps,
    web_search_steps,
)
from cdk_infra.shared.imported_vpc import import_vpc, private_subnets
from cdk_infra.shared.origin_allowlist import origin_allowlist_read
from cdk_infra.shared.stage import STREAM_DLQ, SYNCHRONOUS, Stage

# Requests per second across the whole stage, and the burst allowed above it.
API_RATE_LIMIT = 20
API_BURST_LIMIT = 40


PREFLIGHT_UNAUTHENTICATED = ("A false positive: browsers send a CORS preflight without credentials, so OPTIONS must be"
                             " unauthenticated for the authorized routes it precedes to work.")


RUNTIME_TELEMETRY = ("X-Ray's trace and sampling actions take no resource, nor does cloudwatch:PutMetricData, "
                     "which its condition keeps to AgentCore's namespace.")
LIST_STREAMS = "CDK's DynamoDB event source grants dynamodb:ListStreams, which takes no resource, by default."
CROSS_REGION_INFERENCE = ("A cross-region inference profile routes to the foundation model in any Region, so "
                          "the model grant names every Region; the profile itself is this Region's.")


@jsii.implements(apigwv2.IAccessLogSettings)
class _AccessLog:
    """Where the HTTP API stage writes its access log, and in what format."""

    def __init__(self, destination: apigwv2.IAccessLogDestination, log_format: apigw.AccessLogFormat):
        self._destination, self._format = destination, log_format

    @property
    def destination(self) -> apigwv2.IAccessLogDestination:
        return self._destination

    @property
    def format(self) -> apigw.AccessLogFormat:
        return self._format


class AgentProviderStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        stage: Stage,
        vpc_id: str,
        app_sg_id: str,
        private_subnet_ids: str,
        availability_zones: list[str],
        # OIDC issuer of the identity provider. The gateway, runtime and HTTP API
        # discover keys from it; the interceptor verifies tokens against it.
        oidc_issuer_url: str,
        # Clients whose tokens the MCP gateway accepts (the frontend and Quick).
        allowed_client_ids: list[str],
        # The frontend's client: the audience for the runtime and HTTP API.
        frontend_client_id: str,
        # Implementation the interceptor builds; see agent_provider/identity_provider.py.
        identity_provider: str,
        # Only for AppSync Events, whose Cognito auth mode takes a pool id.
        user_pool_id: str,
        web_search_model_id: str | None,
        # Other services' functions the web console's HTTP API routes to: the
        # connection registry's connections API, and the schema service's admin routes.
        connections_api_function: lambda_.IFunction,
        admin_function: lambda_.IFunction,
        # Deploy the agent runtime that answers the web console's chat page.
        enable_chat: bool,
        dynamodb_endpoint_url: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = import_vpc(self, vpc_id=vpc_id, availability_zones=availability_zones,
                         private_subnet_ids=private_subnet_ids)
        app_sg = ec2.SecurityGroup.from_security_group_id(self, "AppSG", app_sg_id)

        # Every database connection comes from the user's DDB row + their own
        # secret at runtime; nothing here holds a stack-wide database credential.
        base_env = {"STACK_NAME": parent_stack_name}
        oidc_discovery_url = f"{oidc_issuer_url}/.well-known/openid-configuration"

        # ── Sessions DDB Table ───────────────────────────────────────────
        sessions_table = dynamodb.Table(self, "SessionsTable",
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sort_key", type=dynamodb.AttributeType.STRING),
            **stage.table_settings(self),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
        )

        # ── Gateway Interceptor Lambda ───────────────────────────────────
        # Carries PyJWT and the identity provider classes so it can verify the
        # caller's token itself. The interceptor payload has no claims object —
        # only the bearer token — so verifying that token is the only way to
        # obtain a trustworthy subject.
        interceptor_fn = stage.python_function(self, "InterceptorFunction",
            failures=SYNCHRONOUS.format("the AgentCore Gateway, as its request interceptor"),
            outside_vpc=("Verifies callers' tokens against Cognito's published signing keys;"
                         " reaches nothing in the VPC."),
            handler="interceptor_handler.handler",
            code=lambda_.Code.from_asset(paths.PACKAGE, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(interceptor_steps(), pip=PYJWT)),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "IDENTITY_PROVIDER": identity_provider,
                "OIDC_ISSUER": oidc_issuer_url,
                # The clients the gateway authorizer accepts. A token issued to
                # anything else is refused even if its signature is valid.
                "ALLOWED_CLIENT_IDS": Fn.join(",", allowed_client_ids),
            },
        )

        # ── MCP Dispatcher Lambda ────────────────────────────────────────
        # Turns the database the agent named into the caller's own connection by
        # asking the connection registry's function, and runs statements through
        # the engine providers. It reads no table and no secret.
        mcp_fn = stage.python_function(self, "McpDispatcherFunction",
            failures=SYNCHRONOUS.format("the AgentCore Gateway"),
            outside_vpc=("Routes tool calls by invoking the provider functions (Lambda, SSM APIs);"
                         " those that reach databases are in the VPC."),
            handler="mcp_handler.handler",
            code=lambda_.Code.from_asset(paths.PACKAGE, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(dispatcher_steps())),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment=base_env,
        )
        mcp_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{parent_stack_name}/*"],
        ))
        # Only functions the provider stacks tagged as providers (tag set in each
        # provider stack). The dispatcher cannot call the API function, the
        # interceptor, the ingestion function, or anything else in the account.
        mcp_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:*"],
            conditions={"StringEquals": {"aws:ResourceTag/mcp-service-provider": parent_stack_name}},
        ))
        nag.own_parameters(mcp_fn, parent_stack_name)
        nag.tagged_providers(mcp_fn, parent_stack_name)
        # ── web_search MCP tool Lambda, with web_search_model_id ─────────
        # Its own function and gateway target, so only it holds Bedrock permissions. Off unless
        # the deployment turns it on: the agent's query goes to a web search outside the VPC.
        web_search_fn = None
        if web_search_model_id:
            web_search_fn = self._web_search_function(stage, web_search_model_id)

        # ── AgentCore Gateway (CUSTOM_JWT) ───────────────────────────────
        gateway_role = iam.Role(self, "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                }),
        )
        gateway_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[mcp_fn.function_arn, interceptor_fn.function_arn,
                       *([web_search_fn.function_arn] if web_search_fn else [])],
        ))

        from cdk_infra.agent_provider.gateway_tool_schema import DATA_SOURCE_TOOLS, WEB_TOOLS

        def _build_input_schema(schema: dict):
            props = None
            if schema.get("properties"):
                props = {
                    k: agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                        type=v["type"], description=v.get("description"))
                    for k, v in schema["properties"].items()
                }
            return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                type=schema["type"], properties=props,
                required=schema.get("required") or None)

        gateway = agentcore.CfnGateway(self, "McpGateway",
            name=f"{parent_stack_name}-gw",
            protocol_type="MCP",
            role_arn=gateway_role.role_arn,
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=oidc_discovery_url,
                    allowed_clients=allowed_client_ids,
                    allowed_scopes=[f"{parent_stack_name}-gateway/invoke"],
                )),
            interceptor_configurations=[
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=interceptor_fn.function_arn)),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True),
                )],
        )

        # ── Gateway application logging ──────────────────────────────────
        # The gateway writes no logs until delivery is declared (source, destination,
        # delivery); without them a refused request leaves no trace anywhere.
        gateway_log_group = stage.log_group(self, "GatewayLogGroup")

        log_source = logs.CfnDeliverySource(self, "GatewayLogSource",
            name=f"{parent_stack_name}-gw-app-logs",
            log_type="APPLICATION_LOGS",
            resource_arn=gateway.attr_gateway_arn,
        )

        log_destination = logs.CfnDeliveryDestination(self, "GatewayLogDestination",
            name=f"{parent_stack_name}-gw-cwl",
            destination_resource_arn=gateway_log_group.log_group_arn,
        )

        gateway_delivery = logs.CfnDelivery(self, "GatewayLogDelivery",
            delivery_source_name=log_source.name,
            delivery_destination_arn=log_destination.attr_arn,
        )
        # CfnDelivery references the source by *name*, which is a plain string,
        # so CloudFormation infers no dependency and can try to create the
        # delivery before the source exists.
        gateway_delivery.node.add_dependency(log_source)

        def _target(construct_id: str, name: str, function, tools: list[dict]):
            return agentcore.CfnGatewayTarget(self, construct_id,
                gateway_identifier=gateway.attr_gateway_identifier,
                # Clients show every tool as "<target name>___<tool>", so this is the
                # label users see. Letters, digits and single hyphens only: the
                # gateway rejects underscores.
                name=name,
                target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=function.function_arn,
                            tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=[
                                    agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                        name=t["name"], description=t["description"],
                                        input_schema=_build_input_schema(t["inputSchema"]))
                                    for t in tools
                                ])))),
                credential_provider_configurations=[
                    agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE")],
            )

        _target("McpTarget", "adam", mcp_fn, DATA_SOURCE_TOOLS)
        if web_search_fn:
            _target("WebSearchTarget", "web-search", web_search_fn, WEB_TOOLS)

        # ── AgentCore Runtime (CUSTOM_JWT), with enable_chat ─────────────
        # The agent behind the web console's chat page: a Strands agent in a container,
        # calling the same Gateway tools Quick calls, as the signed-in user. Its code and
        # Dockerfile are in src/adam/agent_provider/agent_runtime/.
        runtime = None
        if enable_chat:
            # Built from the package with everything excluded but AGENT_IMAGE_FILES: CDK stages
            # only those, so the build is sent the agent and its verifier and nothing else.
            agent_image = ecr_assets.DockerImageAsset(self, "AgentImage",
                directory=paths.PACKAGE,
                file="agent_provider/agent_runtime/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
                exclude=["**", *(f"!{f}" for f in AGENT_IMAGE_FILES)],
                ignore_mode=IgnoreMode.DOCKER,
            )

            runtime_role = iam.Role(self, "RuntimeRole",
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com",
                    conditions={
                        "StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                    }),
            )
            agent_image.repository.grant_pull(runtime_role)
            runtime_role.add_to_policy(iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                         "logs:DescribeLogGroups", "logs:DescribeLogStreams"],
                # AgentCore names the runtime's log groups <runtime name>-<id>-<endpoint>: only its own.
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/"
                           f"{parent_stack_name}_agent-*"]))
            # The image runs under opentelemetry-instrument: traces to X-Ray and metrics in
            # AgentCore's namespace. X-Ray has no resource-level permissions.
            runtime_role.add_to_policy(iam.PolicyStatement(
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                         "xray:GetSamplingRules", "xray:GetSamplingTargets"],
                resources=["*"]))
            runtime_role.add_to_policy(iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"], resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}))
            # The chat page's models only (chat_models.py), which the chat route also checks.
            runtime_role.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=chat_models.invoke_resources(self.region, self.account)))
            # The runtime appends events for the stream processor, and reads the conversation's
            # earlier turns as history, under the caller its token verifies; it never deletes.
            runtime_role.add_to_policy(iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:Query"], resources=[sessions_table.table_arn]))
            stage.data_key(self).grant_encrypt_decrypt(runtime_role)
            nag.acknowledge(runtime_role, "IAM5", RUNTIME_TELEMETRY, findings=["Resource::*"])
            cfn_nag.skip_role_policies(runtime_role, "W12", RUNTIME_TELEMETRY)
            nag.acknowledge(runtime_role, "IAM5", "AgentCore names the runtime's log groups after it with a "
                            "generated id; only this runtime's.",
                            findings=[nag.arn("logs", "log-group:/aws/bedrock-agentcore/runtimes/"
                                                      f"{parent_stack_name}_agent-*")])
            nag.acknowledge(runtime_role, "IAM5", CROSS_REGION_INFERENCE,
                            findings=[RegexAppliesTo(regex=r"/^Resource::arn:aws:bedrock:\*::foundation-model\/.+$/")])

            runtime = agentcore.CfnRuntime(self, "AgentRuntime",
                agent_runtime_name=f"{parent_stack_name}_agent",
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=agent_image.image_uri)),
                role_arn=runtime_role.role_arn,
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                    network_mode="VPC",
                    network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                        security_groups=[app_sg.security_group_id],
                        subnets=private_subnets(private_subnet_ids))),
                authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                    custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                        discovery_url=oidc_discovery_url,
                        # The website sends the user's access token, which names its client in
                        # client_id (an access token has no audience); the agent forwards it.
                        allowed_clients=[frontend_client_id])),
                request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                    request_header_allowlist=["Authorization"]),
                environment_variables={
                    "GATEWAY_URL": gateway.attr_gateway_url,
                    "SESSIONS_TABLE": sessions_table.table_name,
                    "APP_REGION": self.region,
                    # The runtime verifies each caller's token itself, as the interceptor does.
                    "IDENTITY_PROVIDER": identity_provider,
                    "OIDC_ISSUER": oidc_issuer_url,
                    "ALLOWED_CLIENT_IDS": frontend_client_id,
                    **({"AWS_ENDPOINT_URL_DYNAMODB": f"https://{dynamodb_endpoint_url}"}
                       if dynamodb_endpoint_url else {}),
                },
            )

        # ── AppSync Events API ───────────────────────────────────────────
        events_api = appsync.CfnApi(self, "EventsApi",
            name=f"{parent_stack_name}-events",
            event_config=appsync.CfnApi.EventConfigProperty(
                auth_providers=[
                    appsync.CfnApi.AuthProviderProperty(
                        auth_type="AMAZON_COGNITO_USER_POOLS",
                        cognito_config=appsync.CfnApi.CognitoConfigProperty(
                            aws_region=self.region, user_pool_id=user_pool_id)),
                    appsync.CfnApi.AuthProviderProperty(auth_type="AWS_IAM"),
                ],
                connection_auth_modes=[appsync.CfnApi.AuthModeProperty(auth_type="AMAZON_COGNITO_USER_POOLS")],
                default_publish_auth_modes=[appsync.CfnApi.AuthModeProperty(auth_type="AWS_IAM")],
                default_subscribe_auth_modes=[appsync.CfnApi.AuthModeProperty(auth_type="AMAZON_COGNITO_USER_POOLS")],
            ))
        # Channels are /agent/{user sub}/{session id}; the handler lets a caller
        # subscribe only under their own sub (see on_subscribe.js and its tests).
        appsync.CfnChannelNamespace(self, "AgentChannel",
            api_id=events_api.attr_api_id,
            name="agent",
            # Code handlers need no handler_configs: those describe Lambda data
            # source integrations, which this JavaScript does not use.
            code_handlers=(Path(__file__).parent / "on_subscribe.js").read_text(),
        )

        # ── Sessions Stream Processor ────────────────────────────────────
        stream_fn = stage.python_function(self, "SessionsStreamProcessor",
            failures=STREAM_DLQ,
            outside_vpc="Publishes chat events to AppSync Events; reaches nothing in the VPC.",
            handler="sessions_stream_handler.handler",
            # Only its own file: it imports nothing from this package.
            code=lambda_.Code.from_asset(paths.AGENT_PROVIDER,
                exclude=["**/__pycache__"],
                bundling=bundling_options([copy_files("", ["sessions_stream_handler.py"], "")])),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={"EVENTS_API_HTTP_ENDPOINT": events_api.attr_dns_http},
        )
        stream_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["appsync:EventPublish"],
            resources=[f"arn:aws:appsync:{self.region}:{self.account}:apis/{events_api.attr_api_id}/*"]))
        nag.acknowledge(stream_fn, "IAM5", "Publishes to channels of this deployment's own Events API only; "
                        "a channel is named per user and conversation, so they cannot be listed.",
                        findings=[RegexAppliesTo(regex=r"/^Resource::arn:aws:appsync:.*:apis\/<EventsApi[A-Za-z0-9]*\.ApiId>\/\*$/")])
        nag.acknowledge(stream_fn, "IAM5", LIST_STREAMS, findings=["Resource::*"])
        cfn_nag.skip_role_policies(stream_fn.role, "W12", LIST_STREAMS)
        # What still fails after its retries, kept for inspection: one bad record must
        # not hold up every chat behind it on the shard.
        stream_dlq = stage.failure_queue(self, "SessionsStreamFailures")
        stream_fn.add_event_source(lambda_events.DynamoEventSource(sessions_table,
            starting_position=lambda_.StartingPosition.LATEST, batch_size=10,
            # A failed channel is retried on its own, the batch split to isolate it,
            # for at most 5 minutes: a chat event any older is no use to anyone.
            report_batch_item_failures=True,
            bisect_batch_on_error=True,
            retry_attempts=2,
            max_record_age=Duration.minutes(5),
            on_failure=lambda_events.SqsDlq(stream_dlq)))

        # ── Chat API Lambda (the agent's HTTP routes) ───────────────────
        api_lambda = stage.python_function(self, "ApiFunction",
            failures=SYNCHRONOUS.format("the HTTP API"),
            handler="api_handler.handler",
            code=lambda_.Code.from_asset(paths.PACKAGE, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(chat_api_steps())),
            # The HTTP API gives up on a route at 30 s; the chat route waits only for the
            # runtime to start the turn (chat.py).
            timeout=Duration.seconds(30),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[app_sg],
            environment={
                **base_env,
                "SESSIONS_TABLE": sessions_table.table_name,
                **({"RUNTIME_ARN": runtime.attr_agent_runtime_arn} if runtime else {}),
                "CHAT_MODELS": json.dumps(chat_models.CHAT_MODELS),
                "DEPLOY_REGION": self.region,
                **({"AWS_ENDPOINT_URL_DYNAMODB": f"https://{dynamodb_endpoint_url}"} if dynamodb_endpoint_url else {}),
            },
        )
        api_lambda.add_to_role_policy(origin_allowlist_read(self, parent_stack_name))
        nag.own_parameters(api_lambda, parent_stack_name)
        # Conversation history: records each prompt and conversation, lists and reads
        # them back, deletes them, and writes the "your chat failed" event. Every call
        # is keyed by the caller's verified sub (conversations.py).
        api_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
                     "dynamodb:BatchWriteItem"],
            resources=[sessions_table.table_arn]))
        stage.data_key(self).grant_encrypt_decrypt(api_lambda)

        # ── HTTP API Gateway ────────────────────────────────────────────
        jwt_authorizer = HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=oidc_issuer_url,
            jwt_audience=[frontend_client_id],
        )

        http_api = apigwv2.HttpApi(self, "HttpApi",
            default_authorizer=jwt_authorizer,
            # The stage is declared below, with its access log and rate limit.
            create_default_stage=False,
            # No cors_preflight: when one is set the gateway answers preflights itself and
            # ignores the CORS headers the Lambda returns, so the origin allowlist in
            # shared/http_response.py would never apply. CORS is decided there, from the
            # origin_* SSM parameters; the OPTIONS route below sends preflights to it.
        )
        # Who called which route and how it answered, the record a security review asks for;
        # and, since /api/chat starts a model invocation, a rate limit. Declared where the
        # default stage was, so it is the same stage.
        api_stage = apigwv2.HttpStage(http_api, "DefaultStage", http_api=http_api, stage_name="$default",
            auto_deploy=True,
            access_log_settings=_AccessLog(
                apigwv2.LogGroupLogDestination(stage.log_group(self, "HttpApiAccessLogs")),
                apigw.AccessLogFormat.custom(json.dumps({
                    "requestId": "$context.requestId", "time": "$context.requestTime",
                    "ip": "$context.identity.sourceIp", "sub": "$context.authorizer.claims.sub",
                    "route": "$context.routeKey", "status": "$context.status",
                    "latencyMs": "$context.responseLatency", "error": "$context.error.message"}))),
            throttle=apigwv2.ThrottleSettings(rate_limit=API_RATE_LIMIT, burst_limit=API_BURST_LIMIT))

        no_auth = apigwv2.HttpNoneAuthorizer()
        api_integration = HttpLambdaIntegration("ApiIntegration", api_lambda)

        http_api.add_routes(path="/api/health", methods=[apigwv2.HttpMethod.GET],
            integration=api_integration)
        http_api.add_routes(path="/api/{proxy+}", methods=[
            apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE,
        ], integration=api_integration)
        for route in http_api.add_routes(path="/api/{proxy+}", methods=[apigwv2.HttpMethod.OPTIONS],
                                         integration=api_integration, authorizer=no_auth):
            nag.acknowledge(route, "APIG4", PREFLIGHT_UNAUTHENTICATED)
        # Other services' routes, each to the function that owns them.
        methods = [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST,
                   apigwv2.HttpMethod.PATCH, apigwv2.HttpMethod.DELETE]
        for name, function, paths_ in (
                ("ConnectionsApiIntegration", connections_api_function,
                 ("/api/connections", "/api/connections/{proxy+}", "/api/databases")),
                ("AdminApiIntegration", admin_function, ("/api/admin/{proxy+}",))):
            integration = HttpLambdaIntegration(name, function)
            for path in paths_:
                http_api.add_routes(path=path, methods=methods, integration=integration)
                for route in http_api.add_routes(path=path, methods=[apigwv2.HttpMethod.OPTIONS],
                                                 integration=integration, authorizer=no_auth):
                    nag.acknowledge(route, "APIG4", PREFLIGHT_UNAUTHENTICATED)

        # ── Outputs ─────────────────────────────────────────────────────
        if runtime:
            # The website shows its chat page only when this output is there.
            CfnOutput(self, "RuntimeArn", value=runtime.attr_agent_runtime_arn)
        CfnOutput(self, "GatewayId", value=gateway.attr_gateway_identifier)
        CfnOutput(self, "QuickMcpServerUrl", value=gateway.attr_gateway_url,
                  description="Amazon Quick MCP integration: MCP server endpoint")
        CfnOutput(self, "HttpApiUrl", value=api_stage.url)
        CfnOutput(self, "EventsApiEndpoint", value=events_api.attr_dns_http)
        CfnOutput(self, "EventsApiRealtimeEndpoint", value=events_api.attr_dns_realtime)

    def _web_search_function(self, stage: Stage, web_search_model_id: str) -> lambda_.Function:
        """The web_search tool's function: Amazon Nova web grounding, and the only Bedrock grant it has."""
        web_search_fn = stage.python_function(self, "WebSearchFunction",
            failures=SYNCHRONOUS.format("the AgentCore Gateway"),
            outside_vpc="Calls Amazon Bedrock only; reaches nothing in the VPC.",
            handler="web_search_handler.handler",
            code=lambda_.Code.from_asset(paths.PACKAGE, exclude=SOURCE_EXCLUDES,
                bundling=bundling_options(web_search_steps(), pip=(BOTO3,))),
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={"WEB_SEARCH_MODEL_ID": web_search_model_id},
        )
        # Nova web grounding. A US cross-region inference profile routes to the
        # foundation model in any US region, so the model grant is region-wildcarded;
        # the system tool is a separate, account-level grant.
        web_search_model = web_search_model_id.split(".", 1)[1]
        web_search_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{web_search_model_id}",
                f"arn:aws:bedrock:*::foundation-model/{web_search_model}",
            ],
        ))
        nag.acknowledge(web_search_fn, "IAM5", CROSS_REGION_INFERENCE,
                        findings=[f"Resource::arn:aws:bedrock:*::foundation-model/{web_search_model}"])
        web_search_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeTool"],
            resources=[f"arn:aws:bedrock::{self.account}:system-tool/amazon.nova_grounding"],
        ))
        return web_search_fn
