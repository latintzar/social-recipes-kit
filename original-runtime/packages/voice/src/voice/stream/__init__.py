"""Streaming STT — home reserved for Phase 4.

Phase 3.5b: empty. The Deepgram WebSocket proxy in
``apps/scope-backend/routes/transcribe_stream.py`` remains app-level per the
Wave-12 design (``design.md`` D5); Phase 4 absorbs it here as
``voice.stream.deepgram.DeepgramStreamingProvider`` alongside a
``StreamingProvider`` Protocol + ``StreamingEvent`` type.

No exportable symbols in Phase 3.5b. Importing this module is a no-op.
"""

from __future__ import annotations

__all__: list[str] = []
