#!/usr/bin/env python3
"""Verify the prospective public tree without reading ignored private state."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE_EXPRESSION = "GPL-3.0-only OR LicenseRef-TradingOPS-Commercial-1.0"
REQUIRED_FILES = {
    "LICENSE",
    "NOTICE",
    "CLA.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    "sbom.cdx.json",
    "README.md",
    "README.zh-CN.md",
    "docs/API_QUICKSTART.md",
    "docs/AI_API_QUICKSTART.md",
}
SECRET_NAME = re.compile(
    r"(?:TOKEN|PASSWORD|SECRET|PRIVATE_KEY|API_KEY|SIGNING_SECRET|ENCRYPTION_KEY)$"
)
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")
REPOSITORY_PLACEHOLDER = re.compile("OWNER" + "/" + "REPOSITORY" + "|" + "REPOSITORY" + "_" + "URL")
REQUIRED_IDENTITY_TEXT = {
    "LICENSE": ("COPYRIGHT_HOLDER", "COMMERCIAL_EMAIL"),
    "NOTICE": ("COPYRIGHT_HOLDER", "COMMERCIAL_EMAIL"),
    "SECURITY.md": ("SECURITY_EMAIL", "nineheavens223-sys/TradeOps"),
    "SUPPORT.md": ("COMMERCIAL_EMAIL", "SECURITY_EMAIL"),
    "README.md": ("nineheavens223-sys/TradeOps.git",),
    "README.zh-CN.md": ("nineheavens223-sys/TradeOps.git",),
}
FORBIDDEN_GATES = {
    "TRADING_AUTO_ADD_ENABLED",
    "TRADING_AUTO_OPERATING_REFILL_ENABLED",
    "TRADING_AUTO_PROFIT_SWEEP_ENABLED",
    "TRADING_CAPITAL_TRANSFER_ENABLED",
    "TRADING_FREQTRADE_LIVE_ORDER_SEND_ENABLED",
    "TRADING_BINANCE_LIVE_ORDER_SEND_ENABLED",
    "TRADING_HYPERLIQUID_LIVE_ORDER_SEND_ENABLED",
}


def _candidate_paths() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required")
    output = subprocess.check_output(  # noqa: S603 - resolved git, fixed arguments
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    paths: list[Path] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        source = ROOT / relative
        if source.is_file() or source.is_symlink():
            paths.append(relative)
    return sorted(set(paths))


def _copy_candidate(destination: Path, paths: list[Path]) -> None:
    for relative in paths:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            raise RuntimeError(f"public candidate contains symlink: {relative}")
        shutil.copy2(source, target)


def _text_files(paths: list[Path]):
    for relative in paths:
        source = ROOT / relative
        try:
            yield relative, source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def _verify_static(paths: list[Path]) -> None:
    available = {path.as_posix() for path in paths}
    missing = sorted(REQUIRED_FILES - available)
    if missing:
        raise RuntimeError("missing public files: " + ", ".join(missing))

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if f'license = "{LICENSE_EXPRESSION}"' not in pyproject:
        raise RuntimeError("pyproject.toml license expression is not the public dual license")

    absolute_path_hits = [
        str(relative) for relative, content in _text_files(paths) if PRIVATE_PATH.search(content)
    ]
    if absolute_path_hits:
        raise RuntimeError(
            "personal absolute paths in public candidate: " + ", ".join(absolute_path_hits)
        )

    repository_placeholder_hits = [
        str(relative)
        for relative, content in _text_files(paths)
        if REPOSITORY_PLACEHOLDER.search(content)
    ]
    if repository_placeholder_hits:
        raise RuntimeError(
            "unresolved repository placeholders: " + ", ".join(repository_placeholder_hits)
        )

    for filename, required_values in REQUIRED_IDENTITY_TEXT.items():
        content = (ROOT / filename).read_text(encoding="utf-8")
        missing_values = [value for value in required_values if value not in content]
        if missing_values:
            raise RuntimeError(
                f"{filename} is missing publication identity values: " + ", ".join(missing_values)
            )

    env_values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        env_values[name.strip()] = value.strip()
    populated_secrets = sorted(
        name for name, value in env_values.items() if value and SECRET_NAME.search(name)
    )
    if populated_secrets:
        raise RuntimeError(
            ".env.example contains populated secret fields: " + ", ".join(populated_secrets)
        )
    unsafe_gates = sorted(
        name for name in FORBIDDEN_GATES if env_values.get(name, "false").lower() != "false"
    )
    if unsafe_gates:
        raise RuntimeError("dangerous public gates are not disabled: " + ", ".join(unsafe_gates))


def _run_gitleaks(binary: Path, snapshot: Path) -> None:
    subprocess.run(  # noqa: S603 - user-selected scanner binary, fixed arguments
        [
            str(binary),
            "dir",
            str(snapshot),
            "--config",
            str(snapshot / ".gitleaks.toml"),
            "--redact=100",
            "--no-banner",
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitleaks-bin", type=Path)
    parser.add_argument("--keep-snapshot", type=Path)
    args = parser.parse_args()
    paths = _candidate_paths()
    _verify_static(paths)
    with tempfile.TemporaryDirectory(prefix="tradingops-public-") as temporary:
        snapshot = Path(temporary)
        _copy_candidate(snapshot, paths)
        if args.gitleaks_bin:
            _run_gitleaks(args.gitleaks_bin.resolve(), snapshot)
        if args.keep_snapshot:
            destination = args.keep_snapshot.resolve()
            if destination.exists():
                raise RuntimeError(f"snapshot destination already exists: {destination}")
            shutil.copytree(snapshot, destination)
    total_bytes = sum((ROOT / path).stat().st_size for path in paths)
    print(f"public candidate verified: {len(paths)} files, {total_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
