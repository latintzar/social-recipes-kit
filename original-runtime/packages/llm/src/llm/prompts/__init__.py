"""
llm.prompts — versioned, bilingual, Jinja-rendered prompt library
====================================================================

Replaces scattered ``SYSTEM_PROMPT = '''...'''`` module-level constants
in routes and services. Every prompt is a ``.md`` file with a YAML
frontmatter block (metadata), rendered through Jinja at call time.

Layout::

    templates/
        concern_classifier/
            v1.md                 # version-pinned
        variation_extraction/
            v1_en.md             # language pinned
            v1_es.md

Public surface
--------------

>>> render_prompt("concern_classifier", variables={"message": "la caldera se rompió"})
"You are Leo..."

>>> PROMPTS.list()
['concern_classifier', 'variation_extraction', ...]

See :class:`~llm.prompts.registry.PromptRegistry` for the full API.
"""

from llm.prompts.loader import Prompt, load_prompt
from llm.prompts.registry import (
    PROMPTS,
    PromptRegistry,
    get_prompt,
    list_prompts,
    render_prompt,
)

__all__ = [
    "Prompt",
    "PromptRegistry",
    "PROMPTS",
    "load_prompt",
    "render_prompt",
    "list_prompts",
    "get_prompt",
]
