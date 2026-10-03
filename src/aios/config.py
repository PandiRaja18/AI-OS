"""Runtime configuration, loaded from the environment or a .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Platform settings. Every field is overridable via an AIOS_* env var."""

    model_config = SettingsConfigDict(
        env_prefix="AIOS_", env_file=".env", extra="ignore"
    )

    model: str = "claude-opus-5"
    workspace: Path = Path(".aios")
    data_dir: Path = Path("data")

    # Which model backs a live run: "claude" or "local". A self-hosted server is
    # reached through its OpenAI-compatible endpoint, or Ollama's native one.
    llm_provider: str = "claude"
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:32b-instruct"
    llm_api_style: str = "ollama"
    llm_api_key: str | None = None
    llm_timeout_seconds: float = 300.0
    llm_max_repairs: int = 2

    max_plan_attempts: int = 3
    max_task_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    tool_timeout_seconds: float = 20.0
    promotion_confidence: float = 0.8

    # Demo lever: number of transient failures the ledger tool injects per run so
    # the retry/degradation path is observable in a live walkthrough.
    injected_ledger_failures: int = 2

    # Demo lever: tools listed here fail on every call, standing in for an
    # outage that forces the planner to route around a blocked task.
    outage_tools: tuple[str, ...] = ()

    @property
    def checkpoint_db(self) -> Path:
        return self.workspace / "checkpoints.db"

    @property
    def index_db(self) -> Path:
        return self.workspace / "runs.db"

    @property
    def semantic_db(self) -> Path:
        return self.workspace / "semantic.db"

    @property
    def trace_dir(self) -> Path:
        return self.workspace / "traces"

    @property
    def report_dir(self) -> Path:
        return self.workspace / "reports"

    @property
    def demo_db(self) -> Path:
        return self.data_dir / "demo.db"

    @property
    def policy_dir(self) -> Path:
        return self.data_dir / "policies"

    def ensure_dirs(self) -> None:
        """Create every directory the platform writes to."""
        for path in (self.workspace, self.trace_dir, self.report_dir):
            path.mkdir(parents=True, exist_ok=True)
