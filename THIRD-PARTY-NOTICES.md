# Third-party notices

Peter Neyra's original Social Recipes and recovered Vitrina implementation are released under the root [MIT licence](LICENSE). The source lineage is recorded in [provenance/original-runtime.json](provenance/original-runtime.json).

The agent uses [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) as an external dependency at revision `f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`. Copyright © 2025 Nous Research. Its exact [MIT licence](LICENSES/hermes-agent-MIT.txt), source metadata hashes and dependency profiles are recorded in [runtime/hermes.lock.json](runtime/hermes.lock.json). Its implementation is obtained from the official repository during setup.

Python dependencies are separately licensed distributions installed through [requirements-agent.lock](requirements-agent.lock). That lock combines this project's dependencies with the pinned SDK's core, MCP and Anthropic declarations and its upstream security overrides. Their licences and notices remain with the installed distributions. FFmpeg, yt-dlp, the React player’s dependencies, connected providers and original social videos retain their own terms.

The broader shopper source includes optional provider/service integrations. Its presence records the original architecture; using those integrations requires your own accounts, dependencies and connection checks. The default public launcher selects the recipe-only profile.
