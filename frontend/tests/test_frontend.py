#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The website stack serves a built UI, and the app refuses to deploy without one."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

FRONTEND = Path(__file__).resolve().parents[1]
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from cdk_infra.app_config import connect_sources, stack_prefix  # noqa: E402
from cdk_infra.frontend_stack import FrontendStack  # noqa: E402

ENV = cdk.Environment(account="123456789012", region="us-west-2")
# frontend/cdk.json's context, its feature flags above all, as the cdk CLI gives app.py.
APP_CONTEXT = json.loads((FRONTEND / "cdk.json").read_text())["context"]

# Backend outputs as install_cdk.sh writes them: one entry per stack, with outputs the UI doesn't call.
BACKEND_OUTPUTS = {
    "T-Auth": {"UserPoolId": "us-west-2_EXAMPLE", "UserPoolClientId": "exampleclient", "UserPoolDomainUrl": "https://t.auth.us-west-2.amazoncognito.com/",
               "GatewayScope": "t/invoke"},
    "T-Api": {"HttpApiUrl": "https://a1.execute-api.us-west-2.amazonaws.com/"},
    "T-Agent": {"EventsApiEndpoint": "e1.appsync-api.us-west-2.amazonaws.com",
                "EventsApiRealtimeEndpoint": "e1.appsync-realtime-api.us-west-2.amazonaws.com",
                "RuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/t"},
}


def _stack(dist: Path, production: bool = False) -> FrontendStack:
    return FrontendStack(cdk.App(context=APP_CONTEXT), "T-Frontend", parent_stack_name="T", production=production,
                         dist_path=str(dist), connect_sources=connect_sources(BACKEND_OUTPUTS, "us-west-2"), env=ENV)


def test_the_stack_prefix_raises_only_the_first_letter_of_the_stage():
    assert stack_prefix("devAlice") == "AdamDevAlice"
    assert stack_prefix("dev") == "AdamDev"


def _headers(tmp_path) -> dict:
    (tmp_path / "index.html").write_text("<html></html>")
    template = Template.from_stack(_stack(tmp_path))
    (dist,) = template.find_resources("AWS::CloudFront::Distribution").values()
    policy_ref = dist["Properties"]["DistributionConfig"]["DefaultCacheBehavior"]["ResponseHeadersPolicyId"]["Ref"]
    return template.to_json()["Resources"][policy_ref]["Properties"]["ResponseHeadersPolicyConfig"][
        "SecurityHeadersConfig"]


