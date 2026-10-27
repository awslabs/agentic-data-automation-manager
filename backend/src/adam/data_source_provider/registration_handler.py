#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Registers this stack's scanners with the schema service when it deploys.

A CloudFormation custom resource, one per engine, with the properties Engine,
ScannerFunctionArn and ResultsBucket. Create and Update publish
ProviderRegistered on the scan bus; Delete publishes ProviderDeregistered
(shared/scan_events.py). A refused event fails the deployment rather than
leaving an engine silently unregistered.
"""

import logging

import boto3

from adam.shared import scan_events

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    properties = event.get("ResourceProperties") or {}
    engine = str(properties.get("Engine") or "")
    if not engine:
        raise ValueError("The registration resource has no Engine property.")
    events = boto3.client("events")
    if event.get("RequestType") == "Delete":
        scan_events.publish(events, scan_events.DATABASE_PROVIDER, scan_events.PROVIDER_DEREGISTERED,
                            {"engine": engine, "scannerFunctionArn": properties.get("ScannerFunctionArn")})
    else:
        scan_events.publish(events, scan_events.DATABASE_PROVIDER, scan_events.PROVIDER_REGISTERED, {
            "engine": engine,
            "scannerFunctionArn": properties.get("ScannerFunctionArn"),
            "resultsBucket": properties.get("ResultsBucket"),
        })
    logger.info("%s the %s scanner", "Deregistered" if event.get("RequestType") == "Delete" else "Registered",
                engine)
    return {"PhysicalResourceId": f"{engine}-scanner"}
