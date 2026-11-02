# Social Recipes

Read [README.md](README.md), [the product tour](docs/product-tour.md) and [the runtime guide](docs/agent-runtime.md). The default product is Instagram Reel → recipe in the same chat. Vitrina Local is the separate commerce layer; its original broader skill/tools are retained as source context.

Use Python 3.14 and the combined hash-pinned `requirements-agent.lock` for the recovered agent. The standalone extractor has its own Python 3.10+ package/setup. Run `python -m pytest -q` for offline checks. Configuration belongs in your own `SOCIAL_RECIPES_*` environment. The public launcher selects one dedicated pinned Hermes checkout and private state directory.

Preserve original source lineage in `provenance/original-runtime.json`. Record narrow adaptations there when changing recovered files. Historical deployment notes under `original-runtime/` describe the retained prototype; the current runnable setup is `docs/agent-runtime.md`. Use fictional test fixtures and explicit own accounts for live connection tests.
