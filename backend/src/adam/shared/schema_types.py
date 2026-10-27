#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The two value types the data source providers and the schema provider both speak.

An object's identity, and a scanned object with its definition. Kept here, imported
by both sides, so neither service imports the other's code. Standard library only:
it is bundled with several functions and must not depend on any of them.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectRef:
    """A schema object's identity (a table or view, for a database), compared case-insensitively.

    Case-folded because the same object is spelled `MCP_DEV.CLIENTS` by Oracle
    and `mcp_dev.clients` by the Glue catalog, and visibility checks compare
    the two.
    """

    schema: str
    name: str

    def __post_init__(self):
        object.__setattr__(self, "schema", self.schema.casefold())
        object.__setattr__(self, "name", self.name.casefold())

    def to_dict(self) -> dict[str, str]:
        return {"schema": self.schema, "name": self.name}

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "ObjectRef":
        return cls(schema=value["schema"], name=value["name"])


@dataclass(frozen=True)
class SchemaObject:
    """One scanned object, with its definition as the source reports it (DDL, for a database)."""

    schema: str
    name: str
    kind: str  # the source's word for it: "table" or "view", for a database
    definition: str

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.schema, self.name)

    def to_dict(self) -> dict[str, str]:
        return {"schema": self.schema, "name": self.name, "kind": self.kind, "definition": self.definition}

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "SchemaObject":
        """Raises ValueError for anything that is not a scanned object."""
        fields = ("schema", "name", "kind", "definition")
        if not isinstance(value, dict) or not all(isinstance(value.get(f), str) and value[f] for f in fields):
            raise ValueError(f"Not a scanned schema object: {str(value)[:200]}")
        return cls(**{f: value[f] for f in fields})
