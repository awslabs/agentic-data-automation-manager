#!/bin/bash
#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
#
# Called by frontend/app.py to build the Vite app for deployment.
# Reads backend outputs JSON, generates aws-exports.ts, builds the Vite app.
#
# Supports two formats:
#   1. Monolithic: {"BackendStack": {"UserPoolId": "...", ...}}
#   2. Multi-stack: {"Stack-Auth": {"UserPoolId": "..."}, "Stack-Agent": {"HttpApiUrl": "..."}, ...}
#
# OUTPUTS_FILE (required) is the stage's backend outputs file; frontend/app.py sets it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -z "${OUTPUTS_FILE:-}" ]; then
  echo "ERROR: set OUTPUTS_FILE to the stage's backend outputs file (frontend/backend_outputs_<stage>.json)."
  exit 1
fi

if [ ! -f "$OUTPUTS_FILE" ]; then
  echo "ERROR: $OUTPUTS_FILE not found. Deploy backend first."
  exit 1
fi

echo "Reading outputs from $OUTPUTS_FILE"

# Flatten all stack outputs into a single dict, then extract values. The path is an
# argument rather than part of the program, so no file name can change what it runs.
python3 - "$OUTPUTS_FILE" <<'PY'
import json, sys

with open(sys.argv[1]) as f:
    raw = json.load(f)

# Flatten: merge all stack outputs into one dict
flat = {}
for stack_outputs in raw.values():
    if isinstance(stack_outputs, dict):
        flat.update(stack_outputs)

# The site can't sign in or reach its API without these; RuntimeArn is empty when chat is off.
required = ['UserPoolId', 'UserPoolClientId', 'UserPoolDomainUrl', 'GatewayScope', 'HttpApiUrl', 'EventsApiEndpoint']
missing = [k for k in required if not flat.get(k)]
if missing:
    sys.exit(f'ERROR: {sys.argv[1]} lacks {", ".join(missing)}. Deploy the backend for this stage first.')
if '_' not in flat['UserPoolId']:
    sys.exit(f'ERROR: UserPoolId {flat["UserPoolId"]!r} is not <region>_<id>, so the region is unknown.')

# Map to template placeholders
mapping = {
    # A user pool id is '<region>_<id>', e.g. 'us-east-1_EXAMPLE12'.
    'REGION': flat['UserPoolId'].rsplit('_', 1)[0],
    'USER_POOL_ID': flat['UserPoolId'],
    'USER_POOL_CLIENT_ID': flat['UserPoolClientId'],
    'COGNITO_DOMAIN': flat['UserPoolDomainUrl'].removeprefix('https://'),
    'GATEWAY_SCOPE': flat['GatewayScope'],
    'HTTP_API_URL': flat['HttpApiUrl'],
    'EVENTS_API_ENDPOINT': flat['EventsApiEndpoint'],
    'RUNTIME_ARN': flat.get('RuntimeArn', ''),
}

# Read template, replace placeholders, write aws-exports.ts
with open('aws-exports.ts.template') as f:
    content = f.read()
for key, value in mapping.items():
    content = content.replace(f'<{key}>', value)
with open('src/aws-exports.ts', 'w') as f:
    f.write(content)

print('Generated src/aws-exports.ts')
for k, v in mapping.items():
    print(f'  {k}: {v[:60]}')
PY

# Build exactly the versions package-lock.json records.
npm ci
npm run build

echo "Build complete: dist/"
