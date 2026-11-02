#!/usr/bin/env python3
"""Populate ``match_universal`` on every catalog row.

Run once after a catalog migration or a matcher prompt change. Idempotent
— rows that already have a ``match_universal`` block are skipped unless
``--force`` is passed.

The matcher is OpenRouter-bound network IO with ~10–20s latency per call,
so we offload through the shared ``packages/core`` thread pool and run
**parallel batches** (``--concurrency``, default 10). Within a batch
requests fly together; we wait for the whole batch to settle before
flushing the catalog to disk and starting the next batch. This keeps
the catalog write-side single-threaded (one writer = no merge
conflicts) while collapsing end-to-end runtime ~10×.

Usage::

    OPENROUTER_API_KEY=… python3 scripts/backfill_matches.py
    OPENROUTER_API_KEY=… python3 scripts/backfill_matches.py --force --concurrency 5
    OPENROUTER_API_KEY=… python3 scripts/backfill_matches.py --force --only tiktok-7180050326215527726

The matcher emits non-blocking errors for rows whose recipe is too sparse
to match — they're left untouched and reported at the end. Cost: roughly
$0.05–0.08 per recipe (Sonnet 4.5, ~5k input + ~700 output). Parallelism
doesn't change the per-call cost ceiling (each call still capped at
``max_tokens=4096`` in :mod:`shop_matcher`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

# Bootstrap packages/core onto sys.path for local CLI use. In Docker
# ``PYTHONPATH`` already contains ``/app/packages/core/src``; this
# branch lights up only when the script is run from a developer
# checkout without that env wiring.
_CORE_SRC = HERE.parents[5] / "packages" / "core" / "src"
if _CORE_SRC.exists() and str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from core import shared_thread_pool  # noqa: E402

import shop_matcher  # noqa: E402

CATALOG_PATH = PARENT / "creator_catalog.json"

logger = logging.getLogger("vitrina.backfill_matches")


def load_catalog() -> list[dict]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def write_catalog(rows: list[dict]) -> None:
    CATALOG_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _match_one(row: dict) -> tuple[str, dict | None, str | None, float]:
    """Worker: run the matcher for one row.

    Pure function from row → (row_id, match | None, err | None, elapsed).
    No shared state mutated — the main loop merges the result back into
    the catalog list under the single writer (the main thread itself).
    """
    row_id = row.get("id") or "<no-id>"
    recipe = row.get("recipe") or {}
    started = time.monotonic()
    try:
        match = shop_matcher.match_recipe(
            recipe, catchment_km=None, auto_expand=False
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to caller
        return row_id, None, str(exc)[:200], time.monotonic() - started
    return row_id, match, None, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute matches even when a row already has match_universal.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Restrict to specific row id(s); repeat for multiple.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the matcher but do not write the catalog back.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max parallel matcher calls per batch (default 10).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rows = load_catalog()
    rows_by_id: dict[str, dict] = {r.get("id"): r for r in rows if r.get("id")}
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    matched: list[str] = []

    # Build the work queue first so the parallel executor sees only the
    # rows that actually need a matcher call. Skip-rules stay identical
    # to the pre-parallel script.
    todo: list[dict] = []
    for row in rows:
        row_id = row.get("id") or "<no-id>"
        if args.only and row_id not in args.only:
            continue
        if row.get("status") != "processed":
            skipped.append(f"{row_id} (status={row.get('status')})")
            continue
        if row.get("match_universal") and not args.force:
            skipped.append(f"{row_id} (already matched)")
            continue
        recipe = row.get("recipe") or {}
        if not recipe.get("ingredients"):
            skipped.append(f"{row_id} (no ingredients)")
            continue
        todo.append(row)

    concurrency = max(1, args.concurrency)
    # The shared pool is a process-wide singleton (capped at the value
    # passed on first call). For a one-shot CLI like this, that means
    # our --concurrency flag is honoured even though we go through the
    # shared infra rather than spinning a private executor.
    pool = shared_thread_pool(max_workers=concurrency)

    logger.info(
        "backfill.start total=%d todo=%d concurrency=%d",
        len(rows),
        len(todo),
        concurrency,
    )

    # Process in batches so the catalog file is flushed periodically
    # (cheap durability — if the process dies on row 17 of 20, rows 1-10
    # are already on disk). Within a batch matcher calls fly in parallel;
    # the main thread is the only writer.
    started_total = time.monotonic()
    for batch_start in range(0, len(todo), concurrency):
        batch = todo[batch_start : batch_start + concurrency]
        logger.info(
            "backfill.batch.start size=%d (rows %d–%d of %d)",
            len(batch),
            batch_start + 1,
            batch_start + len(batch),
            len(todo),
        )
        futures = {pool.submit(_match_one, row): row for row in batch}
        for future in as_completed(futures):
            row_id, match, err, elapsed = future.result()
            if err is not None or match is None:
                logger.error("matcher.row.failed id=%s err=%s", row_id, err)
                failed.append((row_id, err or "unknown"))
                continue
            target = rows_by_id.get(row_id)
            if target is None:
                logger.warning(
                    "matcher.row.orphan id=%s (not found in catalog after fetch)",
                    row_id,
                )
                continue
            target["match_universal"] = match
            matched.append(row_id)
            logger.info(
                "matcher.row.ok id=%s feasibility=%s shops=%d total_eur=%.2f elapsed_s=%.1f",
                row_id,
                match.get("feasibility"),
                len(match.get("shop_groups") or []),
                match.get("total_eur") or 0,
                elapsed,
            )
        if not args.dry_run and matched:
            write_catalog(rows)
            logger.info(
                "backfill.batch.flushed matched_so_far=%d path=%s",
                len(matched),
                CATALOG_PATH,
            )

    if args.dry_run:
        logger.info("dry_run=True; catalog not written")

    elapsed_total = time.monotonic() - started_total
    logger.info(
        "backfill.summary matched=%d skipped=%d failed=%d elapsed_s=%.1f",
        len(matched),
        len(skipped),
        len(failed),
        elapsed_total,
    )
    for row_id, err in failed:
        logger.info("  failed: %s — %s", row_id, err)
    for entry in skipped:
        logger.info("  skipped: %s", entry)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
