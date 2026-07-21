import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

try:
    from scripts.tushare_auth import get_tushare_token
except ModuleNotFoundError:
    from tushare_auth import get_tushare_token


ROOT = Path(__file__).resolve().parents[1]
TOKEN_LITERAL = re.compile(r"(?i)(?<![a-z0-9])[a-z0-9]{56}(?![a-z0-9])")


def test_environment_token_takes_precedence() -> None:
    with TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"TUSHARE_TOKEN": "runtime-token"}, clear=True
    ):
        secret_file = Path(tmp) / ".tushare_token"
        secret_file.write_text("file-token", encoding="utf-8")
        assert get_tushare_token([secret_file]) == "runtime-token"


def test_secret_file_is_supported() -> None:
    with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
        secret_file = Path(tmp) / ".tushare_token"
        secret_file.write_text("file-token\n", encoding="utf-8")
        assert get_tushare_token([secret_file]) == "file-token"


def test_missing_token_has_actionable_error() -> None:
    with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
        missing = Path(tmp) / ".tushare_token"
        with pytest.raises(RuntimeError, match="TUSHARE_TOKEN is not set"):
            get_tushare_token([missing])


def test_source_tree_has_no_tushare_shaped_literal() -> None:
    files = [path for path in ROOT.iterdir() if path.is_file()]
    for directory in (ROOT / "scripts", ROOT / "config", ROOT / "docs", ROOT / "research"):
        if directory.exists():
            files.extend(path for path in directory.rglob("*") if path.is_file())

    suffixes = {".py", ".ps1", ".md", ".patch", ".yml", ".yaml", ".example"}
    leaked = []
    for path in files:
        if path.suffix.lower() not in suffixes and path.name != "Dockerfile":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if TOKEN_LITERAL.search(content):
            leaked.append(str(path.relative_to(ROOT)))

    assert leaked == [], f"Tushare-shaped credential literals found in: {leaked}"
