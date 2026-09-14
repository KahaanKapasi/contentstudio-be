from dataclasses import dataclass


@dataclass
class RawItem:
    source: str
    source_url: str | None
    raw_text: str


@dataclass
class SourceResult:
    source_name: str
    items: list[RawItem]
    ok: bool
    error: str | None = None
