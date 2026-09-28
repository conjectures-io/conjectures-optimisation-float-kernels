"""Every service knob in one pydantic-settings object, so a bad value fails at startup."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import ClassVar

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VALIDATOR = Path(__file__).resolve().parent.parent

# The submitted Kernel.lean; the same cap the gate's intake stage enforces.
MAX_FILE_BYTES = 256_000
# The whole multipart body: the file, the form fields and their headers.
MAX_REQUEST_BYTES = 2 * MAX_FILE_BYTES
# A kernel at or below this ratio to the incumbent qualifies for promotion (proposed 3% rule).
PROMOTION_BAR = float(os.environ.get("PROMOTION_BAR", "0.97"))


class Settings(BaseSettings):
    # The API and worker tunables, read from SERVICE_* env vars.
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="SERVICE_", extra="ignore"
    )

    host: str = "0.0.0.0"
    port: int = 9300
    # Where each submission's Kernel.lean is written. Everything else is in Postgres.
    files: Path = VALIDATOR / ".work/submissions"

    # Signed writes allowed per hotkey per minute, counted in Postgres.
    rate_per_minute: int = Field(default=10, ge=1)
    rate_window_seconds: int = Field(default=60, ge=1)

    # Max signed-timestamp drift from our clock; outside it a recorded upload can't be replayed.
    signature_window_seconds: int = Field(default=300, ge=1)

    # Comma-separated Host allowlist; "*" accepts any, right behind a filtering proxy.
    allowed_hosts: str = "*"

    # This worker's id in the submissions table; carries the pid so two per box never clash.
    worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")

    # Age at which a claim counts as abandoned; must exceed the gate's timeout.
    stale_claim_seconds: float = Field(default=7200.0, gt=0)

    # Gate crashes on one submission before it ends in `error`, uncharged, off the queue.
    max_gate_errors: int = Field(default=3, ge=1)

    @field_validator("files")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        # Relative .env paths are relative to the repo, not the process's start directory.
        return value if value.is_absolute() else (VALIDATOR.parent / value).resolve()

    @property
    def host_allowlist(self) -> list[str]:
        # The Host header allow-list, from its comma-separated env form.
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    def submission_dir(self, sub_id: int) -> Path:
        # Where one submission's files are stored.
        return self.files / str(sub_id)


def load() -> Settings:
    # Read the settings from the environment once per process.
    return Settings()
