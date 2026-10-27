#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Cognito Custom Message trigger: the invitation an administrator's new user receives links to the website.

Cognito still generates the temporary password and sends the email; this only writes
its text. The website's address is read when the invitation is sent, from the SSM
parameter the website stack publishes, because the website is deployed after this
stack. Every other message keeps Cognito's default text.
"""

import os

import boto3


def handler(event, context):
    if event["triggerSource"] != "CustomMessage_AdminCreateUser":
        return event
    stack = os.environ["STACK_NAME"]
    # Raises when the website is not deployed: the invitation is refused, not sent without a link.
    website = boto3.client("ssm").get_parameter(Name=f"/{stack}/origin_frontend")["Parameter"]["Value"]
    request = event["request"]
    event["response"]["emailSubject"] = "Your Agentic Data Automation Manager (ADAM) account"
    event["response"]["emailMessage"] = (
        f"An administrator created an account for you.<br><br>"
        f'Sign in at <a href="{website}">{website}</a><br>'
        f"Username: {request['usernameParameter']}<br>"
        f"Temporary password: {request['codeParameter']}<br><br>"
        "You'll choose a new password the first time you sign in."
    )
    return event
