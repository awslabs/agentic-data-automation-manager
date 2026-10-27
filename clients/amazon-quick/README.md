# Amazon Quick

Connect Amazon Quick to the service as an MCP integration. Quick then calls the gateway's tools,
`search_schema` and `execute_statement` (and `web_search`, when the deployment turns it on with
`ENABLE_WEB_SEARCH=true`), through AgentCore Gateway, signed in as each Quick user.

## Before you start

- **The stack is deployed** (`./install_cdk.sh`), and its outputs are in
  `frontend/backend_outputs_<stage>.json`.
- **Each Quick user has a user in the stack's Cognito user pool** (`UserPoolId`, Auth stack).
  Self sign-up is off: an administrator creates the users, and Cognito emails each one a link
  to the website and a temporary password.
- **Each user has added their own database logins** on the website's Connections page, one per
  registered data source they use. The agent can reach only data sources the user has a login
  for, and sees only what that login can see.

## Add the integration (administrator)

In Amazon Quick, add an MCP integration with these values. All of them are stack outputs.

| Quick field | Value |
|---|---|
| Authentication type | Custom user based OAuth |
| Base URL | `QuickMcpServerUrl` (Agent stack) |
| Client ID | `QuickClientId` (Auth stack) |
| Client secret | The output of the command in `QuickClientSecretCommand` (Auth stack) |
| Token URL | `QuickTokenUrl` (Auth stack) |
| Authorization URL | `QuickAuthorizationUrl` (Auth stack) |

The client secret is not a stack output, because anyone who can describe the stack can read its
outputs. The command reads it from Cognito:

```bash
aws cognito-idp describe-user-pool-client --profile <profile> --region <region> \
  --user-pool-id <UserPoolId> --client-id <QuickClientId> \
  --query UserPoolClient.ClientSecret --output text
```

To print every output of a stage:

```bash
aws cloudformation describe-stacks --profile <profile> --region <region> \
  --stack-name Adam<Stage>-Auth \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output text
```

Nothing else needs typing in Quick:

- **Scope.** The Gateway requires the custom scope `Adam<Stage>-gateway/invoke`
  (output `GatewayScope`). The Quick client is allowed it, so the tokens Quick receives carry it.
- **Callback URL.** Quick shows its redirect URL when you save the integration. The Quick client
  already allows `https://quick.aws.com/sn/oauthcallback` and the console's
  `https://<region>.quicksight.aws.amazon.com/sn/oauthcallback` for us-east-1, us-east-2 and
  us-west-2.

## Sign in (each user)

The first time a user uses the integration, Quick sends them to the stack's Cognito sign-in page.
They sign in with their own Cognito user, once. From then on, every tool call runs as that user:
the Gateway verifies their token, and each statement runs as their own database login.

## If something fails

| Symptom | Cause |
|---|---|
| The sign-in page says the user doesn't exist | The integration points at another stack's Cognito client or domain, often one from before a redeploy. Delete the integration and add it again with this stage's outputs. |
| A tool call fails with `insufficient_scope` | The token lacks the Gateway's scope: the integration uses a client that isn't allowed it. Check the client ID against `QuickClientId`. |
| The agent finds no tables | The user has no database logins on the Connections page, or their logins can see none of the indexed tables. |
| Anything else | The Gateway logs each decision (`insufficient_scope`, a missing token, every tool call) to the Agent stack's gateway log group, and the dispatcher and provider functions log the user, tool, statement and row count. |
