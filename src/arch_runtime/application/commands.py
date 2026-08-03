"""Strict immutable application commands."""

from __future__ import annotations

from typing import Annotated, Literal

from arch_kernel.contracts import ActorType, Route
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator


class CreateProjectCommand(BaseModel):
    """Normalized intent to create one minimal project aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_name: Literal["arch.runtime.command.create_project"] = (
        "arch.runtime.command.create_project"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    summary: Annotated[str, StringConstraints(max_length=2_000)] | None = None
    owner: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    project_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    criticality: Literal["low", "medium", "high", "critical"]
    default_route: Route
    objectives: tuple[Annotated[str, StringConstraints(min_length=1, max_length=500)], ...] = ()
    constraints: tuple[Annotated[str, StringConstraints(min_length=1, max_length=500)], ...] = ()
    success_criteria: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=500)], ...
    ] = ()
    actor_type: ActorType = ActorType.HUMAN
    actor_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor_display_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("idempotency_key", "name", "owner", "project_type", "actor_id"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        for field in ("summary", "actor_display_name"):
            item = normalized.get(field)
            if isinstance(item, str):
                normalized[field] = item.strip()
        slug = normalized.get("slug")
        if isinstance(slug, str):
            normalized["slug"] = slug.strip().lower()
        for field in ("objectives", "constraints", "success_criteria"):
            items = normalized.get(field)
            if isinstance(items, (list, tuple)):
                normalized[field] = tuple(
                    item.strip() if isinstance(item, str) else item for item in items
                )
        return normalized

    def request_payload(self) -> dict[str, object]:
        """Return normalized logical intent without its idempotency scope key."""

        return self.model_dump(
            mode="json",
            exclude={"idempotency_key"},
        )
