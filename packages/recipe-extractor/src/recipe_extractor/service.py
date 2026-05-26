"""Tiny HTTP wrapper so an app in any language can call the extractor.

    pip install "recipe-extractor[service]"
    recipe-extractor-serve            # -> http://127.0.0.1:8000

Then from your app:

    POST /extract     {"url": "https://www.tiktok.com/@user/video/123"}
    POST /synthesize  {"brief": "something with leftover chicken and spinach"}
    GET  /videos/<id>.mp4              # the persisted, natively-playable MP4

The /videos static mount serves whatever RECIPE_OUTPUT_DIR/videos contains, so
the URL you hand your frontend player is just <base>/videos/<id>.mp4.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .extract import VIDEOS_DIR, extract_recipe
from .synthesize import synthesize_recipe_from_brief

app = FastAPI(title="recipe-extractor", version="0.1.0")


class ExtractBody(BaseModel):
    url: str
    title: str | None = None
    model: str | None = None


class SynthesizeBody(BaseModel):
    brief: str
    dietary_notes: str | None = None
    model: str | None = None


@app.post("/extract")
def extract(body: ExtractBody) -> dict:
    try:
        result = extract_recipe(body.url, title=body.title, model=body.model or _default_model())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("status") != "processed":
        raise HTTPException(status_code=502, detail=result.get("extraction", {}).get("error", "extraction failed"))
    return result


@app.post("/synthesize")
def synthesize(body: SynthesizeBody) -> dict:
    try:
        return synthesize_recipe_from_brief(
            body.brief, dietary_notes=body.dietary_notes, model=body.model
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _default_model() -> str:
    return os.environ.get("RECIPE_MODEL", "google/gemini-2.5-flash")


# Serve persisted MP4s for native playback. Created lazily so import never fails.
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/videos", StaticFiles(directory=str(VIDEOS_DIR)), name="videos")


def run() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("RECIPE_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("RECIPE_SERVICE_PORT", "8000")),
    )


if __name__ == "__main__":
    run()
