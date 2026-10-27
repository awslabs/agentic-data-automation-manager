#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

# The agent's image holds the agent, the verifier it checks each caller's token with, and
# the packages they import, and nothing else: no database driver, no provider code, and no
# package installer. Built from the package (cdk_infra/shared/bundling.py AGENT_IMAGE_FILES).

# Pinned by digest, so every build starts from the same image (update both together).
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS build

COPY agent_provider/agent_runtime/requirements.txt requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# Packages come from the build stage. The installer is removed so that code running
# in the agent cannot add to what it has.
COPY --from=build /install /usr/local
RUN pip uninstall -y pip setuptools wheel \
    && useradd -m -u 1000 bedrock_agentcore

WORKDIR /app
COPY agent_provider/agent_runtime/agent.py agent.py
COPY agent_provider/__init__.py agent_provider/identity_provider.py agent_provider/cognito_identity_provider.py agent_provider/
COPY shared/__init__.py shared/environment.py shared/

USER bedrock_agentcore

EXPOSE 8080

CMD ["opentelemetry-instrument", "python", "agent.py"]
