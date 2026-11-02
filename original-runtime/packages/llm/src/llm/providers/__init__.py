"""Provider adapters for :mod:`llm`.

Each provider owns body construction, HTTP / boto transport, response
parsing, and provider-specific error mapping. Callers never import from
here directly — route through :func:`llm.chat` which dispatches via the
model's ``provider`` attribute.
"""
