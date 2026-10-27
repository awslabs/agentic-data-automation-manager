#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""An administrator's new user is told where to sign in, not just a username and password."""

import pytest

from adam.auth_provider import invitation_message_trigger as trigger


def _event(source):
    return {"triggerSource": source, "request": {"usernameParameter": "{username}", "codeParameter": "{####}"},
            "response": {"emailSubject": None, "emailMessage": None}}


class _Ssm:
    def __init__(self, missing=False):
        self.missing, self.names = missing, []

    def get_parameter(self, Name):
        self.names.append(Name)
        if self.missing:
            raise RuntimeError("ParameterNotFound")
        return {"Parameter": {"Value": "https://d123.cloudfront.net"}}


@pytest.fixture
def ssm(monkeypatch):
    monkeypatch.setenv("STACK_NAME", "T")
    client = _Ssm()
    monkeypatch.setattr(trigger.boto3, "client", lambda name: client)
    return client


def test_the_invitation_links_to_the_website_and_keeps_cognitos_placeholders(ssm):
    message = trigger.handler(_event("CustomMessage_AdminCreateUser"), None)["response"]["emailMessage"]
    assert 'href="https://d123.cloudfront.net"' in message
    # Cognito refuses a message without them: they are where it puts the username and password.
    assert "{username}" in message and "{####}" in message
    assert ssm.names == ["/T/origin_frontend"]


def test_other_messages_keep_cognitos_text(ssm):
    event = trigger.handler(_event("CustomMessage_ForgotPassword"), None)
    assert event["response"]["emailMessage"] is None and ssm.names == []


def test_no_website_means_no_invitation(monkeypatch):
    monkeypatch.setenv("STACK_NAME", "T")
    monkeypatch.setattr(trigger.boto3, "client", lambda name: _Ssm(missing=True))
    with pytest.raises(RuntimeError, match="ParameterNotFound"):
        trigger.handler(_event("CustomMessage_AdminCreateUser"), None)
