#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Cognito pre token generation trigger (V2): puts the user's email in their access token.

Users sign in with their email, so their Cognito username is a generated id that no one
recognises. The admin routes record who made each change from the verified access token;
with the email in it, a record names a person. Only the access token is changed (the ID
token already carries the email), and only by adding this one claim.
"""

EMAIL_CLAIM = "email"


def handler(event, context):
    email = (event.get("request", {}).get("userAttributes") or {}).get("email", "")
    if email:
        event.setdefault("response", {})["claimsAndScopeOverrideDetails"] = {
            "accessTokenGeneration": {"claimsToAddOrOverride": {EMAIL_CLAIM: email}},
        }
    return event
