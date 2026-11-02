"""Explicit recipient configuration; no account or provider discovery."""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

HERMES_REVISION = "f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab"
REPOSITORY = Path(__file__).resolve().parents[2]
if not (REPOSITORY / "original-runtime").is_dir():
    REPOSITORY = Path(__file__).resolve().parent / "_source"
ORIGINAL = REPOSITORY / "original-runtime"
BACKEND = ORIGINAL / "apps/vitrina/product/backend"
PREFIX = "SOCIAL_RECIPES_"
REQUIRED = (
    "PROVIDER",
    "MODEL",
    "API_KEY",
    "STATE_DIR",
    "HERMES_SOURCE",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "IG_APP_SECRET",
    "IG_VERIFY_TOKEN",
    "IG_ACCESS_TOKEN",
    "IG_BUSINESS_ID",
)


class ConfigurationError(ValueError):
    pass


def require_private_regular_file(path: Path) -> None:
    """Existing state files have one owned regular inode, never an alias/pipe."""
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise ConfigurationError(
                "Use dedicated regular files for this project's private state"
            )


@dataclass(frozen=True)
class RuntimeConfig:
    values: dict[str, str]

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> RuntimeConfig:
        source = os.environ if env is None else env
        missing = [
            PREFIX + k for k in REQUIRED if not source.get(PREFIX + k, " ").strip()
        ]
        if missing:
            raise ConfigurationError("Configure your own " + ", ".join(missing))
        values = {k: str(source[PREFIX + k]).strip() for k in REQUIRED}
        for key in (
            "VISION_BASE_URL",
            "VISION_MODEL",
            "VISION_API_KEY",
            "PUBLIC_BASE_URL",
        ):
            if source.get(PREFIX + key):
                values[key] = source[PREFIX + key].strip()
        if values["PROVIDER"] not in {"anthropic", "openrouter"}:
            raise ConfigurationError(
                "SOCIAL_RECIPES_PROVIDER must be anthropic or openrouter"
            )
        for key in ("STATE_DIR", "HERMES_SOURCE"):
            if not Path(values[key]).is_absolute():
                raise ConfigurationError(PREFIX + key + " must be an absolute path")
        if len(values["IG_APP_SECRET"]) < 16 or len(values["IG_VERIFY_TOKEN"]) < 16:
            raise ConfigurationError(
                "Use app-secret and verification-token values of at least 16 characters"
            )
        vision = [
            values.get(k) for k in ("VISION_BASE_URL", "VISION_MODEL", "VISION_API_KEY")
        ]
        if (any(vision) and not all(vision)) or (
            values["PROVIDER"] == "anthropic" and not all(vision)
        ):
            raise ConfigurationError(
                "Configure VISION_BASE_URL, VISION_MODEL and VISION_API_KEY together for your OpenAI-compatible vision service"
            )
        if values.get("VISION_BASE_URL"):
            vision_endpoint = urlparse(values["VISION_BASE_URL"])
            if (
                vision_endpoint.scheme != "https"
                or not vision_endpoint.hostname
                or vision_endpoint.username
                or vision_endpoint.password
            ):
                raise ConfigurationError("Use your HTTPS vision-service base URL")
        endpoint = urlparse(values["SUPABASE_URL"])
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
        ):
            raise ConfigurationError(
                "SOCIAL_RECIPES_SUPABASE_URL must be your HTTPS project URL"
            )
        return cls(values)

    @property
    def state(self) -> Path:
        return Path(self.values["STATE_DIR"]).resolve()

    @property
    def hermes(self) -> Path:
        return self.state / "hermes"

    @property
    def sdk(self) -> Path:
        return Path(self.values["HERMES_SOURCE"]).resolve()

    def validate_sdk(self) -> None:
        if Path(self.values["HERMES_SOURCE"]).is_symlink():
            raise ConfigurationError("Use a dedicated Hermes source directory")
        # Check metadata only. Never read existing user credential/config files.
        if (self.sdk / ".env").exists():
            raise ConfigurationError(
                "Use a dedicated unmodified Hermes source checkout with configuration in this project's environment"
            )
        if not (self.sdk / "run_agent.py").is_file():
            raise ConfigurationError(
                "SOCIAL_RECIPES_HERMES_SOURCE must contain the pinned official Hermes source"
            )
        result = subprocess.run(
            ["git", "-C", str(self.sdk), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or result.stdout.strip() != HERMES_REVISION:
            raise ConfigurationError(
                "Check out official Hermes revision " + HERMES_REVISION
            )
        clean = subprocess.run(
            ["git", "-C", str(self.sdk), "diff", "--quiet", "HEAD", "--"],
            capture_output=True,
            check=False,
        )
        if clean.returncode:
            raise ConfigurationError("Use the unmodified pinned Hermes source checkout")
        extras = subprocess.run(
            ["git", "-C", str(self.sdk), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=False,
        )
        if extras.returncode or extras.stdout.strip():
            raise ConfigurationError(
                "Keep your dedicated Hermes checkout free of additional source/config files"
            )

    def prepare_state(self) -> None:
        raw = Path(self.values["STATE_DIR"])
        if (
            any(part.is_symlink() for part in (raw, *raw.parents))
            or self.state == Path.home().resolve()
            or self.state == Path("/")
        ):
            raise ConfigurationError("Choose a dedicated empty state directory")
        marker = self.state / ".social-recipes-owned"
        require_private_regular_file(marker)
        require_private_regular_file(self.hermes / "config.yaml")
        if marker.is_symlink() or self.hermes.is_symlink():
            raise ConfigurationError(
                "State ownership files must stay within your state directory"
            )
        if (
            self.state.exists()
            and any(self.state.iterdir())
            and (
                not marker.is_file()
                or marker.read_text() != "social-recipes-agent-v1\n"
            )
        ):
            raise ConfigurationError("Choose an empty state directory for this project")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        marker.write_text("social-recipes-agent-v1\n")
        marker.chmod(0o600)
        if self.hermes.is_symlink() or marker.is_symlink():
            raise ConfigurationError(
                "State ownership files must stay within your state directory"
            )
        self.hermes.mkdir(exist_ok=True, mode=0o700)
        self.hermes.chmod(0o700)
        # Account-bearing state is explicitly absent before importing the SDK.
        if any(
            (self.hermes / name).exists()
            for name in (".env", ".op.env", "auth.json", "managed/.env")
        ):
            raise ConfigurationError(
                "Keep account keys in this project's explicit environment"
            )

    def child_environment(
        self, inherited: dict[str, str] | None = None
    ) -> dict[str, str]:
        source = os.environ if inherited is None else inherited
        # Preserve ordinary system identity; never borrow ambient provider/account settings.
        env = {
            k: source[k]
            for k in (
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "TZ",
                "SYSTEMROOT",
                "VIRTUAL_ENV",
            )
            if k in source
        }
        env.update({PREFIX + k: v for k, v in self.values.items()})
        env.update(
            {
                "SOCIAL_RECIPES_RUNTIME": "1",
                "SOCIAL_RECIPES_PROFILE": "recipe",
                "HERMES_HOME": str(self.hermes),
                "HERMES_AGENT_RUN_ID": "social-recipes",
                "OTEL_SDK_DISABLED": "true",
                "DO_NOT_TRACK": "1",
                "HERMES_DISABLE_LAZY_INSTALLS": "1",
                "VITRINA_AGENT_PROVIDER": self.values["PROVIDER"],
                "VITRINA_AGENT_MODEL": self.values["MODEL"],
                "VITRINA_AGENT_MAX_TOKENS": "4096",
                "VITRINA_AGENT_TRACE_SUPABASE": "0",
                "SUPABASE_URL": self.values["SUPABASE_URL"],
                "SUPABASE_SERVICE_ROLE_KEY": self.values["SUPABASE_SERVICE_ROLE_KEY"],
                "INSTAGRAM_APP_SECRET": self.values["IG_APP_SECRET"],
                "INSTAGRAM_VERIFY_TOKEN": self.values["IG_VERIFY_TOKEN"],
                "INSTAGRAM_PAGE_ACCESS_TOKEN": self.values["IG_ACCESS_TOKEN"],
                "INSTAGRAM_BUSINESS_ID": self.values["IG_BUSINESS_ID"],
                "RECIPE_OUTPUT_DIR": str(self.state / "recipes"),
                "SOCIAL_RECIPES_SKILL_PATH": str(
                    REPOSITORY / "runtime/profiles/recipe/SKILL.md"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        env[
            "ANTHROPIC_API_KEY"
            if self.values["PROVIDER"] == "anthropic"
            else "OPENROUTER_API_KEY"
        ] = self.values["API_KEY"]
        env["RECIPE_LLM_API_KEY"] = self.values.get(
            "VISION_API_KEY", self.values["API_KEY"]
        )
        env["RECIPE_LLM_URL"] = (
            self.values.get("VISION_BASE_URL", "https://openrouter.ai/api/v1").rstrip(
                "/"
            )
            + "/chat/completions"
        )
        env["RECIPE_MODEL"] = self.values.get("VISION_MODEL", self.values["MODEL"])
        paths = [
            str(self.sdk),
            str(Path(__file__).resolve().parents[1]),
            str(BACKEND / "agent"),
            str(BACKEND),
            str(REPOSITORY / "packages/recipe-extractor/src"),
        ]
        paths.extend(
            str(ORIGINAL / "packages" / p / "src")
            for p in ("core", "llm", "storage", "voice")
        )
        env["PYTHONPATH"] = os.pathsep.join(paths)
        return env
