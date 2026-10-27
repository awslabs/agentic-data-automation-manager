#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The models the chat page offers: the chat route accepts only these, the page lists them
(GET /api/models), and the agent runtime may invoke only these. The first is the default."""

CHAT_MODELS = [
    {"id": "global.anthropic.claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
    {"id": "global.anthropic.claude-opus-5-5", "label": "Claude Opus 5.5"},
    {"id": "global.anthropic.claude-haiku-5-5", "label": "Claude Haiku 5.5"},
    {"id": "global.amazon.nova-2-lite-v1:0", "label": "Amazon Nova 2 Lite"},
]


def invoke_resources(region: str, account: str) -> list[str]:
    """What invoking these models through their inference profiles takes: each profile, and its
    foundation model in whichever Region the profile routes to (global profiles name no Region)."""
    resources = []
    for model in CHAT_MODELS:
        foundation = model["id"].split(".", 1)[1]
        resources += [f"arn:aws:bedrock:{region}:{account}:inference-profile/{model['id']}",
                      f"arn:aws:bedrock:*::foundation-model/{foundation}",
                      f"arn:aws:bedrock:::foundation-model/{foundation}"]
    return resources
