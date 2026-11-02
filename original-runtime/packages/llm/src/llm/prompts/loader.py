"""
llm.prompts.loader — parse ``.md + YAML frontmatter`` → Prompt
================================================================

Frontmatter format::

    ---
    id: concern_classifier
    version: 1
    model: anthropic/claude-haiku-4.5
    language: es
    description: "Classify homeowner messages into care buckets."
    ---
    You are Leo... {{ message }}

Renderer: Jinja2 with ``StrictUndefined`` so a missing variable is a
loud error, not a silent empty string. Sandboxing is on — no filesystem
access from inside templates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from jinja2 import Environment, StrictUndefined

from llm.errors import PromptNotFound


@dataclass(frozen=True)
class Prompt:
    """A loaded prompt template with its metadata.

    Rendered via :meth:`render`. Tests can construct ``Prompt(...)``
    inline without touching disk.
    """

    id: str
    version: int
    body: str
    model: Optional[str] = None
    language: Optional[str] = None
    description: Optional[str] = None
    source_path: Optional[Path] = None

    def render(self, variables: Optional[Dict[str, Any]] = None) -> str:
        """Render the template through Jinja with ``StrictUndefined``.

        Unknown variables raise ``jinja2.UndefinedError`` — never
        silently substitute the empty string.
        """
        env = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        return env.from_string(self.body).render(**(variables or {}))


# ---------------------------------------------------------------------------
# Disk loader
# ---------------------------------------------------------------------------


def _parse_frontmatter(raw: str) -> tuple[Dict[str, Any], str]:
    """Extract YAML frontmatter from a ``.md`` file. Missing → ``{}``."""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise PromptNotFound(f"invalid YAML frontmatter: {exc}") from exc
    return meta, parts[2].lstrip("\n")


def load_prompt(path: Path) -> Prompt:
    """Parse a ``.md + YAML frontmatter`` file into a :class:`Prompt`.

    Raises
    ------
    PromptNotFound
        If *path* doesn't exist, frontmatter is invalid, or ``id`` /
        ``version`` are missing.
    """
    if not path.exists():
        raise PromptNotFound(f"prompt file not found: {path}", model=str(path))

    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)

    if "id" not in meta:
        raise PromptNotFound(f"prompt {path.name!r} missing 'id' frontmatter")
    if "version" not in meta:
        raise PromptNotFound(
            f"prompt {path.name!r} missing 'version' frontmatter"
        )

    return Prompt(
        id=str(meta["id"]),
        version=int(meta["version"]),
        body=body.strip() + "\n",
        model=meta.get("model"),
        language=meta.get("language"),
        description=meta.get("description"),
        source_path=path,
    )


__all__ = ["Prompt", "load_prompt"]
