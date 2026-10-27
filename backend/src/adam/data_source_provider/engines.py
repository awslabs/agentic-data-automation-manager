#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The engines this service implements, and what a connection to each looks like.

The mapping from an engine string onto its provider class. Adding an engine is one
entry here, one in cdk_infra/shared/data_source_engines.py (its functions and driver)
and its SSM key in shared/provider_client.py, which tests/unit/test_bundles.py checks
agree; plus, for an engine located by host, its name in schema_provider/source_identity.py,
and its Glue classification in the schema provider. Engine modules are imported lazily,
so a function that only routes needs no database driver.
"""

from importlib import import_module

_ENGINE_PROVIDERS: dict[str, tuple[str, str]] = {
    "oracle": (
        "adam.data_source_provider.oracle_provider",
        "OracleProvider",
    ),
    "postgres": (
        "adam.data_source_provider.postgresql_provider",
        "PostgreSQLProvider",
    ),
    "mysql": (
        "adam.data_source_provider.mysql_provider",
        "MySQLProvider",
    ),
    "mariadb": (
        "adam.data_source_provider.mariadb_provider",
        "MariaDBProvider",
    ),
    "redshift": (
        "adam.data_source_provider.redshift_provider",
        "RedshiftProvider",
    ),
}

# Every engine but Oracle is located by a URL; this is its scheme.
_URL_SCHEMES = {"postgres": "postgresql", "mysql": "mysql", "mariadb": "mariadb", "redshift": "redshift"}


class UnknownEngine(ValueError):
    """An engine this service has no provider for."""


def known_engines() -> list[str]:
    return sorted(_ENGINE_PROVIDERS)


def provider_class(engine: str, *, described_as: str = ""):
    """The provider class for `engine`. Raises UnknownEngine, naming the known engines."""
    engine = engine.lower()
    if engine not in _ENGINE_PROVIDERS:
        raise UnknownEngine(
            f"{described_as or 'The request'} names engine {engine!r}, which has no "
            f"provider. Known engines: {known_engines()}"
        )
    module_path, class_name = _ENGINE_PROVIDERS[engine]
    return getattr(import_module(module_path), class_name)


def build_dsn(engine: str, host: str, port: str, database: str) -> str:
    """The engine's native DSN form."""
    if engine == "oracle":
        return f"{host}:{port}/{database}"
    return f"{_URL_SCHEMES[engine]}://{host}:{port}/{database}"
