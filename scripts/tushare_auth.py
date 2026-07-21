"""Load the Tushare credential without embedding it in source code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRET_FILES = (
    PROJECT_ROOT / "data" / ".tushare_token",
    Path(r"C:\rdagent\data\.tushare_token"),
    Path("/run/secrets/tushare_token"),
)


def get_tushare_token(secret_files: Iterable[Path | str] | None = None) -> str:
    """Return the runtime token, preferring the environment over secret files."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token

    candidates: list[Path] = []
    configured_file = os.environ.get("TUSHARE_TOKEN_FILE", "").strip()
    if configured_file:
        candidates.append(Path(configured_file))
    candidates.extend(Path(path) for path in (secret_files or DEFAULT_SECRET_FILES))

    errors: list[str] = []
    for path in candidates:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if token:
            return token
        errors.append(f"{path}: file is empty")

    checked = ", ".join(str(path) for path in candidates) or "no secret files"
    detail = f"; read errors: {'; '.join(errors)}" if errors else ""
    raise RuntimeError(
        "TUSHARE_TOKEN is not set and no readable, non-empty Tushare secret "
        f"file was found (checked: {checked}){detail}"
    )
