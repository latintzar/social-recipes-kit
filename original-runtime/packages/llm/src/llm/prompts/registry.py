"""
llm.prompts.registry — catalogue of loaded prompts
=====================================================

Discovers every ``.md`` under :mod:`llm.prompts.templates`, indexes by
(id, version, language), and exposes :class:`PromptRegistry` for
lookup / :func:`render_prompt` for one-shot render.

Population strategy: lazy. First call to :meth:`PromptRegistry.get`
walks the templates directory and builds the index in-place. This keeps
import-time side effects limited to ``llm.config`` / ``llm.errors`` and
makes tests that mock the template dir trivial to write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llm.errors import PromptNotFound
from llm.prompts.loader import Prompt, load_prompt

_TEMPLATES_ROOT = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PromptRegistry:
    """In-memory catalogue keyed on ``(id, version, language)``.

    Constructor is zero-arg; :meth:`_load` populates from
    ``llm/prompts/templates/``. Tests use :meth:`register` to inject a
    fixture prompt without touching disk.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root: Path = root or _TEMPLATES_ROOT
        self._prompts: Dict[Tuple[str, int, Optional[str]], Prompt] = {}
        self._loaded: bool = False

    def _load(self) -> None:
        """Walk the templates root, parse every ``.md``. Idempotent."""
        if self._loaded:
            return
        if self._root.exists():
            for md in sorted(self._root.rglob("*.md")):
                p = load_prompt(md)
                self._prompts[(p.id, p.version, p.language)] = p
        self._loaded = True

    def register(self, prompt: Prompt) -> None:
        """Inject a :class:`Prompt` into the registry. Idempotent on key.

        Tests use this to avoid disk I/O.
        """
        self._loaded = True
        self._prompts[(prompt.id, prompt.version, prompt.language)] = prompt

    def get(
        self,
        prompt_id: str,
        *,
        version: Optional[int] = None,
        language: Optional[str] = None,
    ) -> Prompt:
        """Look up a prompt. *version* ``None`` ⇒ highest; *language*
        ``None`` ⇒ language-agnostic entry, else falls through to
        language-specific entry.

        Raises
        ------
        PromptNotFound
            No matching entry (after walking the templates dir).
        """
        self._load()

        candidates: List[Prompt] = [
            p
            for p in self._prompts.values()
            if p.id == prompt_id
            and (version is None or p.version == version)
            and (language is None or p.language in (None, language))
        ]
        if not candidates:
            raise PromptNotFound(
                f"no prompt found for id={prompt_id!r} "
                f"version={version!r} language={language!r}"
            )
        candidates.sort(key=lambda p: (p.version, p.language or ""), reverse=True)
        return candidates[0]

    def list(self) -> List[str]:
        """Every known prompt id, in registration order (deduplicated)."""
        self._load()
        seen: set[str] = set()
        out: List[str] = []
        for p in self._prompts.values():
            if p.id not in seen:
                seen.add(p.id)
                out.append(p.id)
        return sorted(out)


# Module-level default registry. Tests that need isolation instantiate
# their own ``PromptRegistry(root=tmp_path)``.
PROMPTS: PromptRegistry = PromptRegistry()


def render_prompt(
    prompt_id: str,
    *,
    variables: Optional[Dict[str, Any]] = None,
    version: Optional[int] = None,
    language: Optional[str] = None,
) -> str:
    """One-shot: look up *prompt_id* and :meth:`~Prompt.render` it.

    The go-to call for 99% of use. Goes through :data:`PROMPTS`, so
    tests using :meth:`PromptRegistry.register` will pick up their
    fixtures transparently.
    """
    return PROMPTS.get(prompt_id, version=version, language=language).render(variables)


def list_prompts() -> List[str]:
    """Convenience wrapper around :meth:`PROMPTS.list`."""
    return PROMPTS.list()


def get_prompt(
    prompt_id: str,
    *,
    version: Optional[int] = None,
    language: Optional[str] = None,
) -> Prompt:
    """Return the raw :class:`Prompt` without rendering.

    Useful for lint tools + docs — callers that want to inspect
    ``.model`` hint, ``.description``, etc. without incurring a Jinja
    render pass.
    """
    return PROMPTS.get(prompt_id, version=version, language=language)


__all__ = [
    "PromptRegistry",
    "PROMPTS",
    "render_prompt",
    "list_prompts",
    "get_prompt",
]
