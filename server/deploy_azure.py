"""Prepares an Azure App Service deploy without touching Azure. Two jobs:

  python3 server/deploy_azure.py package
      -> dist/lifeline-server.zip: an allowlist of server code and static files only, so
         server/.env, local rosters and incident files can never be uploaded.

  python3 server/deploy_azure.py vm-env --public-url https://<host> [--demo]
      -> dist/lifeline.env (mode 600): the same for the VM (Caddy in front), as a server/.env file.

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
import secrets
import zipfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent
DIST = SERVER.parent / "dist"
NOT_DEPLOYED = {"test_domain.py", "smoke_test.py", "deploy_azure.py", "seed_demo_history.py", "storm_drill.py"}
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
# docs/pitch/demo-script.md. Assume-power lets the one real unit run among synthetic homes that never report.
STAGE_DEMO = {"ESCALATE_AFTER_SECONDS": "12", "LIFELINE_STORM_WATCH": "demo", "LIFELINE_DEMO_ASSUME_POWER": "1"}
# The VM: Caddy on the same machine proxies 443 to 8080 and overwrites X-Real-IP with the caller's address.
# Caddy on the same VM overwrites X-Real-IP; loopback-only bind means only Caddy can set it.
VM_SETTINGS = {"PORT": "8080", "LIFELINE_BIND": "127.0.0.1", "LIFELINE_CLIENT_IP_HEADER": "X-Real-IP"}
APP_SERVICE_ONLY = ("WEBSITES_PORT", "SCM_DO_BUILD_DURING_DEPLOYMENT")

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


def hosted_values(public_url: str, demo: bool) -> dict[str, str]:
    if not public_url.startswith("https://") or public_url.rstrip("/") != public_url:
        raise SystemExit("--public-url must be https://<host> with no trailing slash")
    dotenv = read_dotenv(SERVER / ".env")
    missing = [key for key in REQUIRED if not dotenv.get(key)]
    if missing:
        raise SystemExit(f"server/.env is missing {', '.join(missing)} (generate secrets with "
                         "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    values = dict(SETTINGS)
    values.update({key: dotenv[key] for key in FROM_DOTENV if dotenv.get(key)})
    values.update(STAGE_DEMO if demo else {})
    values["LIFELINE_PUBLIC_URL"] = public_url
    return values


def write_private(target: Path, text: str) -> None:
    DIST.mkdir(exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)


def settings(public_url: str, demo: bool) -> Path:
    values = hosted_values(public_url, demo)
    target = DIST / "azure-settings.json"
    write_private(target, json.dumps([{"name": k, "value": v, "slotSetting": False} for k, v in values.items()]))
    print(f"{target}: {len(values)} settings: {', '.join(sorted(values))}")
    print("values not shown. Apply with -o none, then delete the file.")
    return target


def storm_settings(public_url: str, admin_email: str) -> Path:
    """The storm-demo board: its own local-file data under /home (persistent on App Service, untouched
    by zip deploys), synthetic homes only, nothing copied from server/.env, so it can never reach the
    real Supabase project or the ntfy topic. Outages stay open without heartbeats (no laptop needed
    once server/storm_drill.py has run)."""
    if not public_url.startswith("https://") or public_url.rstrip("/") != public_url:
        raise SystemExit("--public-url must be https://<host> with no trailing slash")
    password = secrets.token_urlsafe(18)
    values = {**SETTINGS, **STAGE_DEMO,
              "LIFELINE_PUBLIC_URL": public_url,
              "LIFELINE_NOTIFIER": "console",           # synthetic numbers only; nothing leaves the server
              "LIFELINE_ADMIN_PASSWORD": password,
              "LIFELINE_BOOTSTRAP_ADMIN": admin_email,
              "LIFELINE_PATIENTS": "/home/site/storm-patients.json",
              "LIFELINE_OUTAGES": "/home/site/storm-outages.json",
              "LIFELINE_STAFF": "/home/site/storm-staff.json",
              "LIFELINE_SESSION_SECRET": secrets.token_urlsafe(32),
              "LIFELINE_RESPOND_SECRET": secrets.token_urlsafe(32),
              "ESCALATE_AFTER_SECONDS": "120",
              "OFFLINE_AFTER_SECONDS": "604800",
              "SILENT_ALERT_AFTER_SECONDS": "604800",
              "LIFELINE_EMPOWER": "on"}
    target = DIST / "azure-storm-settings.json"
    write_private(target, json.dumps([{"name": k, "value": v, "slotSetting": False} for k, v in values.items()]))
    write_private(DIST / "storm-admin-password.txt", password + "\n")
    print(f"{target}: {len(values)} settings: {', '.join(sorted(values))}")
    print(f"sign-in password for {admin_email}: {DIST / 'storm-admin-password.txt'} (mode 600, not shown)")
    return target


def vm_env(public_url: str, demo: bool) -> Path:
    """The server/.env for the VM, in the format app.load_dotenv reads."""
    values = {k: v for k, v in hosted_values(public_url, demo).items() if k not in APP_SERVICE_ONLY}
    values.update(VM_SETTINGS)
    if any("\n" in v for v in values.values()):
        raise SystemExit("a value in server/.env spans lines; fix it there first")
    target = DIST / "lifeline.env"
    write_private(target, "".join(f"{k}={v}\n" for k, v in values.items()))
    print(f"{target}: {len(values)} settings: {', '.join(sorted(values))}")
    print("values not shown. Copy it to the VM, then delete it here.")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("package", help="build dist/lifeline-server.zip")
    settings_parser = commands.add_parser("settings", help="write dist/azure-settings.json")
    settings_parser.add_argument("--public-url", required=True, help="https://<defaultHostName>, no trailing slash")
    settings_parser.add_argument("--demo", action="store_true", help="stage settings: 12 s escalation, Storm Watch demo")
    vm_parser = commands.add_parser("vm-env", help="write dist/lifeline.env for the VM")
    vm_parser.add_argument("--public-url", required=True, help="https://<VM DNS name>, no trailing slash")
    vm_parser.add_argument("--demo", action="store_true", help="stage settings: 12 s escalation, Storm Watch demo")
    storm_parser = commands.add_parser("storm-settings", help="write dist/azure-storm-settings.json (storm-demo board)")
    storm_parser.add_argument("--public-url", required=True, help="https://<storm app defaultHostName>")
    storm_parser.add_argument("--admin", required=True, help="email that signs in as admin")
    args = parser.parse_args()
    if args.command == "storm-settings":
        storm_settings(args.public_url, args.admin)
    elif args.command == "package":
        package()
    elif args.command == "settings":
        settings(args.public_url, args.demo)
    else:
        vm_env(args.public_url, args.demo)


if __name__ == "__main__":
    main()
