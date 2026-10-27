#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What each engine's provider and scanner functions are built from.

The deploy-time half of adam/data_source_provider/engines.py:
adding an engine is one entry there, one here, and its SSM key in
shared/provider_client.py (tests/unit/test_bundles.py checks the three agree).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineBuild:
    # The CDK id prefixes. The scanner's predates the provider's rename; changing it
    # replaces the scanner's registration, whose delete deregisters the engine.
    provider_id: str
    scanner_id: str
    # pip requirements for the driver, each at an exact version: the driver, and any dependency
    # a newer release of which breaks it.
    driver: tuple[str, ...]
    # The engine's modules in data_source_provider/, beside the ones every provider carries.
    files: tuple[str, ...]
    # Verifies the server against the RDS CA bundle (shared/database_tls.py).
    rds_tls: bool
    # SSM key, under /<parent stack>/, the provider function's name is published at.
    ssm_key: str
    # Installed without the dependencies it declares, when some of those serve only features this
    # project does not use; `driver` then lists, at exact versions, the ones it does need.
    driver_alone: tuple[str, ...] = ()


ENGINES: dict[str, EngineBuild] = {
    "oracle": EngineBuild("Oracle", "Oracle", ("oracledb==26.0.1",), ("oracle_provider.py", "tls.py"), True,
                          "oracle_provider_function_name"),
    # scramp 1.5.0 dropped make_channel_binding, which pg8000 1.31.5 (the newest) calls on connect.
    "postgres": EngineBuild("PostgreSQL", "Postgres", ("pg8000==1.31.5", "scramp==1.4.17"),
                            ("postgresql_provider.py", "tls.py"), True, "postgresql_provider_function_name"),
    "mysql": EngineBuild("MySQL", "MySQL", ("PyMySQL==1.2.3",), ("mysql_provider.py", "tls.py"), True,
                         "mysql_provider_function_name"),
    "mariadb": EngineBuild("MariaDB", "MariaDB", ("PyMySQL==1.2.3",),
                           ("mysql_provider.py", "mariadb_provider.py", "tls.py"), True,
                           "mariadb_provider_function_name"),
    # redshift_connector verifies the server against the CA bundle it ships. Of what it declares, only
    # scramp, packaging and boto3 are imported for a password login: requests (and with it certifi,
    # MPL-2.0), beautifulsoup4 and lxml serve only its SAML/Okta/ADFS/Azure sign-in plugins.
    "redshift": EngineBuild("Redshift", "Redshift", ("scramp==1.4.17", "packaging==26.3", "boto3==1.43.103"),
                            ("redshift_provider.py",), False, "redshift_provider_function_name",
                            driver_alone=("redshift_connector==2.2.0",)),
}
