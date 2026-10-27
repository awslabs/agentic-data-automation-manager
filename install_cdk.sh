#!/bin/bash
#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
#
# Deploys the stack into an existing VPC, with your own databases. The network
# comes from ./deploy.env (copy deploy.env.example); anything missing is asked
# for and saved. With no flags, deploys the backend and then the frontend.
#
#   -b   backend stacks only
#   -f   frontend only (needs the outputs file from an earlier backend deploy)
#   -s   stage for this run, overriding STAGE below
#   -y   no IAM approval prompts (cdk --require-approval never)
#   -h   cdk --hotswap-fallback. Development only: it introduces drift.
#   -n   cdk --no-rollback
#   -v   cdk --verbose
#
# Images are built with CDK_DOCKER if set, else docker (CDK_DOCKER=finch for Finch).

# The stage becomes the prefix Adam<Stage>, which names the stacks, prefixes
# resource names and SSM paths (/Adam<Stage>/...), and picks the outputs file
# frontend/backend_outputs_<stage>.json, so stages deploy side by side.
#
# dev tears down clean: deleting its stacks deletes its tables, users and logs, and it
# accepts sign-in from the local frontend dev server.
STAGE=dev
# prod keeps users' data: point-in-time recovery (backups) and deletion protection on the
# tables, deletion protection on the user pool, a year of logs, and all of it kept when a
# stack is deleted. No local dev server sign-in.
# STAGE=prod

# Every path below is the repository's, wherever the script is run from.
cd "$(dirname "$0")" || exit 1

# Every run's output also goes to logs/ (git-ignored), for troubleshooting after
# the terminal has scrolled. Exit codes are unaffected.
mkdir -p logs
exec > >(tee -a "logs/install-$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

echo Started install at `date`
export SECONDS=0
# CDK flags accumulate into an array and are expanded as "${cdk_flags[@]}".
# Never build them as a single string and never run them through `eval`:
# `eval` returns its own exit status, so a failed `cdk deploy` looks like a
# success to the `if [ $? -ne 0 ]` checks below, and it swallows the output
# these deploys need to show.
cdk_flags=()
b=0
f=0
stage=''

while getopts "nyhfbvs:" opt; do
  case ${opt} in
    y ) cdk_flags+=(--require-approval never) ;;
    h ) cdk_flags+=(--hotswap-fallback) ;;
    n ) cdk_flags+=(--no-rollback) ;;
    v ) cdk_flags+=(--verbose) ;;
    f ) f=1 ;;
    b ) b=1 ;;
    s ) stage=$OPTARG ;;
    * ) exit 1 ;;
  esac
done

# Neither -b nor -f means both.
if [ $b -eq 0 ] && [ $f -eq 0 ]; then
  b=1
  f=1
fi

if [ ! -f .venv/bin/activate ]; then
  uv venv
fi
source .venv/bin/activate
# Every run: a fresh clone has none of these, and a pulled change may add one.
uv pip install --quiet -r backend/requirements.txt -r frontend/requirements.txt || {
  echo "ERROR: could not install the CDK app's requirements into .venv."; exit 1; }

DEPLOY_ENV="${DEPLOY_ENV:-./deploy.env}"
if [ -f "$DEPLOY_ENV" ]; then
  set -a; source "$DEPLOY_ENV"; set +a
fi
# Anything deploy.env lacks is asked for, and saved, so the next run asks nothing.
required=(DEPLOY_PROFILE DEPLOY_REGION VPC_ID APP_SG_ID PRIVATE_SUBNET_IDS AVAILABILITY_ZONES VPC_CIDR_BLOCK)
for v in "${required[@]}"; do
  if [ -z "${!v:-}" ]; then
    if [ ! -t 0 ]; then
      echo "ERROR: $v is not set in $DEPLOY_ENV (see deploy.env.example)."
      exit 1
    fi
    read -r -p "$v (see deploy.env.example): " value
    if [ -z "$value" ]; then
      echo "ERROR: $v is required."
      exit 1
    fi
    printf -v "$v" '%s' "$value"
    # Quoted for the shell that sources this file next time: a value is data, never a command.
    printf '%s=%q\n' "$v" "$value" >> "$DEPLOY_ENV"
  fi
