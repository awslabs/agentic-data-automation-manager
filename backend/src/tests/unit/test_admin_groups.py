#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Reading the caller's Cognito groups from the API Gateway event.

HTTP API hands a list claim to the Lambda as a string such as "[admin ops]", not
as a JSON array; a parser that expects a list would treat every caller as
being in no group, or worse, treat the substring "admin" in "administrators"
as membership.
"""

import pytest

from shared.http_response import ForbiddenError, caller_groups, require_group


def _event(groups):
    claims = {"sub": "s"} if groups is None else {"sub": "s", "cognito:groups": groups}
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


@pytest.mark.parametrize("claim,expected", [
    (None, set()),
    ("", set()),
    ("[]", set()),
    ("[admin]", {"admin"}),
    ("[admin ops]", {"admin", "ops"}),
    ('["admin","ops"]', {"admin", "ops"}),
    (["admin", "ops"], {"admin", "ops"}),
    ("admin", {"admin"}),
])
def test_groups_are_read_in_every_form_gateway_can_deliver(claim, expected):
    assert caller_groups(_event(claim)) == expected


def test_membership_is_exact_not_a_substring():
    with pytest.raises(ForbiddenError):
        require_group(_event("[administrators]"), "admin")
    with pytest.raises(ForbiddenError):
        require_group(_event("[not-admin]"), "admin")


def test_no_groups_is_forbidden():
    with pytest.raises(ForbiddenError):
        require_group(_event(None), "admin")


def test_a_member_passes():
    require_group(_event("[ops admin]"), "admin")