def test_the_page_may_call_only_this_deployments_backend(tmp_path):
    policy = _headers(tmp_path)["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    directives = dict(d.split(" ", 1) for d in policy.split("; "))
    assert directives["connect-src"].split() == [
        "'self'", "https://t.auth.us-west-2.amazoncognito.com", "https://cognito-idp.us-west-2.amazonaws.com",
        "https://a1.execute-api.us-west-2.amazonaws.com", "https://e1.appsync-api.us-west-2.amazonaws.com",
        "wss://e1.appsync-realtime-api.us-west-2.amazonaws.com"]
    assert directives["script-src"] == "'self'" and directives["default-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'" and directives["object-src"] == "'none'"


def test_browsers_are_told_https_only_and_no_framing(tmp_path):
    headers = _headers(tmp_path)
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31536000
    assert headers["FrameOptions"]["FrameOption"] == "DENY"
    assert headers["ContentTypeOptions"]["Override"] is True


def test_the_bucket_refuses_requests_without_tls(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (policy,) = Template.from_stack(_stack(tmp_path)).find_resources("AWS::S3::BucketPolicy").values()
    denies = [st for st in policy["Properties"]["PolicyDocument"]["Statement"] if st["Effect"] == "Deny"]
    assert any(st["Condition"] == {"Bool": {"aws:SecureTransport": "false"}} for st in denies)


def _deployments(template):
    """The two deployments, by what they publish: the hashed assets, and everything else."""
    found = template.find_resources("Custom::CDKBucketDeployment")
    assets = {k: v for k, v in found.items() if k.startswith("DeployAssets")}
    pages = {k: v for k, v in found.items() if k.startswith("DeployWebsite")}
    assert len(assets) == len(pages) == 1 and len(found) == 2, list(found)
    return next(iter(assets.items())), next(iter(pages.items()))


def test_pages_are_revalidated_on_every_load_and_hashed_assets_are_cached_for_a_year(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (_, assets), (_, pages) = _deployments(Template.from_stack(_stack(tmp_path)))
    assert pages["Properties"]["SystemMetadata"]["cache-control"] == "no-cache"
    assert pages["Properties"]["Exclude"] == ["assets/*"] and pages["Properties"].get("Prune", True) is True
    assert assets["Properties"]["SystemMetadata"]["cache-control"] == "public, max-age=31536000, immutable"
    assert assets["Properties"]["Include"] == ["assets/*"] and assets["Properties"]["Prune"] is False


def test_a_page_is_published_only_after_the_assets_it_names(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (assets_id, _), (_, pages) = _deployments(Template.from_stack(_stack(tmp_path)))
    assert assets_id in pages.get("DependsOn", []), pages.get("DependsOn")


def test_the_deploy_holds_no_cloudfront_permission(tmp_path):
    """It does not invalidate: CDK can grant that only on every distribution in the account."""
    (tmp_path / "index.html").write_text("<html></html>")
    template = Template.from_stack(_stack(tmp_path))
    for deployment in template.find_resources("Custom::CDKBucketDeployment").values():
        assert "DistributionId" not in deployment["Properties"]
    actions = [a for policy in template.find_resources("AWS::IAM::Policy").values()
               for st in policy["Properties"]["PolicyDocument"]["Statement"]
               for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])]
    assert not [a for a in actions if a.startswith("cloudfront:")], actions


def test_only_cdks_deploy_handlers_carry_the_deploy_handler_reasons(tmp_path):
    from cdk_infra.checkov import DEPLOY_HANDLER_REASONS
    (tmp_path / "index.html").write_text("<html></html>")
    given = sorted(k for k, fn in Template.from_stack(_stack(tmp_path)).find_resources("AWS::Lambda::Function").items()
                   if {s["id"]: s["comment"] for s in fn.get("Metadata", {}).get("checkov", {}).get("skip", [])}
                   .get("CKV_AWS_116") == DEPLOY_HANDLER_REASONS["CKV_AWS_116"])
    handlers = ("CustomCDKBucketDeployment", "CustomS3AutoDeleteObjectsCustomResourceProviderHandler")
    assert [next((h for h in handlers if k.startswith(h)), k) for k in given] == list(handlers), given


def test_without_a_built_ui_there_is_no_stack(tmp_path):
    with pytest.raises(ValueError, match="index.html"):
        _stack(tmp_path)


def test_without_this_stages_backend_outputs_the_app_stops_before_building(tmp_path):
    env = {**os.environ, "CDK_DEFAULT_REGION": "us-west-2", "CDK_OUTDIR": str(tmp_path),
           "CDK_CONTEXT_JSON": '{"stage": "nosuchstage"}'}
    result = subprocess.run([sys.executable, "app.py"], cwd=FRONTEND, env=env,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode != 0
    assert "no outputs for AdamNosuchstage" in result.stderr


def test_without_a_stage_the_app_stops_instead_of_building_for_dev(tmp_path):
    env = {**os.environ, "CDK_DEFAULT_REGION": "us-west-2", "CDK_OUTDIR": str(tmp_path), "CDK_CONTEXT_JSON": "{}"}
    result = subprocess.run([sys.executable, "app.py"], cwd=FRONTEND, env=env,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode != 0
    assert "No stage given" in result.stderr


def test_only_this_distribution_can_read_the_bucket_through_origin_access_control(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    template = Template.from_stack(_stack(tmp_path))
    template.resource_count_is("AWS::CloudFront::CloudFrontOriginAccessIdentity", 0)
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    (dist,) = template.find_resources("AWS::CloudFront::Distribution").values()
    (origin,) = dist["Properties"]["DistributionConfig"]["Origins"]
    assert "OriginAccessControlId" in origin
    (policy,) = template.find_resources("AWS::S3::BucketPolicy").values()
    reads = [st for st in policy["Properties"]["PolicyDocument"]["Statement"]
             if st.get("Principal") == {"Service": "cloudfront.amazonaws.com"}]
    assert reads and all("AWS:SourceArn" in st["Condition"]["StringEquals"] for st in reads)


def _run_build_script(tmp_path, outputs: dict) -> subprocess.CompletedProcess:
    """The UI build script, run on a copy (the real src/aws-exports.ts is left alone) with an npm that does nothing."""
    ui = FRONTEND / "src" / "adam_ui"
    work = tmp_path / "ui"
    (work / "src").mkdir(parents=True)
    for name in ("build_website_deployment.sh", "aws-exports.ts.template"):
        (work / name).write_text((ui / name).read_text())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "npm").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "npm").chmod(0o755)
    outputs_file = tmp_path / "outputs.json"
    outputs_file.write_text(json.dumps(outputs))
    env = {**os.environ, "OUTPUTS_FILE": str(outputs_file), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["bash", str(work / "build_website_deployment.sh")], env=env,
                          capture_output=True, text=True, timeout=60, check=False)


def _without(output: str) -> dict:
    return {stack: {k: v for k, v in values.items() if k != output} for stack, values in BACKEND_OUTPUTS.items()}


def test_the_ui_config_carries_every_backend_output_it_needs(tmp_path):
    result = _run_build_script(tmp_path, BACKEND_OUTPUTS)
    assert result.returncode == 0, result.stderr
    config = (tmp_path / "ui" / "src" / "aws-exports.ts").read_text()
    assert not re.search(r"<[A-Z_]+>", config)
    assert 'region: "us-west-2"' in config
    assert 'eventsHttpEndpoint: "e1.appsync-api.us-west-2.amazonaws.com"' in config


@pytest.mark.parametrize("output", ["UserPoolId", "UserPoolClientId", "UserPoolDomainUrl", "GatewayScope",
                                    "HttpApiUrl", "EventsApiEndpoint"])
def test_a_missing_backend_output_stops_the_build_instead_of_shipping_a_blank(tmp_path, output):
    result = _run_build_script(tmp_path, _without(output))
    assert result.returncode != 0
    assert f"lacks {output}" in result.stderr
    assert not (tmp_path / "ui" / "src" / "aws-exports.ts").exists()


def test_without_the_agent_runtime_the_ui_is_built_with_chat_off(tmp_path):
    result = _run_build_script(tmp_path, _without("RuntimeArn"))
    assert result.returncode == 0, result.stderr
    assert 'agentRuntimeArn: ""' in (tmp_path / "ui" / "src" / "aws-exports.ts").read_text()


def test_only_the_deployment_handler_is_exempt_from_keyed_settings(tmp_path):
    """CDK's BucketDeployment exposes no key for its handler's settings; nothing of ours is exempt."""
    from cdk_infra.checkov import UNKEYED_HANDLER_SETTINGS
    (tmp_path / "index.html").write_text("<html></html>")
    exempt = [k for k, fn in Template.from_stack(_stack(tmp_path)).find_resources("AWS::Lambda::Function").items()
              if {s["id"]: s["comment"] for s in fn.get("Metadata", {}).get("checkov", {}).get("skip", [])}
              .get("CKV_AWS_173") == UNKEYED_HANDLER_SETTINGS]
    assert len(exempt) == 1 and exempt[0].startswith("CustomCDKBucketDeployment"), exempt


def _keyed_stack(tmp_path, production):
    (tmp_path / "index.html").write_text("<html></html>")
    return Template.from_stack(_stack(tmp_path, production))


def test_the_distributions_access_logs_go_to_a_log_group_under_the_stacks_key(tmp_path):
    template = _keyed_stack(tmp_path, production=False)
    (source,) = template.find_resources("AWS::Logs::DeliverySource").values()
    assert source["Properties"]["LogType"] == "ACCESS_LOGS"
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution")
    assert distribution in json.dumps(source["Properties"]["ResourceArn"])
    (destination,) = template.find_resources("AWS::Logs::DeliveryDestination").values()
    (log_group_id,) = [k for k in template.find_resources("AWS::Logs::LogGroup")
                       if k.startswith("DistributionAccessLogs")]
    assert destination["Properties"]["DestinationResourceArn"] == {"Fn::GetAtt": [log_group_id, "Arn"]}
    (key,) = template.find_resources("AWS::KMS::Key")
    log_group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert log_group["Properties"]["KmsKeyId"] == {"Fn::GetAtt": [key, "Arn"]}
    assert template.find_resources("AWS::Logs::Delivery")


@pytest.mark.parametrize(("production", "kept", "days"), [(True, "Retain", 365), (False, "Delete", 30)])
def test_prod_keeps_its_access_logs_and_the_key_that_reads_them(tmp_path, production, kept, days):
    template = _keyed_stack(tmp_path, production)
    (log_group,) = [v for k, v in template.find_resources("AWS::Logs::LogGroup").items()
                    if k.startswith("DistributionAccessLogs")]
    (key,) = template.find_resources("AWS::KMS::Key").values()
    assert log_group["DeletionPolicy"] == key["DeletionPolicy"] == kept
    assert log_group["Properties"]["RetentionInDays"] == days


def test_no_unresolved_aws_solutions_finding(tmp_path):
    """Checked as app.py checks the stack: every cdk-nag finding is fixed or acknowledged with its reason."""
    from aws_cdk.assertions import Annotations, Match
    from cdk_nag import AwsSolutionsChecks

    from cdk_infra import nag
    (tmp_path / "index.html").write_text("<html></html>")
    stack = _stack(tmp_path)
    cdk.Aspects.of(stack.node.root).add(AwsSolutionsChecks())
    nag.acknowledge_app_wide(stack.node.root)
    annotations = Annotations.from_stack(stack)
    found = [f"{m.id}: {m.entry.data}"[:300] for level in (annotations.find_error, annotations.find_warning)
             for m in level("*", Match.string_like_regexp("AwsSolutions-"))]
    assert not found, "\n".join(found)