done
stage="${stage:-$STAGE}"
# Letters and digits only: the prefix must also be a valid Cognito domain, Glue
# database, and AgentCore Gateway name.
if [[ ! "$stage" =~ ^[A-Za-z][A-Za-z0-9]*$ ]]; then
  echo "ERROR: stage '$stage' must start with a letter and contain only letters and digits."
  exit 1
fi
profile_args=(--profile "$DEPLOY_PROFILE")
REGION="$DEPLOY_REGION"
# The backend's outputs, which frontend/app.py builds the UI from.
# One file per stage, so deploying one stage never builds the UI from another's outputs.
outputs_file="backend_outputs_${stage}.json"
context=(
  -c "stage=${stage}"
  -c "vpcId=${VPC_ID}"
  -c "appSgId=${APP_SG_ID}"
  -c "privateSubnetIds=${PRIVATE_SUBNET_IDS}"
  -c "availabilityZones=${AVAILABILITY_ZONES}"
  -c "vpcCidrBlock=${VPC_CIDR_BLOCK}"
)
if [ -n "${DYNAMODB_ENDPOINT_URL:-}" ]; then
  context+=(-c "dynamodbEndpointUrl=${DYNAMODB_ENDPOINT_URL}")
fi
# The web console's chat page needs the agent runtime; Quick does not. app.py refuses
# anything but true or false.
if [ -n "${ENABLE_CHAT:-}" ]; then
  context+=(-c "enableChat=${ENABLE_CHAT}")
fi
# web_search, Amazon Nova web grounding. Off unless true: the agent's query leaves the VPC for a
# web search. app.py refuses anything but true or false.
if [ -n "${ENABLE_WEB_SEARCH:-}" ]; then
  context+=(-c "enableWebSearch=${ENABLE_WEB_SEARCH}")
fi
# The Redshift Serverless workgroup the Redshift provider's integration tests use.
if [ -n "${REDSHIFT_TEST_TARGET:-}" ]; then
  context+=(-c "redshiftTestTarget=${REDSHIFT_TEST_TARGET}")
fi

ACCOUNT_ID=$(aws sts get-caller-identity "${profile_args[@]}" --query Account --output text 2>/dev/null)
if [ -z "$ACCOUNT_ID" ]; then
  echo "ERROR: cannot resolve the AWS account. Check the credentials for profile $DEPLOY_PROFILE."
  exit 1
fi
# app.py reads CDK_DEFAULT_ACCOUNT/REGION to resolve env={account, region}.
export CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID"
export CDK_DEFAULT_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"
# app.py reads the VPC's existing endpoints with these credentials at synth.
export AWS_PROFILE="$DEPLOY_PROFILE"
prefix="Adam$(tr '[:lower:]' '[:upper:]' <<< "${stage:0:1}")${stage:1}"
echo "Deploying $prefix to account $ACCOUNT_ID in $REGION, VPC $VPC_ID"

# The website's URL is the web client's sign-in callback. The website stack publishes it
# in SSM, so it comes from the account, whichever checkout deploys. Only a first install,
# before the website stack exists, has none; the Auth stack gets it once the website is up.
website_deployed() {
  local err
  if err=$(aws cloudformation describe-stacks --stack-name "${prefix}-Frontend" \
             "${profile_args[@]}" --region "$REGION" 2>&1 >/dev/null); then
    return 0
  fi
  if grep -q "does not exist" <<< "$err"; then
    return 1
  fi
  echo "ERROR: cannot tell whether ${prefix}-Frontend is deployed: $err" >&2
  exit 1
}
website_url_from_ssm() {
  local url
  url=$(aws ssm get-parameter --name "/${prefix}/origin_frontend" --query Parameter.Value \
          --output text "${profile_args[@]}" --region "$REGION") || {
    echo "ERROR: ${prefix}-Frontend is deployed but /${prefix}/origin_frontend can't be read." >&2
    exit 1
  }
  if [ -z "$url" ] || [ "$url" = "None" ]; then
    echo "ERROR: /${prefix}/origin_frontend is empty; the web client would lose its sign-in callback." >&2
    exit 1
  fi
  echo "$url"
}
website_url=""
if website_deployed; then
  website_url=$(website_url_from_ssm) || exit 1
  context+=(-c "websiteUrl=${website_url}")
