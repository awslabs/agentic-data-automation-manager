#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The website: S3 + CloudFront, serving the UI that frontend/app.py builds."""

from pathlib import Path

from aws_cdk import (
    Aspects,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
)
from constructs import Construct

from cdk_infra import cfn_nag, checkov, nag

# Why, said once for both checkov and cdk-nag.
WEBSITE_BUCKET_UNLOGGED = ("Read only by this distribution through origin access control and written only by the "
                           "deploy; it holds no user data whose reads need a record.")
ACCESS_LOGS_DELIVERED = ("Access logs are delivered to the DistributionAccessLogs log group by CloudWatch Logs "
                         "delivery; the scanners recognise only the legacy S3 logging setting.")
NO_TLS_MINIMUM = ("Served on its cloudfront.net name, whose default certificate cannot set a minimum TLS version; "
                  "that needs a custom domain and ACM certificate.")
NO_WEB_ACL = ("Serves only the static UI; the API it calls is the HTTP API, not behind this distribution, so a web "
              "ACL here would guard only public files.")


class FrontendStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        # prod keeps its access logs a year, and with the stack; other stages a month.
        production: bool,
        dist_path: str,
        # Every origin the UI calls: Cognito, the HTTP API and AppSync Events (frontend/app.py).
        connect_sources: list[str],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not (Path(dist_path) / "index.html").is_file():
            raise ValueError(f"{dist_path} has no index.html; build the UI before synthesizing this stack.")

        # ── S3 Bucket ────────────────────────────────────────────────────
        # The built UI and nothing else: rebuilt on every deploy, so nothing in it needs keeping.
        website_bucket = s3.Bucket(self, "WebsiteBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        checkov.skip(website_bucket, "CKV_AWS_21", "Holds only the built UI, replaced on every deploy from the "
                     "repository; earlier versions would only take up space.")
        nag.acknowledge(website_bucket, "S1", WEBSITE_BUCKET_UNLOGGED)
        cfn_nag.skip(website_bucket, "W35", WEBSITE_BUCKET_UNLOGGED)
        checkov.skip(website_bucket, "CKV_AWS_18", WEBSITE_BUCKET_UNLOGGED)

        # The page runs only its own scripts and talks only to this deployment's backend.
        # Inline styles are allowed: Mantine, AG Grid and ECharts set them at run time.
        content_security_policy = "; ".join([
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            f"connect-src 'self' {' '.join(connect_sources)}",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ])
        security_headers = cloudfront.ResponseHeadersPolicy(self, "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=content_security_policy, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True),
            ),
        )

        # The stack's own key, for its log groups, as the backend's stacks have.
        # Kept whenever the logs it encrypts are kept, or they could no longer be read.
        key = kms.Key(self, "DataKey", enable_key_rotation=True,
                      removal_policy=RemovalPolicy.RETAIN if production else RemovalPolicy.DESTROY,
                      description=f"{parent_stack_name}-Frontend: its access and deployment logs")
        key.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
            resources=["*"],
            conditions={"ArnLike": {"kms:EncryptionContext:aws:logs:arn":
                                    f"arn:aws:logs:{self.region}:{self.account}:log-group:*"}}))

        # ── CloudFront ───────────────────────────────────────────────────
        distribution = cloudfront.Distribution(self, "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                # Creates the origin access control and the bucket policy that lets
                # only this distribution read. Without LIST, a missing key is a 403,
                # which the fallback below answers with index.html like a 404.
                origin=origins.S3BucketOrigin.with_origin_access_control(website_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=security_headers,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(http_status=404, response_http_status=200,
                    response_page_path="/index.html", ttl=Duration.seconds(0)),
                cloudfront.ErrorResponse(http_status=403, response_http_status=200,
                    response_page_path="/index.html", ttl=Duration.seconds(0)),
            ],
        )
        # Who loaded the site, from where: CloudFront's access logs, kept in a log group under the
        # stack's key rather than in a bucket that would need ACLs enabled.
        access_logs = logs.LogGroup(self, "DistributionAccessLogs", encryption_key=key,
            retention=logs.RetentionDays.ONE_YEAR if production else logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN if production else RemovalPolicy.DESTROY)
        log_source = logs.CfnDeliverySource(self, "DistributionLogSource",
            name=f"{parent_stack_name}-cloudfront", log_type="ACCESS_LOGS",
            resource_arn=f"arn:aws:cloudfront::{self.account}:distribution/{distribution.distribution_id}")
        log_destination = logs.CfnDeliveryDestination(self, "DistributionLogDestination",
            name=f"{parent_stack_name}-cloudfront-logs", destination_resource_arn=access_logs.log_group_arn,
            output_format="json")
        delivery = logs.CfnDelivery(self, "DistributionLogDelivery",
            delivery_source_name=log_source.name, delivery_destination_arn=log_destination.attr_arn)
        delivery.add_dependency(log_source)
        nag.acknowledge(distribution, "CFR3", ACCESS_LOGS_DELIVERED)
        cfn_nag.skip(distribution, "W10", ACCESS_LOGS_DELIVERED)
        checkov.skip(distribution, "CKV_AWS_86", ACCESS_LOGS_DELIVERED)
        nag.acknowledge(distribution, "CFR4", NO_TLS_MINIMUM)
        cfn_nag.skip(distribution, "W70", NO_TLS_MINIMUM)
        nag.acknowledge(distribution, "CFR2", NO_WEB_ACL)
        nag.acknowledge(distribution, "CFR1", "Open worldwide on purpose: once published, the stack is deployed by "
                        "customers anywhere, and the API behind it authorises each user.")
        checkov.skip(distribution, "CKV_AWS_174", NO_TLS_MINIMUM)
        checkov.skip(distribution, "CKV_AWS_68", NO_WEB_ACL)

        # ── Deploy built frontend ────────────────────────────────────────
        # CDK's handlers for the deployment and the bucket's emptying state no reasons of their own.
        Aspects.of(self).add(checkov.DeployHandlerReasons())
        # Vite names each built script and stylesheet by its content hash, so a name never changes
        # meaning: they are cached for a year and never pruned, and a tab still on the previous build
        # can still load its own. Everything else, index.html first, is revalidated on every load,
        # and the edge keeps it at most CachingOptimized's one-second minimum TTL, so a deploy shows
        # at once without an invalidation (which would need cloudfront:CreateInvalidation on every
        # distribution: BucketDeployment cannot scope it to one).
        deploy_logs = logs.LogGroup(self, "DeployWebsiteLogs", encryption_key=key,
                                    retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        assets = s3_deployment.BucketDeployment(self, "DeployAssets",
            sources=[s3_deployment.Source.asset(dist_path)],
            destination_bucket=website_bucket,
            include=["assets/*"], exclude=["*"],
            prune=False,
            log_group=deploy_logs,
            cache_control=[s3_deployment.CacheControl.set_public(),
                           s3_deployment.CacheControl.max_age(Duration.days(365)),
                           s3_deployment.CacheControl.immutable()],
        )
        website = s3_deployment.BucketDeployment(self, "DeployWebsite",
            sources=[s3_deployment.Source.asset(dist_path)],
            destination_bucket=website_bucket,
            # Pruning leaves what it excludes in place: the hashed assets above.
            exclude=["assets/*"],
            log_group=deploy_logs,
            cache_control=[s3_deployment.CacheControl.no_cache()],
        )
        # A new index.html is published only once the files it names are there.
        website.node.add_dependency(assets)

        # ── SSM + Outputs ────────────────────────────────────────────────
        ssm.StringParameter(self, "SSM_origin_frontend",
            parameter_name=f"/{parent_stack_name}/origin_frontend",
            string_value=f"https://{distribution.distribution_domain_name}",
        ).apply_removal_policy(RemovalPolicy.DESTROY)

        CfnOutput(self, "CloudFrontUrl",
            value=f"https://{distribution.distribution_domain_name}",
        )
        CfnOutput(self, "BucketName", value=website_bucket.bucket_name)
