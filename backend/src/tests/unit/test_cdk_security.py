#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Security properties of the synthesized CloudFormation.

Stacks are built for real (CDK's own template assertions), with bundling off so
no container runs. Building them also proves each template is deployable: a
dependency cycle, which `cdk synth` accepts, raises here.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from aws_cdk.assertions import Template

BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adam.schema_provider import scan_targets  # noqa: E402
from cdk_infra.agent_provider.chat_models import CHAT_MODELS  # noqa: E402
from tests.unit.cdk_stacks import ENV, PREFIX, build_stacks  # noqa: E402


@pytest.fixture(scope="module")
def templates() -> dict[str, Template]:
    # Stacks reference their Lambda sources relative to the backend directory.
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        return _build()
    finally:
        os.chdir(previous)


@pytest.fixture(scope="module")
def stacks() -> dict:
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        return build_stacks()
    finally:
        os.chdir(previous)


def _build() -> dict[str, Template]:
    return {name: Template.from_stack(stack) for name, stack in build_stacks().items()}


PROVIDERS = ("OracleProviderFunction", "PostgreSQLProviderFunction", "MySQLProviderFunction",
             "MariaDBProviderFunction", "RedshiftProviderFunction")
SCANNERS = {"oracle": "OracleScannerFunction", "postgres": "PostgresScannerFunction", "mysql": "MySQLScannerFunction",
            "mariadb": "MariaDBScannerFunction", "redshift": "RedshiftScannerFunction"}
SECRETS = f"arn:aws:secretsmanager:{ENV.region}:{ENV.account}:secret:"
SCANNER_PATH = f"{SECRETS}/{PREFIX}/scanners/*"
CONNECTIONS_PATH = f"{SECRETS}/{PREFIX}/connections/*"


def _function(templates, prefix: str, stack: str = "agent") -> tuple[str, dict]:
    functions = templates[stack].find_resources("AWS::Lambda::Function")
    return next((k, r) for k, r in functions.items() if k.startswith(prefix))


def _role_statements(templates, function_prefix: str, stack: str = "agent") -> list[dict]:
    """The statements of the policies attached to one function's role."""
    _, function = _function(templates, function_prefix, stack)
    role = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return [st for pol in templates[stack].find_resources("AWS::IAM::Policy").values()
            if {"Ref": role} in pol["Properties"]["Roles"]
            for st in pol["Properties"]["PolicyDocument"]["Statement"]]


def _role_policy_statements(template: Template, role_prefix: str) -> list[dict]:
    """The inline statements attached to the role whose logical id starts with role_prefix."""
    (role,) = [k for k in template.find_resources("AWS::IAM::Role") if k.startswith(role_prefix)]
    return [st for pol in template.find_resources("AWS::IAM::Policy").values()
            if {"Ref": role} in pol["Properties"]["Roles"]
            for st in pol["Properties"]["PolicyDocument"]["Statement"]]


def _statements(template: Template) -> list[dict]:
    out = []
    for resource in template.find_resources("AWS::IAM::Policy").values():
        out.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return out


def _invokers(templates, function_logical_id: str) -> set[str]:
    """Every role, in any stack, whose own policy lets it invoke the function, however the
    function's ARN is written (GetAtt here, an import from another stack)."""
    holders = set()
    for template in templates.values():
        for policy in template.find_resources("AWS::IAM::Policy").values():
            for st in policy["Properties"]["PolicyDocument"]["Statement"]:
                if "lambda:InvokeFunction" in _actions(st) and function_logical_id in json.dumps(_resources(st)):
                    holders |= {r["Ref"] for r in policy["Properties"]["Roles"]}
    return holders


def _own_key(templates, stack: str) -> list:
    """The resource list naming the stack's own data key: tables, logs, settings and queues use it."""
    (key,) = templates[stack].find_resources("AWS::KMS::Key")
    return [{"Fn::GetAtt": [key, "Arn"]}]


def _grants(statements: list[dict]) -> list[tuple]:
    """(actions, resources, condition) per statement, comparable as a whole."""
    return [(_actions(s), _resources(s), s.get("Condition")) for s in statements]


def _actions(statement: dict) -> list[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


def _resources(statement: dict) -> list:
    resource = statement.get("Resource", [])
    return [resource] if isinstance(resource, (str, dict)) else list(resource)


def _schema_function(templates) -> list:
    """The schema service's function as the provider stack sees it, and its versions: the Glue
    stack's output naming the function, read across stacks (not a CloudFormation export)."""
    (function,) = [k for k in templates["glue"].find_resources("AWS::Lambda::Function")
                   if k.startswith("GlueCatalogProviderFunction")]
    (output,) = [k for k, o in templates["glue"].to_json()["Outputs"].items()
                 if o["Value"] == {"Fn::GetAtt": [function, "Arn"]}]
    arn = {"Fn::GetStackOutput": {"StackName": "T-Glue", "Region": "us-west-2", "OutputName": output}}
    return [arn, {"Fn::Join": ["", [arn, ":*"]]}]


class TestInvokePermissions:

    def test_the_dispatcher_holds_only_provider_invokes_and_its_parameters(self, templates):
        """Its whole grant: invoke only functions tagged as this stack's providers, and read
        the parameters naming them. Anything added to its role fails this."""
        assert _grants(_role_statements(templates, "McpDispatcherFunction")) == [
            (["kms:Decrypt"], _own_key(templates, "agent"), None),
            (["ssm:GetParameter"], [f"arn:aws:ssm:{ENV.region}:{ENV.account}:parameter/{PREFIX}/*"], None),
            (["lambda:InvokeFunction"], [f"arn:aws:lambda:{ENV.region}:{ENV.account}:function:*"],
             {"StringEquals": {"aws:ResourceTag/mcp-service-provider": PREFIX}}),
        ]

    def test_the_api_function_invokes_nothing(self, templates):
        """The chat route calls the runtime over HTTPS with the caller's own header; it never
        re-invokes a function, which would put the caller's token in an event payload."""
        assert not [s for s in _role_statements(templates, "ApiFunction") if "lambda:InvokeFunction" in _actions(s)]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_data_source_providers_invoke_only_the_schema_service(self, templates, provider):
        """They read each connection's registered database from the schema service's
        function, and may invoke nothing else: not each other, not the registry."""
        invokes = [s for s in _role_statements(templates, provider, "external")
                   if "lambda:InvokeFunction" in _actions(s)]
        assert [_resources(s) for s in invokes] == [_schema_function(templates)]

    def test_the_providers_and_the_registry_are_tagged_and_the_connections_api_is_not(self, templates):
        """The dispatcher may invoke what is tagged; the connections API answers only the HTTP API."""
        expected = {**dict.fromkeys(PROVIDERS, True), "ConnectionRegistryFunction": True,
                    "ConnectionsApiFunction": False}
        for prefix, tagged in expected.items():
            _, function = _function(templates, prefix, "external")
            tags = {t["Key"]: t["Value"] for t in function["Properties"].get("Tags", [])}
            assert (tags.get("mcp-service-provider") == PREFIX) is tagged, prefix

    def test_the_search_function_is_tagged_as_a_provider(self, templates):
        search = [r for k, r in templates["glue"].find_resources("AWS::Lambda::Function").items()
                  if k.startswith("GlueCatalogProviderFunction")]
        tags = {t["Key"]: t["Value"] for t in search[0]["Properties"].get("Tags", [])}
        assert tags.get("mcp-service-provider") == PREFIX


class TestProviderRolesHaveOnlyWhatTheyUse:

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_a_provider_holds_only_its_row_read_the_secret_reader_and_the_registration_read(
            self, templates, provider):
        """Its whole grant: GetItem on the connections table (the row it is given),
        assuming the reader role, tagged, to read that row owner's secret, and invoking
        the schema service's function for the row's registered database."""
        table = [k for k in templates["external"].find_resources("AWS::DynamoDB::Table")
                 if k.startswith("ConnectionsTable")]
        reader = [k for k in templates["external"].find_resources("AWS::IAM::Role") if k.startswith("SecretReader")]
        assert sorted(_grants(_role_statements(templates, provider, "external"))) == sorted([
            (["sts:AssumeRole", "sts:TagSession"], [{"Fn::GetAtt": [reader[0], "Arn"]}], None),
            (["dynamodb:GetItem"], [{"Fn::GetAtt": [table[0], "Arn"]}], None),
            (["lambda:InvokeFunction"], _schema_function(templates), None),
            (["kms:Decrypt"], _own_key(templates, "external"), None),
        ])


class TestNoBroadGrants:

    def test_no_role_reads_connection_secrets_by_a_broad_path(self, templates):
        """A user's secret is read per user, through the reader role; a scanner login only
        by a scanner (TestProvidersReadOnlyTheirCallersSecrets, TestScannerLogins)."""
        for name, template in templates.items():
            for s in _statements(template):
                if "secretsmanager:GetSecretValue" in _actions(s):
                    for r in _resources(s):
                        assert "${aws:PrincipalTag/userId}" in str(r) or r == SCANNER_PATH, f"{name}: {r}"

    def test_only_the_web_search_function_and_the_chat_agent_may_call_bedrock(self, templates):
        """Web search is its own MCP tool function, so the database dispatcher and the API
        hold no model access; the chat agent's role calls the model the user picked."""
        holders = set()
        for template in templates.values():
            for policy in template.find_resources("AWS::IAM::Policy").values():
                if any(a.startswith("bedrock:") for st in policy["Properties"]["PolicyDocument"]["Statement"]
                       for a in _actions(st)):
                    holders |= {r["Ref"].split("ServiceRole")[0] for r in policy["Properties"]["Roles"]}
        assert {h.rstrip("0123456789ABCDEF") for h in holders} == {"WebSearchFunction", "RuntimeRole"}, holders

    def test_no_role_has_a_managed_policy_beyond_lambdas_own(self, templates):
        """No FullAccess, or any other broad AWS policy: each role's inline statements are its access."""
        allowed = {"AWSLambdaBasicExecutionRole", "AWSLambdaVPCAccessExecutionRole"}
        for name, template in templates.items():
            for logical_id, role in template.find_resources("AWS::IAM::Role").items():
                for arn in role["Properties"].get("ManagedPolicyArns", []):
                    assert json.dumps(arn).rsplit("/", 1)[-1].strip('"]}') in allowed, f"{name}/{logical_id}: {arn}"

    def test_the_chat_agent_holds_only_what_it_uses(self, templates):
        statements = _role_policy_statements(templates["agent"], "RuntimeRole")
        granted = {a for st in statements for a in _actions(st)}
        assert granted == {
            # pull its image
            "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
            "ecr:GetAuthorizationToken",
            # its logs, traces and metrics
            "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups",
            "logs:DescribeLogStreams", "xray:PutTraceSegments", "xray:PutTelemetryRecords",
            "xray:GetSamplingRules", "xray:GetSamplingTargets", "cloudwatch:PutMetricData",
            # the chat page's models, the events the browser reads, and the conversation's history
            "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "dynamodb:PutItem", "dynamodb:Query",
            # the sessions table's key, to write and read those events
            "kms:Decrypt", "kms:Encrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*",
        }, granted
        assert all(_resources(st) == _own_key(templates, "agent") for st in statements
                   if any(a.startswith("kms:") for a in _actions(st))), statements
        for st in statements:
            actions = _actions(st)
            if any(a.startswith("bedrock:") for a in actions):
                # Each offered model's profile and its foundation model, and no wildcard model.
                resources = _resources(st)
                assert not [r for r in resources if r.endswith(("/*", ":*"))], st
                assert {f"arn:aws:bedrock:{ENV.region}:{ENV.account}:inference-profile/{m['id']}"
                        for m in CHAT_MODELS} <= set(resources), st
                assert "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5-5" in resources, st
            if "cloudwatch:PutMetricData" in actions:
                assert st["Condition"] == {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}, st
            if "dynamodb:PutItem" in actions or "dynamodb:Query" in actions:
                assert len(_resources(st)) == 1 and "SessionsTable" in json.dumps(_resources(st)), st

    def test_no_model_invocation_on_every_resource(self, templates):
        for name, template in templates.items():
            for s in _statements(template):
                if any(a.startswith("bedrock:InvokeModel") for a in _actions(s)):
                    assert "*" not in _resources(s), f"{name}: {s}"

    def test_appsync_event_access_is_scoped_to_the_events_api(self, templates):
        grants = [s for s in _statements(templates["agent"]) if any(a.startswith("appsync:Event") for a in _actions(s))]
        assert grants
        for s in grants:
            for r in _resources(s):
                assert '"EventsApi' in json.dumps(r) and '"ApiId"' in json.dumps(r), s

    def test_log_writes_are_scoped_to_log_groups(self, templates):
        writes = [s for s in _statements(templates["agent"])
                  if any(a.startswith("logs:") and a != "logs:CreateLogDelivery" for a in _actions(s))]
        assert writes, "the chat agent writes its own logs"
        for s in writes:
            for r in _resources(s):
                assert ":log-group:/aws/bedrock-agentcore/runtimes/" in json.dumps(r), s

    def test_nothing_in_the_agent_stack_reads_a_secret(self, templates):
        """The connections routes ask a provider to log in; only providers hold credentials."""
        reads = [s for s in _statements(templates["agent"]) if "secretsmanager:GetSecretValue" in _actions(s)]
        assert reads == []

    def test_secret_writes_are_only_connection_and_scanner_secrets(self, templates):
        writes = [s for t in templates.values() for s in _statements(t)
                  if {"secretsmanager:CreateSecret", "secretsmanager:UpdateSecret",
                      "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret"} & set(_actions(s))]
        assert writes
        for s in writes:
            for r in _resources(s):
                assert str(r).endswith((f":secret:/{PREFIX}/connections/*", f":secret:/{PREFIX}/scanners/*")), r
            if "secretsmanager:UpdateSecret" in _actions(s) or "secretsmanager:PutSecretValue" in _actions(s):
                assert all(str(r).endswith(f":secret:/{PREFIX}/connections/*") for r in _resources(s)), s


class TestChatDoesNotRetainTheBearerToken:

    def test_no_agent_function_is_invoked_asynchronously(self, templates):
        """An asynchronous invocation is queued by Lambda and kept for retries and failure
        destinations: nothing in the chat path may go through one."""
        assert templates["agent"].find_resources("AWS::Lambda::EventInvokeConfig") == {}


def _table(templates, prefix: str, stack: str = "glue") -> str:
    return next(k for k in templates[stack].find_resources("AWS::DynamoDB::Table") if k.startswith(prefix))


class TestIngestionFunction:
    """The schema service's side of scanning: it asks on the bus and indexes what comes
    back. It holds no secret, no driver and no database connection."""

    def _ingestion(self, templates):
        return _function(templates, "SchemaIngestionFunction", "glue")[1]

    def _grants(self, templates):
        return _role_statements(templates, "SchemaIngestionFunction", "glue")

    def test_it_is_told_its_tables_and_the_bus(self, templates):
        env = self._ingestion(templates)["Properties"]["Environment"]["Variables"]
        assert env["SCAN_TARGETS_TABLE"] == {"Ref": _table(templates, "ScanTargetsTable")}
        assert env["SCAN_PROVIDERS_TABLE"] == {"Ref": _table(templates, "ScanProvidersTable")}
        bus = next(iter(templates["glue"].find_resources("AWS::Events::EventBus")))
        assert env["SCAN_BUS_NAME"] == {"Ref": bus}
        assert not {"CREDENTIAL_PROVIDER", "SCANNER_SECRET_TAG", "INGESTION_ROLE_NAME"} & set(env)

    def test_it_holds_no_secret_permission(self, templates):
        assert [s for s in self._grants(templates) if any(a.startswith("secretsmanager:") for a in _actions(s))] == []

    def test_it_reads_the_scan_targets_and_may_write_only_a_targets_status(self, templates):
        """Not the target's host, secret or flags."""
        targets = {"Fn::GetAtt": [_table(templates, "ScanTargetsTable"), "Arn"]}
        grants = [s for s in self._grants(templates) if targets in _resources(s)]
        assert sorted(sorted(_actions(s)) for s in grants) == [["dynamodb:GetItem", "dynamodb:Scan"],
                                                               ["dynamodb:UpdateItem"]]
        (write,) = [s for s in grants if _actions(s) == ["dynamodb:UpdateItem"]]
        assert write["Condition"] == {"ForAllValues:StringEquals": {
            "dynamodb:Attributes": ["targetId", *scan_targets.STATUS_FIELDS, "scanRequestId"]}}

    def test_it_records_the_providers_registrations(self, templates):
        providers = {"Fn::GetAtt": [_table(templates, "ScanProvidersTable"), "Arn"]}
        (grant,) = [s for s in self._grants(templates) if providers in _resources(s)]
        assert sorted(_actions(grant)) == ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem",
                                           "dynamodb:Scan"]

    def test_it_may_publish_only_as_the_schema_service(self, templates):
        (publish,) = [s for s in self._grants(templates) if "events:PutEvents" in _actions(s)]
        assert publish["Condition"] == {"StringEquals": {"events:source": f"{PREFIX}.schema-service"}}

    def test_only_the_providers_events_are_delivered_to_it(self, templates):
        rules = [r for r in templates["glue"].find_resources("AWS::Events::Rule").values()
                 if "SchemaIngestionFunction" in json.dumps(r["Properties"]["Targets"])]
        assert len(rules) == 1
        assert rules[0]["Properties"]["EventPattern"] == {
            "source": [f"{PREFIX}.database-provider"],
            "detail-type": ["ProviderRegistered", "ProviderDeregistered", "ScanCompleted", "ScanFailed"]}
        (target,) = rules[0]["Properties"]["Targets"]
        assert "DeadLetterConfig" in target

    def test_a_failed_request_is_not_retried_and_is_kept(self, templates):
        """A retried scan request means a retried login, which can lock the scanner account."""
        configs = [c for c in templates["glue"].find_resources("AWS::Lambda::EventInvokeConfig").values()
                   if "SchemaIngestionFunction" in json.dumps(c["Properties"]["FunctionName"])]
        assert len(configs) == 1 and configs[0]["Properties"]["MaximumRetryAttempts"] == 0
        assert "OnFailure" in configs[0]["Properties"]["DestinationConfig"]

    def test_the_function_is_not_tagged_as_a_provider(self, templates):
        """So the dispatcher, which may invoke providers, cannot invoke it."""
        tags = {t["Key"] for t in self._ingestion(templates)["Properties"].get("Tags", [])}
        assert "mcp-service-provider" not in tags

    def test_nothing_in_the_schema_service_needs_a_deny(self, templates):
        assert [s for s in _statements(templates["glue"]) if s["Effect"] == "Deny"] == []


class TestScanners:
    """Each engine's scanner, in the database provider service: reached by its rule on the
    scan bus and directly by the schema service's admin function, and by nothing else."""

    @pytest.mark.parametrize("engine,prefix", SCANNERS.items())
    def test_it_knows_its_engine_bus_and_bucket(self, templates, engine, prefix):
        props = _function(templates, prefix, "external")[1]["Properties"]
        env = props["Environment"]["Variables"]
        assert env["ENGINE"] == engine and env["STACK_NAME"] == PREFIX
        assert {"SCAN_BUS_NAME", "SCAN_RESULTS_BUCKET", "SCANNER_ROLE_NAME"} <= set(env)
        assert "VpcConfig" in props, "it connects to the databases, which are in the VPC"

    @pytest.mark.parametrize("prefix", SCANNERS.values())
    def test_it_is_not_tagged_as_a_provider(self, templates, prefix):
        """The dispatcher may invoke tagged functions; it must never reach a scanner login."""
        tags = {t["Key"] for t in _function(templates, prefix, "external")[1]["Properties"].get("Tags", [])}
        assert "mcp-service-provider" not in tags

    @pytest.mark.parametrize("prefix", SCANNERS.values())
    def test_only_the_admin_function_and_its_rule_may_invoke_it(self, templates, prefix):
        key, _ = _function(templates, prefix, "external")
        permissions = [p["Properties"] for p in templates["external"].find_resources("AWS::Lambda::Permission").values()
                       if key in json.dumps(p["Properties"]["FunctionName"])]
        principals = sorted(json.dumps(p["Principal"]) for p in permissions)
        assert len(permissions) == 2, permissions
        assert any("AdminApiFunction" in p for p in principals), principals
        (from_rule,) = [p for p in permissions if p["Principal"] == "events.amazonaws.com"]
        assert "ScanRequestsRule" in json.dumps(from_rule["SourceArn"])
        # Nor may any role invoke it through its own policy, in any stack.
        assert {r.rstrip("0123456789ABCDEF") for r in _invokers(templates, key)} <= {"AdminApiFunctionServiceRole"}

    @pytest.mark.parametrize("engine,prefix", SCANNERS.items())
    def test_its_rule_delivers_only_scan_requests_for_its_engine(self, templates, engine, prefix):
        key, _ = _function(templates, prefix, "external")
        rules = [r["Properties"] for r in templates["external"].find_resources("AWS::Events::Rule").values()
                 if key in json.dumps(r["Properties"]["Targets"])]
        assert len(rules) == 1
        assert rules[0]["EventPattern"] == {"source": [f"{PREFIX}.schema-service"],
                                            "detail-type": ["ScanRequested"], "detail": {"engine": [engine]}}
        (target,) = rules[0]["Targets"]
        assert "DeadLetterConfig" in target

    @pytest.mark.parametrize("prefix", SCANNERS.values())
    def test_a_failed_scan_is_not_retried(self, templates, prefix):
        key, _ = _function(templates, prefix, "external")
        invoke_configs = templates["external"].find_resources("AWS::Lambda::EventInvokeConfig").values()
        configs = [c["Properties"] for c in invoke_configs if key in json.dumps(c["Properties"]["FunctionName"])]
        assert len(configs) == 1 and configs[0]["MaximumRetryAttempts"] == 0
        assert "OnFailure" in configs[0]["DestinationConfig"]

    @pytest.mark.parametrize("prefix", SCANNERS.values())
    def test_it_holds_only_scanner_logins_its_results_and_its_own_events(self, templates, prefix):
        statements = [s for s in _role_statements(templates, prefix, "external")
                      if not any(a.startswith(("logs:", "ec2:")) for a in _actions(s))]
        by_service = {}
        for s in statements:
            for service in {a.split(":")[0] for a in _actions(s)}:
                by_service.setdefault(service, []).append(s)
        assert set(by_service) == {"secretsmanager", "s3", "events", "sqs", "kms"}, statements
        # Its settings and the dead-letter queue are under the stack's own key, and no other.
        assert all(_resources(s) == _own_key(templates, "external") for s in by_service["kms"])
        # A failed scan's event goes to the dead-letter queue, and only there.
        assert all("ScanRequestsDeadLetterQueue" in json.dumps(_resources(s)) for s in by_service["sqs"])
        (secrets,) = by_service["secretsmanager"]
        assert sorted(_actions(secrets)) == ["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
                                             "secretsmanager:GetSecretValue"]
        assert _resources(secrets) == [SCANNER_PATH]
        (put,) = by_service["s3"]
        assert _actions(put) == ["s3:PutObject"] and "/scans/*" in json.dumps(_resources(put))
        (publish,) = by_service["events"]
        assert publish["Condition"] == {"StringEquals": {"events:source": f"{PREFIX}.database-provider"}}

    def test_scan_results_are_access_logged_to_a_private_bucket_kept_as_the_stage_keeps_logs(self, templates):
        buckets = templates["external"].find_resources("AWS::S3::Bucket")
        ((results_id, results),) = [(k, v) for k, v in buckets.items() if k.startswith("ScanResultsBucket")]
        ((logs_id, access_logs),) = [(k, v) for k, v in buckets.items() if k.startswith("ScanResultsAccessLogs")]
        assert results["Properties"]["LoggingConfiguration"]["DestinationBucketName"] == {"Ref": logs_id}
        assert access_logs["Properties"]["PublicAccessBlockConfiguration"]["BlockPublicPolicy"] is True
        assert "AccessControl" not in access_logs["Properties"], "log delivery is granted by policy, not an ACL"
        (rule,) = access_logs["Properties"]["LifecycleConfiguration"]["Rules"]
        assert rule["ExpirationInDays"] == 30 and access_logs["DeletionPolicy"] == "Delete"

    def test_only_the_ingestion_function_may_read_scan_results(self, templates):
        (results,) = [k for k in templates["external"].find_resources("AWS::S3::Bucket")
                      if k.startswith("ScanResultsBucket")]
        (policy,) = [p for p in templates["external"].find_resources("AWS::S3::BucketPolicy").values()
                     if p["Properties"]["Bucket"] == {"Ref": results}]
        allows = [s for s in policy["Properties"]["PolicyDocument"]["Statement"] if s["Effect"] == "Allow"]
        reads = [s for s in allows if "s3:GetObject" in _actions(s)]
        assert len(reads) == 1 and _actions(reads[0]) == ["s3:GetObject"]
        assert "SchemaIngestionFunction" in json.dumps(reads[0]["Principal"])
        assert "/scans/*" in json.dumps(_resources(reads[0]))
        # And no role reads it through its own policy: the bucket policy is the only way in.
        bucket = next(k for k in templates["external"].find_resources("AWS::S3::Bucket")
                      if k.startswith("ScanResultsBucket"))
        for name, template in templates.items():
            for st in _statements(template):
                if any(a.startswith(("s3:Get", "s3:List")) or a == "s3:*" for a in _actions(st)):
                    assert bucket not in json.dumps(_resources(st)), f"{name}: {st}"

    def test_the_results_bucket_is_private_encrypted_and_short_lived(self, templates):
        (bucket,) = [b["Properties"] for k, b in templates["external"].find_resources("AWS::S3::Bucket").items()
                     if k.startswith("ScanResultsBucket")]
        assert bucket["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
        assert bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        (rule,) = bucket["LifecycleConfiguration"]["Rules"]
        assert rule["Status"] == "Enabled" and rule["ExpirationInDays"] <= 7  # scanned DDL is not kept

    def test_each_scanner_is_registered_with_its_function_and_bucket(self, templates):
        resources = templates["external"].find_resources("AWS::CloudFormation::CustomResource").values()
        registrations = [r["Properties"] for r in resources if "Engine" in r["Properties"]]
        assert sorted(r["Engine"] for r in registrations) == sorted(SCANNERS)
        for r in registrations:
            assert SCANNERS[r["Engine"]] in json.dumps(r["ScannerFunctionArn"])
            assert "ScanResultsBucket" in json.dumps(r["ResultsBucket"])

    def test_the_registration_function_may_publish_only_as_a_provider(self, templates):
        statements = [s for s in _role_statements(templates, "ScannerRegistrationFunction", "external")
                      if not any(a.startswith("logs:") for a in _actions(s))]
        assert _grants([s for s in statements if _actions(s) == ["kms:Decrypt"]]) == [
            (["kms:Decrypt"], _own_key(templates, "external"), None)]
        statements = [s for s in statements if _actions(s) != ["kms:Decrypt"]]
        assert [_actions(s) for s in statements] == [["events:PutEvents"]]
        assert statements[0]["Condition"] == {"StringEquals": {"events:source": f"{PREFIX}.database-provider"}}


class TestAdminRoutesPermissions:

    def test_only_the_admin_function_and_the_scheduler_may_invoke_the_ingestion_function(self, templates):
        """In any stack: the function is handed to the providers' stack too."""
        ingestion = next(k for k in templates["glue"].find_resources("AWS::Lambda::Function")
                         if k.startswith("SchemaIngestionFunction"))
        holders = {r.split("ServiceRole")[0].split("Role")[0] for r in _invokers(templates, ingestion)}
        assert holders == {"AdminApiFunction", "ScanScheduler"}

    def test_no_function_writes_an_ssm_parameter_at_run_time(self, templates):
        """The scan-target list was once a parameter; it is a DynamoDB partition now."""
        for name, template in templates.items():
            for s in _statements(template):
                assert "ssm:PutParameter" not in _actions(s), f"{name}: {s}"

    def test_the_admin_function_uses_the_schema_services_own_tables(self, templates):
        env = _admin_env(templates)
        assert env["SCAN_TARGETS_TABLE"] == {"Ref": _table(templates, "ScanTargetsTable")}
        assert env["SCAN_PROVIDERS_TABLE"] == {"Ref": _table(templates, "ScanProvidersTable")}


def _admin_env(templates) -> dict:
    return _function(templates, "AdminApiFunction", "glue")[1]["Properties"]["Environment"]["Variables"]


class TestTheAdminRoutesHaveTheirOwnFunction:
    """Only the admin function's role, in the schema service's stack, reaches scan targets,
    schedules, the ingestion function and the providers' scanners."""

    ADMIN_ONLY = ("scheduler:", "iam:PassRole", "glue:GetTables")

    def test_the_user_api_holds_none_of_the_admin_permissions(self, templates):
        for st in _role_statements(templates, "ApiFunction"):
            assert not any(a.startswith(self.ADMIN_ONLY) for a in _actions(st)), st
            assert "/scanners/" not in json.dumps(st), st
            assert "mcp-service-ingestion" not in json.dumps(st), st

    def test_the_admin_function_invokes_only_the_ingestion_function_itself(self, templates):
        """The scanners grant it invoke on themselves; it holds no tag-wide invoke."""
        for st in _role_statements(templates, "AdminApiFunction", "glue"):
            if "lambda:InvokeFunction" in _actions(st):
                assert "Condition" not in st, st
                assert all("SchemaIngestionFunction" in json.dumps(r) for r in _resources(st)), st

    def test_the_admin_function_writes_scan_targets_and_reads_providers(self, templates):
        grants = {json.dumps(_resources(st)): sorted(_actions(st))
                  for st in _role_statements(templates, "AdminApiFunction", "glue")
                  if any(a.startswith("dynamodb:") for a in _actions(st))}
        targets = json.dumps([{"Fn::GetAtt": [_table(templates, "ScanTargetsTable"), "Arn"]}])
        providers = json.dumps([{"Fn::GetAtt": [_table(templates, "ScanProvidersTable"), "Arn"]}])
        assert set(grants) == {targets, providers}
        assert grants[providers] == ["dynamodb:GetItem", "dynamodb:Scan"]
        assert grants[targets] == ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan",
                                   "dynamodb:UpdateItem"]

    @pytest.mark.parametrize("path,stack,owner", [
        ("/api/admin/{proxy+}", "glue", "AdminApiFunction"),
        ("/api/connections", "external", "ConnectionsApiFunction"),
        ("/api/connections/{proxy+}", "external", "ConnectionsApiFunction"),
        ("/api/databases", "external", "ConnectionsApiFunction")])
    def test_each_path_is_routed_to_the_function_that_owns_it(self, templates, path, stack, owner):
        admin_key, _ = _function(templates, owner, stack)
        routes = templates["agent"].find_resources("AWS::ApiGatewayV2::Route")
        admin_routes = [r for r in routes.values() if r["Properties"]["RouteKey"].split()[1] == path]
        methods = {r["Properties"]["RouteKey"].split()[0] for r in admin_routes}
        assert methods == {"GET", "POST", "PATCH", "DELETE", "OPTIONS"}
        integrations = templates["agent"].find_resources("AWS::ApiGatewayV2::Integration")
        for r in admin_routes:
            target = json.dumps(r["Properties"]["Target"])
            integration = next(v for k, v in integrations.items() if k in target)
            assert admin_key in json.dumps(integration["Properties"]["IntegrationUri"])


class TestScannerLogins:
    """Scanner logins are the database provider service's secrets, under its scanner path.
    Only the scanners reach them, and no grant anywhere is bound to a tag."""

    def test_only_the_scanners_have_any_scanner_login_permission(self, templates):
        holders = set()
        for name, template in templates.items():
            for key, policy in template.find_resources("AWS::IAM::Policy").items():
                if "/scanners/" in json.dumps(policy["Properties"]["PolicyDocument"]):
                    holders.add((name, key.split("ServiceRole")[0]))
        assert holders == {("external", fn) for fn in SCANNERS.values()}

    def test_no_secret_grant_is_bound_to_a_tag(self, templates):
        for name, template in templates.items():
            for s in _statements(template):
                if any(a.startswith("secretsmanager:") for a in _actions(s)):
                    assert "ResourceTag" not in json.dumps(s) and "RequestTag" not in json.dumps(s), f"{name}: {s}"

    def test_no_function_may_tag_or_untag_a_secret(self, templates):
        for name, template in templates.items():
            for s in _statements(template):
                assert not any(a.startswith("secretsmanager:") and "Tag" in a for a in _actions(s)), f"{name}: {s}"

    def test_no_secret_action_is_granted_outside_this_deployments_paths(self, templates):
        own = (CONNECTIONS_PATH, SCANNER_PATH, f"{SECRETS}/{PREFIX}/connections/${{aws:PrincipalTag/userId}}/*")
        for name, template in templates.items():
            for s in _statements(template):
                if s["Effect"] == "Allow" and any(a.startswith("secretsmanager:") for a in _actions(s)):
                    for r in _resources(s):
                        assert r in own, f"{name}: {s}"


class TestScanSchedules:
    """Schedules are made at run time, but every piece of IAM they use exists at deploy
    time: a schedule group, a role only Scheduler may assume, and narrow grants."""

    def _group_name(self, templates):
        groups = templates["glue"].find_resources("AWS::Scheduler::ScheduleGroup")
        assert len(groups) == 1
        return next(iter(groups.values()))["Properties"]["Name"]

    def _scheduler_role(self, templates):
        roles = templates["glue"].find_resources("AWS::IAM::Role")
        return next((k, r) for k, r in roles.items() if k.startswith("ScanSchedulerRole"))

    def test_only_scheduler_in_this_account_and_group_may_assume_the_role(self, templates):
        _, role = self._scheduler_role(templates)
        [statement] = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert statement["Principal"] == {"Service": "scheduler.amazonaws.com"}
        assert statement["Condition"]["StringEquals"] == {"aws:SourceAccount": ENV.account}
        assert statement["Condition"]["ArnLike"] == {"aws:SourceArn": (
            f"arn:aws:scheduler:{ENV.region}:{ENV.account}:schedule/{self._group_name(templates)}/*")}

    def test_the_role_may_only_invoke_the_ingestion_function(self, templates):
        key, _ = self._scheduler_role(templates)
        policies = [p for p in templates["glue"].find_resources("AWS::IAM::Policy").values()
                    if {"Ref": key} in p["Properties"]["Roles"]]
        statements = [s for p in policies for s in p["Properties"]["PolicyDocument"]["Statement"]]
        assert statements and all(_actions(s) == ["lambda:InvokeFunction"] for s in statements)
        for s in statements:
            for r in _resources(s):
                assert "SchemaIngestionFunction" in json.dumps(r), r

    def test_the_admin_function_manages_schedules_only_in_that_group(self, templates):
        grants = [s for s in _statements(templates["glue"]) if any(a.startswith("scheduler:") for a in _actions(s))]
        assert len(grants) == 1
        assert sorted(_actions(grants[0])) == ["scheduler:CreateSchedule", "scheduler:DeleteSchedule",
                                               "scheduler:GetSchedule", "scheduler:UpdateSchedule"]
        assert _resources(grants[0]) == [
            f"arn:aws:scheduler:{ENV.region}:{ENV.account}:schedule/{self._group_name(templates)}/*"]

    def test_the_admin_function_may_pass_only_the_scheduler_role_and_only_to_scheduler(self, templates):
        passes = [s for t in templates.values() for s in _statements(t) if "iam:PassRole" in _actions(s)]
        assert len(passes) == 1
        assert _actions(passes[0]) == ["iam:PassRole"]
        [resource] = _resources(passes[0])
        assert "ScanSchedulerRole" in json.dumps(resource), resource
        assert passes[0]["Condition"] == {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}

    def test_nothing_can_create_or_change_iam_at_run_time(self, templates):
        for name, template in templates.items():
            for s in _statements(template):
                iam_actions = [a for a in _actions(s) if a.startswith("iam:")]
                assert iam_actions in ([], ["iam:PassRole"]), f"{name}: {s}"

    def test_the_admin_function_knows_the_group_role_and_target(self, templates):
        env = _admin_env(templates)
        assert env["SCAN_SCHEDULE_GROUP"] == self._group_name(templates)
        assert "ScanSchedulerRole" in json.dumps(env["SCAN_SCHEDULER_ROLE_ARN"])
        assert "SchemaIngestionFunction" in json.dumps(env["SCHEMA_INGESTION_FUNCTION_ARN"])


class TestTheConsoleCanCountIndexedTables:

    def test_the_admin_function_knows_the_index_database(self, templates):
        (database,) = templates["glue"].find_resources("AWS::Glue::Database").values()
        assert _admin_env(templates)["SCHEMA_INDEX_DATABASE"] == database["Properties"]["DatabaseInput"]["Name"]

    def test_it_may_list_tables_of_that_database_only(self, templates):
        reads = [s for s in _role_statements(templates, "AdminApiFunction", "glue") if "glue:GetTables" in _actions(s)]
        assert len(reads) == 1
        assert _actions(reads[0]) == ["glue:GetTables"]
        resources = [str(r) for r in _resources(reads[0])]
        assert resources and "*" not in resources
        assert all(f"{PREFIX.lower()}_schema_index" in r or r.endswith(":catalog") for r in resources), resources


class TestProvidersReadOnlyTheirCallersSecrets:
    """The provider functions hold no secret-read of their own. They assume a role whose
    policy is keyed on a session tag naming the user, so IAM refuses another user's secret."""

    PROVIDER_STACKS = ["external"]

    @pytest.mark.parametrize("prefix", PROVIDERS)
    def test_a_users_provider_cannot_reach_a_scanner_login(self, templates, prefix):
        assert "/scanners/" not in json.dumps(_role_statements(templates, prefix, "external"))

    @pytest.mark.parametrize("stack", PROVIDER_STACKS)
    def test_the_functions_use_the_user_scoped_provider_and_know_the_role(self, templates, stack):
        for prefix in PROVIDERS:
            env = _function(templates, prefix, stack)[1]["Properties"]["Environment"]["Variables"]
            assert env["CREDENTIAL_PROVIDER"] == "secrets_manager_user_scoped"
            assert "SECRET_READER_ROLE_ARN" in env

    def _reader_statements(self, template):
        """Statements of the policy attached to the SecretReader role."""
        out = []
        for policy in template.find_resources("AWS::IAM::Policy").values():
            roles = json.dumps(policy["Properties"]["Roles"])
            if "SecretReader" in roles:
                out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
        return out

    @pytest.mark.parametrize("stack", PROVIDER_STACKS)
    def test_the_reader_role_reads_only_under_the_callers_own_prefix(self, templates, stack):
        reads = self._reader_statements(templates[stack])
        assert len(reads) == 1 and _actions(reads[0]) == ["secretsmanager:GetSecretValue"]
        (resource,) = _resources(reads[0])
        assert str(resource).endswith(f":secret:/{PREFIX}/connections/${{aws:PrincipalTag/userId}}/*"), resource

    @pytest.mark.parametrize("stack", PROVIDER_STACKS)
    def test_only_the_provider_roles_may_assume_it(self, templates, stack):
        roles = templates[stack].find_resources("AWS::IAM::Role")
        reader = next(r for k, r in roles.items() if k.startswith("SecretReader"))
        trust = reader["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert {a for t in trust for a in _actions(t)} == {"sts:AssumeRole", "sts:TagSession"}
        trusted = set()
        for statement in trust:
            principals = statement["Principal"]["AWS"]
            trusted |= {json.dumps(p) for p in (principals if isinstance(principals, list) else [principals])}
        role_of = {p: _function(templates, p, stack)[1]["Properties"]["Role"]["Fn::GetAtt"][0] for p in PROVIDERS}
        provider_roles = {json.dumps({"Fn::GetAtt": [role, "Arn"]}) for role in role_of.values()}
        assert trusted == provider_roles  # the five providers' roles; not the connections API's, not the account

    @pytest.mark.parametrize("stack", PROVIDER_STACKS)
    def test_the_functions_may_assume_only_that_role(self, templates, stack):
        assumes = [s for s in _statements(templates[stack]) if "sts:AssumeRole" in _actions(s)]
        assert assumes
        for statement in assumes:
            assert sorted(_actions(statement)) == ["sts:AssumeRole", "sts:TagSession"]
            assert all("SecretReader" in json.dumps(r) for r in _resources(statement)), statement


class TestApiFunctionDataAccess:

    def test_it_knows_where_to_report_a_failed_chat(self, templates):
        functions = templates["agent"].find_resources("AWS::Lambda::Function")
        api = next(r for k, r in functions.items() if k.startswith("ApiFunction"))
        (table,) = templates["agent"].find_resources("AWS::DynamoDB::Table")
        assert api["Properties"]["Environment"]["Variables"]["SESSIONS_TABLE"] == {"Ref": table}

    def test_dynamodb_access_names_tables_not_a_prefix(self, templates):
        for s in _statements(templates["agent"]):
            if any(a.startswith("dynamodb:") for a in _actions(s)):
                for r in _resources(s):
                    assert not str(r).endswith(f"table/{PREFIX}-*"), r

    def test_it_reaches_only_the_sessions_table_and_never_scans_it(self, templates):
        # Conversation history and the failed-chat event; every call is a key or a
        # query under the caller's own partition, so no Scan and no GetItem across users.
        grants = [s for s in _role_statements(templates, "ApiFunction")
                  if any(a.startswith("dynamodb:") for a in _actions(s))]
        assert [sorted(_actions(s)) for s in grants] == [sorted([
            "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem"])]
        (table,) = templates["agent"].find_resources("AWS::DynamoDB::Table")
        assert _resources(grants[0]) == [{"Fn::GetAtt": [table, "Arn"]}]


class TestCorsIsDecidedInTheLambda:
    """The gateway has no CORS layer of its own: HTTP APIs answer preflights themselves when
    one is configured, which would bypass the Lambda's origin allowlist entirely."""

    def test_the_http_api_has_no_cors_configuration(self, templates):
        (api,) = templates["agent"].find_resources("AWS::ApiGatewayV2::Api").values()
        assert "CorsConfiguration" not in api["Properties"]

    def test_the_preflight_route_reaches_the_lambda_without_an_authorizer(self, templates):
        routes = templates["agent"].find_resources("AWS::ApiGatewayV2::Route").values()
        options = [r["Properties"] for r in routes if r["Properties"]["RouteKey"].startswith("OPTIONS ")]
        assert options, "a preflight has no Authorization header, so it needs a route of its own"
        for route in options:
            assert route["AuthorizationType"] == "NONE"

    def test_every_other_api_route_requires_the_jwt(self, templates):
        routes = templates["agent"].find_resources("AWS::ApiGatewayV2::Route").values()
        others = [r["Properties"] for r in routes if not r["Properties"]["RouteKey"].startswith("OPTIONS ")]
        assert others
        for route in others:
            assert route["AuthorizationType"] == "JWT", route["RouteKey"]


class TestCdkNag:

    def test_no_stack_has_an_unresolved_aws_solutions_finding(self, stacks):
        from aws_cdk.assertions import Annotations, Match
        found = []
        for name, stack in stacks.items():
            annotations = Annotations.from_stack(stack)
            for level in (annotations.find_error, annotations.find_warning):
                found += [f"{name} {m.id}: {m.entry.data}"[:400]
                          for m in level("*", Match.string_like_regexp("AwsSolutions-"))]
        assert not found, "\n".join(found)


class TestCheckovReasons:
    """Every exemption from a checkov check sits on the resource it exempts, with its reason."""

    @staticmethod
    def _skips(resource: dict) -> dict:
        return {s["id"]: s["comment"] for s in resource.get("Metadata", {}).get("checkov", {}).get("skip", [])}

    def test_every_function_says_where_its_failures_go_and_why_it_is_outside_a_vpc(self, templates):
        for name, template in templates.items():
            for logical_id, fn in template.find_resources("AWS::Lambda::Function").items():
                skips = self._skips(fn)
                assert skips.get("CKV_AWS_116"), f"{name}/{logical_id}"
                if "VpcConfig" not in fn["Properties"]:
                    assert skips.get("CKV_AWS_117"), f"{name}/{logical_id}"
                else:
                    assert "CKV_AWS_117" not in skips, f"{name}/{logical_id}"

    def test_only_cdks_deploy_handlers_carry_the_deploy_handler_reasons(self, templates):
        """A function of ours made without stage.python_function would get them silently; this fails instead."""
        from cdk_infra.shared.checkov import DEPLOY_HANDLER_REASONS
        given = sorted((name, logical_id) for name, template in templates.items()
                       for logical_id, fn in template.find_resources("AWS::Lambda::Function").items()
                       if self._skips(fn).get("CKV_AWS_116") == DEPLOY_HANDLER_REASONS["CKV_AWS_116"])
        handlers = ("CustomS3AutoDeleteObjectsCustomResourceProviderHandler",
                    "ScannerRegistrationProviderframeworkonEvent")
        # Each function named by the handler prefix it starts with, or by its full id if none.
        assert [(name, next((h for h in handlers if logical_id.startswith(h)), logical_id))
                for name, logical_id in given] == [("external", h) for h in handlers], given


    def test_no_function_here_is_exempt_from_keyed_settings(self, templates):
        """Every function of ours, and CDK's provider framework, encrypts its settings with a stack key."""
        for name, template in templates.items():
            for logical_id, fn in template.find_resources("AWS::Lambda::Function").items():
                assert "CKV_AWS_173" not in self._skips(fn), f"{name}/{logical_id}"
                if "Environment" in fn["Properties"]:
                    assert "KmsKeyArn" in fn["Properties"], f"{name}/{logical_id}"


class TestApiGateway:

    def test_the_stage_keeps_an_access_log_of_who_called_which_route(self, templates):
        ((logical_id, stage),) = templates["agent"].find_resources("AWS::ApiGatewayV2::Stage").items()
        # The same logical id as the default stage it replaced, so a deploy updates it in place.
        assert logical_id.startswith("HttpApiDefaultStage"), logical_id
        settings = stage["Properties"]["AccessLogSettings"]
        (log_group,) = [k for k in templates["agent"].find_resources("AWS::Logs::LogGroup")
                        if k.startswith("HttpApiAccessLogs")]
        assert settings["DestinationArn"] == {"Fn::GetAtt": [log_group, "Arn"]}
        assert {"sub", "route", "status", "ip"} <= set(json.loads(settings["Format"])), settings["Format"]

    def test_the_default_stage_is_throttled(self, templates):
        (stage,) = templates["agent"].find_resources("AWS::ApiGatewayV2::Stage").values()
        settings = stage["Properties"]["DefaultRouteSettings"]
        # A few people's chat and admin pages; well under the account's default of 10,000 requests a second.
        assert 0 < settings["ThrottlingRateLimit"] <= 100 and 0 < settings["ThrottlingBurstLimit"] <= 200, settings


class TestCognito:

    def test_quick_may_return_from_quick_dot_aws_and_from_the_console_in_each_us_region(self, templates):
        clients = templates["auth"].find_resources("AWS::Cognito::UserPoolClient")
        quick = next(r for k, r in clients.items() if k.startswith("UserPoolQuickClient"))
        assert sorted(quick["Properties"]["CallbackURLs"]) == sorted([
            "https://quick.aws.com/sn/oauthcallback",
            "https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback",
            "https://us-east-2.quicksight.aws.amazon.com/sn/oauthcallback",
            "https://us-west-2.quicksight.aws.amazon.com/sn/oauthcallback",
        ])

    def test_the_password_flow_that_bypasses_srp_is_off(self, templates):
        for resource in templates["auth"].find_resources("AWS::Cognito::UserPoolClient").values():
            flows = resource["Properties"].get("ExplicitAuthFlows", [])
            assert "ALLOW_USER_PASSWORD_AUTH" not in flows

    def test_self_signup_is_off(self, templates):
        for resource in templates["auth"].find_resources("AWS::Cognito::UserPool").values():
            assert resource["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True


class TestTheIndexIsGovernedByIam:

    def test_new_index_tables_are_given_to_iam_whatever_the_lake_formation_default(self, templates):
        (db,) = templates["glue"].find_resources("AWS::Glue::Database").values()
        assert db["Properties"]["DatabaseInput"]["CreateTableDefaultPermissions"] == [
            {"Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}, "Permissions": ["ALL"]}]


def test_a_teardown_leaves_no_named_resource_behind(templates):
    """A retained resource with a deterministic name blocks the next deploy of the stage:
    the scan schedule group did, after the dev stage was deleted and redeployed."""
    names = ("Name", "TableName", "FunctionName", "RoleName", "BucketName", "GroupName", "UserPoolName", "QueueName",
             "LogGroupName", "AgentRuntimeName")
    for name, template in templates.items():
        for key, resource in template.to_json()["Resources"].items():
            if any(isinstance(resource.get("Properties", {}).get(n), (str, dict)) for n in names):
                assert resource.get("DeletionPolicy", "Delete") != "Retain", f"{name}/{key} is retained on delete"


def test_every_client_whose_tokens_reach_the_gateway_is_allowed_its_scope(templates):
    """The gateway requires its custom scope. Quick calls it with its users' tokens, and the
    chat agent forwards the website user's token, so both clients must be allowed that scope;
    a token issued without it is refused with insufficient_scope before any tool runs."""
    (gateway,) = templates["agent"].find_resources("AWS::BedrockAgentCore::Gateway").values()
    required = gateway["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedScopes"]
    assert required == [f"{PREFIX}-gateway/invoke"]
    (server_id,) = templates["auth"].find_resources("AWS::Cognito::UserPoolResourceServer")
    clients = templates["auth"].find_resources("AWS::Cognito::UserPoolClient")
    assert len(clients) == 2
    for name, client in clients.items():
        scopes = json.dumps(client["Properties"].get("AllowedOAuthScopes", []))
        assert server_id in scopes and "/invoke" in scopes, f"{name} is not allowed the gateway scope"


def test_a_stream_record_the_handler_reports_failed_is_retried_then_kept(templates):
    """The stream processor answers with batchItemFailures. DynamoDB retries those only if the
    mapping says the function reports them; otherwise the batch is checkpointed and a chat
    event the browser never got is dropped. Bounded, so one bad record cannot stall the stream."""
    mappings = templates["agent"].find_resources("AWS::Lambda::EventSourceMapping").values()
    (mapping,) = [m["Properties"] for m in mappings if "SessionsTable" in json.dumps(m["Properties"]["EventSourceArn"])]
    assert mapping["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert mapping["BisectBatchOnFunctionError"] is True
    assert 0 < mapping["MaximumRetryAttempts"] <= 5 and mapping["MaximumRecordAgeInSeconds"] <= 900
    assert "SessionsStreamFailures" in json.dumps(mapping["DestinationConfig"]["OnFailure"]["Destination"])


def test_the_gateway_returns_no_debug_detail_to_callers(templates):
    """DEBUG exception level sends internal error detail back to whoever called the tool."""
    (gateway,) = templates["agent"].find_resources("AWS::BedrockAgentCore::Gateway").values()
    assert "ExceptionLevel" not in gateway["Properties"]
