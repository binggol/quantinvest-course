from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_powershell_scripts_parse_in_windows_powershell():
    scripts = sorted((ROOT / "scripts").rglob("*.ps1"))
    assert scripts

    command = r"""
$errorsFound = @()
Get-ChildItem -LiteralPath $env:QI_PS_TEST_ROOT -Recurse -Filter *.ps1 | ForEach-Object {
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile(
    $_.FullName,
    [ref]$tokens,
    [ref]$errors
  ) | Out-Null
  if ($errors.Count) {
    $errorsFound += "$($_.FullName): $($errors.Message -join ' | ')"
  }
}
if ($errorsFound.Count) {
  $errorsFound | ForEach-Object { Write-Error $_ }
  exit 1
}
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        env={**os.environ, "QI_PS_TEST_ROOT": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_manual_batch_runner_fails_closed():
    source = (ROOT / "scripts" / "run_batch_models.ps1").read_text(encoding="utf-8")

    assert '$ErrorActionPreference = "Stop"' in source
    assert "Batch contains unsupported characters" in source
    assert "Unsupported model" in source
    assert "if ($failed.Count)" in source
    assert "exit 1" in source
