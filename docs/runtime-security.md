# Runtime boundaries

The recipe-agent entry admits Instagram events only after HMAC-SHA256 verification and filters them to your configured business-account ID. The webhook body limit is 1 MiB. The internal sender ID determines the conversation UUID and ownership key; clients and model tools cannot submit a different session identity through this entry.

The launcher requires explicit `SOCIAL_RECIPES_*` configuration and a dedicated, unmodified Hermes checkout at the pinned revision. It starts a new interpreter with selected own credentials, an owned state directory and an isolated `HERMES_HOME`. It rejects SDK dotenv files and account-bearing files in that home. Its core package's ancestor-dotenv bootstrap is disabled for this runtime.

The model receives the recipe profile and the recipe extraction MCP toolset. The tool validates HTTPS Instagram permalink/Meta Messaging CDN hosts; the published extractor handles download, frames and vision. The tool subprocess receives vision credentials; Instagram and session-store credentials remain in the transport process. Hermes memory, context-file identity, trajectory saving and background review are disabled in the factory.

Sessions use a dedicated server-only Supabase schema with RLS and explicit grants. The original thread handlers enforce sender ownership again. Thread-access/storage failures stop the public turn rather than silently starting a different history. There is one in-process queue and one recent-message cache; the recovery preserves that single-worker prototype contract.

The optional original shopper/commerce source retains its historical architecture and tests. Its other endpoints, memory, stock, location, voice and payments belong to a separate integration review and are outside the recipe launcher's route/tool surface.

Offline tests deny network connections and use fictional sender IDs, a fake model/extraction result and the original in-memory Supabase fixture. They establish code-path and isolation behavior. Your Meta permissions, current CDN response and provider accuracy are checked with your own accounts when connecting the product.
