#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Abstract base class for schema providers.

A schema provider is the searchable index of every scanned data source's object
definitions (DDL, for a database). It is written by the schema ingestion Lambda
(our `scan_schema` per data source) and read by `search_schema`. Where the index lives (Glue Data Catalog today) is an
implementation detail the agent never sees.

The index holds every object it was given, so reading it is authorization-
neutral. What a caller may be shown is decided by the data source, per caller,
and passed in as `visible`. `search_objects` applies it here, in the base class:
implementations only rank, and cannot override the filter.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from adam.shared.schema_types import ObjectRef, SchemaObject


@dataclass(frozen=True)
class VisibleObject:
    """One object a caller may see, in the data source `execute_statement` names it by."""

    data_source: str
    ref: ObjectRef

    def __post_init__(self):
        object.__setattr__(self, "data_source", self.data_source.casefold())

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "VisibleObject":
        return cls(value["data_source"], ObjectRef(value["schema"], value["name"]))

    def to_dict(self) -> dict[str, str]:
        return {"data_source": self.data_source, **self.ref.to_dict()}


@dataclass(frozen=True)
class SchemaMatch:
    """One ranked search hit, before authorization."""

    data_source: str
    engine: str
    schema: str
    name: str
    definition: str

    @property
    def key(self) -> VisibleObject:
        return VisibleObject(self.data_source, ObjectRef(self.schema, self.name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_source": self.data_source,
            "engine": self.engine,
            "object_name": f"{self.schema}.{self.name}",
            "definition": self.definition,
        }


class SchemaProvider(ABC):
    """Searchable schema index: published by ingestion, searched per caller."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "search_objects" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} overrides search_objects. Implement _rank instead: "
                "the visibility filter lives only in the base class."
            )

    def search_objects(self, query: str, *, visible: frozenset[VisibleObject],
                       limit: int) -> list[dict[str, Any]]:
        """The best `limit` matches for `query` among the objects in `visible`.

        `visible` is required and has no default. An empty set returns nothing:
        it means the caller can see no objects, never "no filter".
        """
        if not isinstance(visible, frozenset):
            raise TypeError(
                f"visible must be a frozenset of VisibleObject, got {type(visible).__name__}."
            )
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}.")
        if not visible:
            return []

        results = []
        for match in self._rank(query):
            if match.key in visible:
                results.append(match.to_dict())
                if len(results) >= limit:
                    break
        return results

    @abstractmethod
    def _rank(self, query: str) -> Iterator[SchemaMatch]:
        """Every indexed object matching `query`, best first, lazily.

        Lazy because the caller stops reading once it has `limit` visible
        matches, and producing a match can cost a catalog read.
        """

    @abstractmethod
    def publish_objects(self, *, data_source: str, engine: str,
                        objects: Iterable[SchemaObject]) -> dict[str, Any]:
        """Make the index hold `objects` for this data source.

        Adds and updates the given objects, and removes the data source's objects
        that are no longer in `objects` -- only those this deployment created,
        never one it found already there. `data_source` is the name
        `execute_statement` resolves to a caller's connection.
        """
