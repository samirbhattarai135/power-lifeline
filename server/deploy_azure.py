"""Prepares an Azure App Service deploy without touching Azure. Two jobs:

  python3 server/deploy_azure.py package
      -> dist/lifeline-server.zip: an allowlist of server code and static files only, so
         server/.env, local rosters and incident files can never be uploaded.

  python3 server/deploy_azure.py settings --public-url https://<host> [--demo]
      -> dist/azure-settings.json (mode 600): App Service settings, secrets read from
         server/.env without printing them. Apply with
         az webapp config appsettings set ... --settings @dist/azure-settings.json -o none
         (-o none matters: without it the CLI prints every value), then delete the file.

Runbook: docs/ops/deploy.md.
"""

from __future__ import annotations

import argparse
import json
import os
import zipfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent
DIST = SERVER.parent / "dist"
NOT_DEPLOYED = {"test_domain.py", "smoke_test.py", "deploy_azure.py", "seed_demo_history.py"}
PORT = "8000"  # the built-in Python container's port; App Service forwards 443 to it

# Plain settings: safe to show, the same for every deploy.
SETTINGS = {
    "PORT": PORT,
    "WEBSITES_PORT": PORT,
    "SCM_DO_BUILD_DURING_DEPLOYMENT": "false",  # stdlib only: nothing to pip install
    "LIFELINE_NOTIFIER": "ntfy",                # never "console" on a host: it logs phone numbers and links
    "LIFELINE_STORM_WATCH": "on",
    "LIFELINE_TIMEZONE": "America/Chicago",
    "ESCALATE_AFTER_SECONDS": "120",
    "OFFLINE_AFTER_SECONDS": "90",
    "SILENT_ALERT_AFTER_SECONDS": "300",
}
STAGE_DEMO = {"ESCALATE_AFTER_SECONDS": "12", "LIFELINE_STORM_WATCH": "demo"}  # docs/pitch/demo-script.md

# Copied from server/.env when present. LIFELINE_PUBLIC_URL is not: in .env it is the laptop's address.
FROM_DOTENV = ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN",
               "LIFELINE_SESSION_SECRET", "LIFELINE_RESPOND_SECRET", "LIFELINE_BOOTSTRAP_ADMIN", "NWS_USER_AGENT",
               "LIFELINE_COUNTY_CONTACT", "LIFELINE_DEMO_ASSUME_POWER", "LIFELINE_EMPOWER")
REQUIRED = ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "NTFY_TOPIC", "LIFELINE_SESSION_SECRET", "LIFELINE_RESPOND_SECRET")


def deployable_files() -> list[Path]:
    code = [p for p in SERVER.glob("*.py") if p.name not in NOT_DEPLOYED] + [SERVER / "startup.sh"]
    static = [p for p in (SERVER / "static").rglob("*") if p.is_file() and not p.name.startswith(".")]
    return sorted(code + static)


def package() -> Path:
    DIST.mkdir(exist_ok=True)
    target = DIST / "lifeline-server.zip"
    files = deployable_files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(SERVER).as_posix())
    names = zipfile.ZipFile(target).namelist()
    if not {"app.py", "startup.sh"} <= set(names) or any(Path(n).name.startswith(".env") or n.endswith(".json") and "/" not in n for n in names):
        raise SystemExit(f"refusing: {target} is missing app.py or contains a secret or data file")
    print(f"{target}: {len(names)} files, {target.stat().st_size // 1024} KB")
    return target


def read_dotenv(path: Path) -> dict[str, str]:
    """Same rules as app.load_dotenv, without touching os.environ."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def settings(public_url: str, demo: bool) -> Path:
    if not public_url.startswith("https://") or public_url.rstrip("/") != public_url:
        raise SystemExit("--public-url must be https://<host> with no trailing slash")
    dotenv = read_dotenv(SERVER / ".env")
    missing = [key for key in REQUIRED if not dotenv.get(key)]
    if missing:
        raise SystemExit(f"server/.env is missing {', '.join(missing)} (generate secrets with "
                         "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    values = {**SETTINGS, **(STAGE_DEMO if demo else {}), "LIFELINE_PUBLIC_URL": public_url}
    values.update({key: dotenv[key] for key in FROM_DOTENV if dotenv.get(key)})
    DIST.mkdir(exist_ok=True)
    target = DIST / "azure-settings.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump([{"name": k, "value": v, "slotSetting": False} for k, v in values.items()], out)
    print(f"{target}: {len(values)} settings: {', '.join(sorted(values))}")
    print("values not shown. Apply with -o none, then delete the file.")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("package", help="build dist/lifeline-server.zip")
    settings_parser = commands.add_parser("settings", help="write dist/azure-settings.json")
    settings_parser.add_argument("--public-url", required=True, help="https://<defaultHostName>, no trailing slash")
    settings_parser.add_argument("--demo", action="store_true", help="stage settings: 12 s escalation, Storm Watch demo")
    args = parser.parse_args()
    if args.command == "package":
        package()
    else:
        settings(args.public_url, args.demo)


if __name__ == "__main__":
    main()
