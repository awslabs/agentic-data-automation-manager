#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What frontend/app.py derives from the stage and the backend's stack outputs."""


def stack_prefix(stage: str) -> str:
    """The prefix of this stage's stack names. Only the first letter is raised, as install_cdk.sh does."""
    return f"Adam{stage[:1].upper()}{stage[1:]}"


def is_production(stage: str) -> bool:
    """By the stacks a stage deploys, not its spelling: "Prod" deploys prod's, so it must not downgrade them."""
    return stack_prefix(stage) == stack_prefix("prod")


def connect_sources(outputs: dict[str, dict[str, str]], region: str) -> list[str]:
    """The origins the UI calls, from this stage's backend outputs: the content security policy
    allows these and nothing else."""
    flat = {k: v for stack in outputs.values() for k, v in stack.items()}
    return [
        flat["UserPoolDomainUrl"].rstrip("/"),
        f"https://cognito-idp.{region}.amazonaws.com",
        flat["HttpApiUrl"].rstrip("/"),
        f"https://{flat['EventsApiEndpoint']}",
        f"wss://{flat['EventsApiRealtimeEndpoint']}",
    ]
