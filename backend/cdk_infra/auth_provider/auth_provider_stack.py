#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CDK stack for the Auth Provider microservice.

Independent stack. Deploys:
  - Cognito User Pool
  - Invitation message trigger Lambda (links invitations to the website)
  - Pre token generation trigger Lambda (puts the user's email in their access token)
  - Gateway resource server + Quick MCP client
  - The admin group
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from cdk_infra.shared import nag, paths
from cdk_infra.shared.bundling import bundling_options, copy_files
from cdk_infra.shared.stage import SYNCHRONOUS, Stage

# Regions Quick is launched from in the console. us-west-1 is left out: its
# Quick support is limited.
QUICK_CONSOLE_REGIONS = ("us-east-1", "us-east-2", "us-west-2")


class AuthProviderStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        stage: Stage,
        website_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Cognito ──────────────────────────────────────────────────────
        invitation_fn = stage.python_function(self, "InvitationMessageTrigger",
            failures=SYNCHRONOUS.format("Cognito, as its custom message trigger"),
            outside_vpc="Reads one SSM parameter to write the invitation email; reaches nothing in the VPC.",
            handler="invitation_message_trigger.handler",
            code=lambda_.Code.from_asset(paths.AUTH_PROVIDER,
                exclude=["**/__pycache__"],
                bundling=bundling_options([copy_files("", ["invitation_message_trigger.py"], "")])),
            timeout=Duration.seconds(10),
            memory_size=128,
            environment={"STACK_NAME": parent_stack_name},
        )
        # The website's address, published by the website stack.
        invitation_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{parent_stack_name}/origin_frontend"],
        ))

        # Users sign in with their email, so their username is a generated id. The admin routes
        # record who made each change from the access token, which this gives the email.
        claims_fn = stage.python_function(self, "AccessTokenClaimsTrigger",
            failures=SYNCHRONOUS.format("Cognito, as its pre token generation trigger"),
            outside_vpc="Adds a claim to the token Cognito is issuing; calls nothing.",
            handler="access_token_claims_trigger.handler",
            code=lambda_.Code.from_asset(paths.AUTH_PROVIDER,
                exclude=["**/__pycache__"],
                bundling=bundling_options([copy_files("", ["access_token_claims_trigger.py"], "")])),
            timeout=Duration.seconds(5),
            memory_size=128,
        )

        self.user_pool = cognito.UserPool(self, "UserPool",
            removal_policy=stage.data_removal_policy,
            deletion_protection=stage.production,
            self_sign_up_enabled=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                require_digits=True, require_lowercase=True,
                require_symbols=True, require_uppercase=True, min_length=8),
            lambda_triggers=cognito.UserPoolTriggers(custom_message=invitation_fn),
            # Changing an access token's claims (claims_fn, a V2 trigger) needs Essentials or Plus.
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
        )
        self.user_pool.add_trigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG, claims_fn,
                                   cognito.LambdaVersion.V2_0)
        nag.acknowledge(self.user_pool, "COG2", "MFA is the deployer's choice, made in Cognito for their users; "
                        "sign-in is invitation only (no self sign-up), and the databases authorise every query.")
        nag.acknowledge(self.user_pool, "COG8", "Cognito's Plus plan (threat protection) is a per-user cost left "
                        "to the deployer to turn on; the stack does not choose it for them.")

        self.user_pool_domain = self.user_pool.add_domain("CognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=parent_stack_name.lower(),
            ),
        )

        # ── Resource Server (custom scopes for gateway) ─────────────────
        gateway_scope = cognito.ResourceServerScope(
            scope_name="invoke",
            scope_description="Invoke MCP gateway",
        )
        self.gateway_resource_server = self.user_pool.add_resource_server(
            "GatewayResourceServer",
            identifier=f"{parent_stack_name}-gateway",
            scopes=[gateway_scope],
        )

        # The website is a public client (no secret): authorization code with PKCE. Its
        # users' tokens carry the gateway scope because the chat agent forwards them to
        # the gateway, which requires it. The website's own URL is a callback once the
        # website is deployed (install_cdk.sh passes it); the local dev server is one in
        # every stage but prod.
        website_callbacks = [f"{website_url}/"] if website_url else []
        self.user_pool_client = self.user_pool.add_client("WebClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                    cognito.OAuthScope.resource_server(self.gateway_resource_server, gateway_scope),
                ],
                callback_urls=[*website_callbacks, *stage.local_frontend_urls],
                logout_urls=[*website_callbacks, *stage.local_frontend_urls],
            ),
        )

        # ── Quick MCP Client (confidential, auth code + PKCE) ───────────
        self.quick_client = self.user_pool.add_client("QuickClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            generate_secret=True,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                # Quick is configured with only a client id and secret -- it is
                # never given a scope list, so it derives one from the pool's
                # discovery document. Cognito advertises
                # scopes_supported: [openid, email, phone, profile] pool-wide
                # regardless of per-client config, so every one of those must be
                # allowed here or the authorize call fails with invalid_scope
                # *after* the user has already authenticated (the error arrives
                # on the redirect, which makes it look like a gateway problem).
                # The gateway requires the resource-server scope below.
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PHONE,
                    cognito.OAuthScope.PROFILE,
                    cognito.OAuthScope.resource_server(
                        self.gateway_resource_server, gateway_scope),
                ],
                callback_urls=[
                    # Quick opened without an AWS account.
                    "https://quick.aws.com/sn/oauthcallback",
                    # Quick opened from the AWS console redirects to the console's
                    # regional host. Same path as above; the host varies by region.
                    *[f"https://{region}.quicksight.aws.amazon.com/sn/oauthcallback"
                      for region in QUICK_CONSOLE_REGIONS],
                ],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
        )

        # ── Admin Group ─────────────────────────────────────────────────
        # Application authorization only: who may register databases and run
        # schema scans. What any user may *see* is decided by their database.
        cognito.CfnUserPoolGroup(self, "Group_admin",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="admin",
            description="Registers database connections and runs schema scans",
        )

        # ── Outputs ─────────────────────────────────────────────────────
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "QuickClientId", value=self.quick_client.user_pool_client_id)
        # The rest of what Quick's MCP integration form asks for. The client secret
        # is not an output: outputs are readable by anyone who can describe the stack.
        CfnOutput(self, "QuickTokenUrl", value=f"{self.user_pool_domain.base_url()}/oauth2/token",
                  description="Amazon Quick MCP integration: token URL")
        CfnOutput(self, "QuickAuthorizationUrl",
                  value=f"{self.user_pool_domain.base_url()}/oauth2/authorize",
                  description="Amazon Quick MCP integration: authorization URL")
        CfnOutput(self, "QuickClientSecretCommand",
                  value=(f"aws cognito-idp describe-user-pool-client --region {self.region} "
                         f"--user-pool-id {self.user_pool.user_pool_id} "
                         f"--client-id {self.quick_client.user_pool_client_id} "
                         "--query UserPoolClient.ClientSecret --output text"),
                  description="Amazon Quick MCP integration: prints the client secret")
        CfnOutput(self, "UserPoolDomainUrl", value=self.user_pool_domain.base_url())
        CfnOutput(self, "GatewayScope", value=f"{parent_stack_name}-gateway/invoke")

