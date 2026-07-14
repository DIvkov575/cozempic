"""The Insight — shared currency between the extractor and the persistence bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TrustClass(str, Enum):
    USER_DIRECTIVE = "user-directive"      # intent/preference/correction — keep verbatim
    AGENT_PROVISIONAL = "agent-provisional"  # model claim — keep only if corroborated
    WORLD_FACT = "world-fact"              # user-asserted fact — never ground truth


@dataclass(frozen=True)
class Insight:
    slug: str          # kebab-case; unique within partition; the [[link]] target
    title: str         # human title for MEMORY.md
    description: str    # one dense line, used for index + recall relevance
    type: str          # user | feedback | project | reference (format.md)
    trust_class: TrustClass
    body: str          # the fact; preserve original wording

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "type": self.type,
            "trust_class": self.trust_class.value,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Insight":
        return cls(
            slug=d["slug"],
            title=d["title"],
            description=d["description"],
            type=d["type"],
            trust_class=TrustClass(d["trust_class"]),
            body=d["body"],
        )