fi

cd backend || exit 1

if [ $b -eq 1 ]; then
  # The same runtime CDK builds images with: CDK_DOCKER if set, else docker. A
  # shell alias (docker=finch) is invisible to scripts, so Finch users set CDK_DOCKER=finch.
  runtime="${CDK_DOCKER:-docker}"
  if ! command -v "$runtime" >/dev/null 2>&1; then
    echo "ERROR: container runtime '$runtime' not found. Install Docker, or set CDK_DOCKER"
    echo "       to the one you use (for example CDK_DOCKER=finch)."
    exit 1
  fi
  # finch's in-VM nerdctl reaches credentials only through the host credential
  # daemon. If it is dead, `finch login` still prints "Login Succeeded" but the
  # image push fails minutes later. Fail here instead of after a full image build.
  if [ "$(basename "$runtime")" = "finch" ] && ! pgrep -f finch-cred/credserver >/dev/null 2>&1; then
    echo "ERROR: finch credential daemon (credserver) is not running."
    echo "       ECR pushes will fail. Fix with: finch vm stop && finch vm start"
    exit 1
  fi
  ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  aws ecr get-login-password --region "$REGION" "${profile_args[@]}" \
    | "$runtime" login --username AWS --password-stdin "$ECR_REGISTRY" 2>/dev/null \
    && echo "  ECR login OK: $ECR_REGISTRY" \
    || echo "  Warning: ECR login failed. Image builds may fail."

  stacks=()
  list_errors=$(mktemp)
  while IFS= read -r s; do
    [ -n "$s" ] && stacks+=("$s")
  done < <(cdk list "${profile_args[@]}" "${context[@]}" 2>"$list_errors")
  if [ ${#stacks[@]} -eq 0 ]; then
    # Synth refuses existing VPC endpoints that would deploy and not work; this shows why.
    cat "$list_errors"
    rm -f "$list_errors"
    echo "ERROR: cdk list returned no stacks."
    exit 1
  fi
  rm -f "$list_errors"
  echo
  echo "Deploying backend stacks: ${stacks[*]}"
  echo "It takes a while the first time through."
  echo
  echo '{}' > "../frontend/$outputs_file"
  cdk deploy "${stacks[@]}" \
    "${profile_args[@]}" \
    --outputs-file "../frontend/$outputs_file" \
    "${context[@]}" \
    "${cdk_flags[@]}"
  if [ $? -ne 0 ]; then
    echo "Backend deploy failed. Exiting."
    exit 1
  fi
fi

if [ $f -eq 1 ]; then
  if [ ! -s "../frontend/$outputs_file" ] || [ "$(cat "../frontend/$outputs_file")" = "{}" ]; then
    echo "ERROR: frontend/$outputs_file has no backend outputs. Deploy the backend first (-b)."
    exit 1
  fi
  echo
  echo "Deploying ${prefix}-Frontend. The first CloudFront distribution takes about 15 minutes."
  echo
  # The website is its own CDK app in frontend/; it needs only the stage. Its
  # outputs go to their own file, so the backend outputs it reads stay intact.
  (cd ../frontend && cdk deploy "${prefix}-Frontend" \
    "${profile_args[@]}" \
    --outputs-file "${outputs_file%.json}.frontend.json" \
    -c "stage=${stage}" \
    "${cdk_flags[@]}")
  if [ $? -ne 0 ]; then
    echo "Frontend deploy failed. Exiting."
    exit 1
  fi
  # A new website URL (a first install) becomes the web client's sign-in callback.
  deployed_url=$(website_url_from_ssm) || exit 1
  if [ "$deployed_url" != "$website_url" ]; then
    echo
    echo "Adding $deployed_url to the web client's sign-in callbacks (${prefix}-Auth)."
    echo
    cdk deploy "${prefix}-Auth" --exclusively \
      "${profile_args[@]}" \
      "${context[@]}" -c "websiteUrl=${deployed_url}" \
      "${cdk_flags[@]}"
    if [ $? -ne 0 ]; then
      echo "Auth update failed. Exiting."
      exit 1
    fi
  fi
fi

cd .. || exit 1
echo
echo Installation complete!
echo elapsed time: $(($SECONDS / 60)) minutes and $(($SECONDS % 60)) seconds
echo
