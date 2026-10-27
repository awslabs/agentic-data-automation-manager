#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The web console's chat API: the agent's routes behind the HTTP API.

The connections and admin routes are other services' functions behind the same HTTP API.
"""

from chat import ROUTES

from shared.http_response import route_request


def handler(event, context):
    return route_request(event, ROUTES)
