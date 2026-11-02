"""
core.auth.users — :class:`CurrentUser` identity dataclass
=========================================================

Resolved caller identity returned by :func:`core.auth.require_auth` and
its role-specific variants. Routes type-annotate it to get the identity
injected:

.. code-block:: python

    from fastapi import Depends
    from core import CurrentUser, require_contractor

    @router.get("/v1/dashboard")
    async def dashboard(user: CurrentUser = Depends(require_contractor)):
        ...

Role derivation
---------------
``role == "contractor"`` iff a claimed contractor row exists for the
Supabase ``auth_user_id``. New signups without a contractor row default
to ``"homeowner"``. See ``internal/refactor/packages/core/audit.md``
§auth invariant 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

__all__ = ["CurrentUser"]


@dataclass
class CurrentUser:
    """Verified caller identity resolved from a Supabase JWT.

    Fields:
      - ``id``: Supabase ``auth.users.id`` UUID.
      - ``email``: verified email on the account.
      - ``role``: ``"contractor"`` iff a claimed contractor row exists,
        else ``"homeowner"``.
      - ``contractor_id``: set only when ``role == "contractor"``.
    """

    id: str
    email: str
    role: Literal["contractor", "homeowner"]
    contractor_id: Optional[str] = None
