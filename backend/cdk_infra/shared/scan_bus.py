#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What the schema service's and the database providers' stacks agree on about the scan bus.

The event sources and types are the ones shared/scan_events.py (runtime) names;
tests/unit/test_scan_events.py checks the two agree.
"""

from aws_cdk import Duration

SCHEMA_SERVICE_SOURCE = "schema-service"
DATABASE_PROVIDER_SOURCE = "database-provider"
SCAN_REQUESTED = "ScanRequested"
REGISTRATION_DISABLED = "RegistrationDisabled"
PROVIDER_EVENTS = ["ProviderRegistered", "ProviderDeregistered", "ScanCompleted", "ScanFailed"]

# Lambda's maximum: a provider's scan cannot run longer, so a scan still waiting
# past this was cut off and will never answer.
SCAN_TIMEOUT = Duration.minutes(15)
