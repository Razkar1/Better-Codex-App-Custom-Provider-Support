#!/usr/bin/env python3
"""Automatic provider-routing fallback for ChatGPT/Codex macOS build 7658.

Modern Desktop builds can still show custom models through model_catalog_json,
but the provider-picker UI patch no longer matches the current frontend layout.
This compatibility installer leaves the native model picker untouched and
routes providers at thread creation using the project's existing
~/.codex/desktop-model-providers.json file.

The routing file is re-read for every new thread. Exact model slugs use
model_providers; unmapped models use default_provider. Existing threads keep the
provider they started with.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import sys
import tempfile

from patch_chatgpt_providers import (
    PatchError,
    asar_header_hash,
    asar_integrity_hash,
    atomic_replace_file,
    contains_marker,
    ensure_provider_config,
    invoking_user_home,
    load_plist,
    make_backup,
    restore_backup,
    run,
    stop_target_app_processes,
)

PATCH_MARKER = b"__codexDesktopRequestProviderRoutingBuild7658"
ASAR_PACKAGE = "@electron/asar@3.4.1"

OLD_SEND_REQUEST = (
    "async sendRequest(e,t,n){if(this.dispatchMessage==null)throw Error("
    "`AppServerRequestClient is missing a message dispatcher`);"
    "return e===`config/read`?this.sendConfigReadRequest(t,n):"
    "this.enqueueRequest(e,t,e===`plugin/list`&&n?.timeoutMs==null?"
    "{...n,timeoutMs:gCn}:n)}"
)

NEW_SEND_REQUEST = (
    "async sendRequest(e,t,n){if(this.dispatchMessage==null)throw Error("
    "`AppServerRequestClient is missing a message dispatcher`);"
    "/*__codexDesktopRequestProviderRoutingBuild7658*/"
    "e===`thread/list`&&(t==null||typeof t!==`object`?t={modelProviders:[]}:"
    "t.modelProviders==null&&(t={...t,modelProviders:[]}));"
    "if(e===`thread/start`&&t!=null&&typeof t===`object`&&t.modelProvider==null)try{"
    "let{codexHome:r}=await DD(`codex-home`,{params:{hostId:this.hostId}}),"
    "i=r.includes(`\\\\`)&&!r.includes(`/`)?`\\\\`:`/`,"
    "a=`${r.replace(/[\\\\/]+$/u,``)}${i}desktop-model-providers.json`,"
    "{contents:o}=await DD(`read-file`,{params:{hostId:this.hostId,path:a}}),"
    "s=JSON.parse(o),c=s?.model_providers?.[t.model]??s?.default_provider;"
    "typeof c===`string`&&c.length>0&&(t={...t,modelProvider:c})"
    "}catch{}"
    "return e===`config/read`?this.sendConfigReadRequest(t,n):"
    "this.enqueueRequest(e,t,e===`plugin/list`&&n?.timeoutMs==null?"
    "{...n,timeoutMs:gCn}:n)}"
)

OLD_PREWARM_PREFIX = (
    "async prewarmThreadStart(e,t){if(this.dispatchMessage==null)throw Error("
    "`AppServerRequestClient is missing a message dispatcher`);"
    "let n=t?.priority??`critical`,r=rE(`thread/start`,t?.source)"
)

NEW_PREWARM_PREFIX = (
    "async prewarmThreadStart(e,t){if(this.dispatchMessage==null)throw Error("
    "`AppServerRequestClient is missing a message dispatcher`);"
    "if(e!=null&&typeof e===`object`&&e.modelProvider==null)try{"
    "let{codexHome:n}=await DD(`codex-home`,{params:{hostId:this.hostId}}),"
    "r=n.includes(`\\\\`)&&!n.includes(`/`)?`\\\\`:`/`,"
    "i=`${n.replace(/[\\\\/]+$/u,``)}${r}desktop-model-providers.json`,"
    "{contents:a}=await DD(`read-file`,{params:{hostId:this.hostId,path:i}}),"
    "o=JSON.parse(a),s=o?.model_providers?.[e.model]??o?.default_provider;"
    "typeof s===`string`&&s.length>0&&(e={...e,modelProvider:s})"
    "}catch{}"
    "let n=t?.priority??`critical`,r=rE(`thread/start`,t?.source)"
)


def parse_args() -> argparse.Namespace:
    home = invoking_user_home()
    configured_codex_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_codex_home).expanduser()
        if configured_codex_home
        else home / ".codex"
    )
    parser = argparse.ArgumentParser(
        description=(
            "Install automatic custom-provider routing for ChatGPT/Codex "
            "26.901.20858 build 7658."
        )
    )
    parser.add_argument(
        "--app",
        type=Path,
        default=Path("/Applications/ChatGPT.app"),
        help="ChatGPT.app to patch (default: /Applications/ChatGPT.app)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=codex_home / "desktop-model-providers.json",
        help="Provider-routing JSON in the effective Codex home",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=home / "Applications" / "ChatGPT Patch Backups",
        help="Directory for complete app backups",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify configuration and build compatibility without modifying the app",
    )
    return parser.parse_args()


def validate_no_global_provider(config_path: Path) -> None:
    codex_config = config_path.parent / "config.toml"
    if not codex_config.is_file():
        raise PatchError(f"Missing Codex config: {codex_config}")
    current_section = None
    for raw_line in codex_config.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line
            continue
        if current_section is None and re.match(r"^model_provider\s*=", line):
            raise PatchError(
                "Remove the top-level `model_provider = ...` line from config.toml; "
                "this fallback selects the provider per new thread."
            )


def find_request_bundle(assets: Path) -> Path:
    matches = []
    for path in sorted(assets.glob("*.js")):
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if OLD_SEND_REQUEST in source and OLD_PREWARM_PREFIX in source:
            matches.append(path)
    if len(matches) != 1:
        raise PatchError(
            "Expected exactly one build-7658 request bundle, found "
            f"{len(matches)}. The app build is unsupported, updated, or already modified."
        )
    return matches[0]


def patch_request_bundle(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    send_count = source.count(OLD_SEND_REQUEST)
    prewarm_count = source.count(OLD_PREWARM_PREFIX)
    if send_count != 1 or prewarm_count != 1:
        raise PatchError(
            f"{path.name} does not match the verified build-7658 request layer "
            f"(sendRequest={send_count}, prewarmThreadStart={prewarm_count})"
        )
    patched = source.replace(OLD_SEND_REQUEST, NEW_SEND_REQUEST, 1).replace(
        OLD_PREWARM_PREFIX, NEW_PREWARM_PREFIX, 1
    )
    if patched.count("desktop-model-providers.json") < 2:
        raise PatchError("Provider-routing injection validation failed")
    path.write_text(patched, encoding="utf-8")


def patch_app(
    app: Path,
    provider_config: Path,
    backup_dir: Path,
    dry_run: bool,
) -> Path | None:
    info_path = app / "Contents" / "Info.plist"
    resources = app / "Contents" / "Resources"
    asar_path = resources / "app.asar"

    if sys.platform != "darwin":
        raise PatchError("This installer only supports macOS")
    if not app.is_dir() or not info_path.is_file() or not asar_path.is_file():
        raise PatchError(f"Not a supported ChatGPT app bundle: {app}")
    if shutil.which("npx") is None:
        raise PatchError("npx is required. Install Node.js, then run this installer again")

    ensure_provider_config(provider_config, overwrite=False)
    validate_no_global_provider(provider_config)

    info, plist_format = load_plist(info_path)
    version = str(info.get("CFBundleShortVersionString", "unknown"))
    build = str(info.get("CFBundleVersion", "unknown"))
    print(f"[APP] ChatGPT/Codex {version} (build {build})")

    if contains_marker(asar_path, PATCH_MARKER):
        print("[OK] Request-layer provider routing is already installed.")
        return None

    current_hash = asar_header_hash(asar_path)
    expected_hash = asar_integrity_hash(info)
    if current_hash != expected_hash:
        raise PatchError(
            "The ASAR header does not match Info.plist integrity metadata. "
            "Restore or reinstall the official app before applying this patch."
        )
    print("[OK] Original ASAR integrity verified.")

    with tempfile.TemporaryDirectory(prefix="codex-provider-route-7658-") as temporary:
        work = Path(temporary)
        extracted = work / "app"
        patched_asar = work / "app.asar"
        patched_plist = work / "Info.plist"

        run(
            ["npx", "--yes", ASAR_PACKAGE, "extract", str(asar_path), str(extracted)],
            label="Extracting application resources",
        )
        assets = extracted / "webview" / "assets"
        if not assets.is_dir():
            raise PatchError("Extracted app has no webview/assets directory")

        target = find_request_bundle(assets)
        print(f"[OK] Matched verified request bundle: {target.name}")
        if dry_run:
            print("[OK] Dry run passed. This app is compatible with the fallback patch.")
            return None

        patch_request_bundle(target)
        run(
            ["npx", "--yes", ASAR_PACKAGE, "pack", str(extracted), str(patched_asar)],
            label="Packing patched application resources",
        )
        if not contains_marker(patched_asar, PATCH_MARKER):
            raise PatchError("Packed ASAR is missing the provider-routing marker")

        patched_header_hash = asar_header_hash(patched_asar)
        info["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"] = patched_header_hash
        with patched_plist.open("wb") as handle:
            plistlib.dump(info, handle, fmt=plist_format, sort_keys=False)

        backup = make_backup(app, backup_dir, version, build)
        print(f"[OK] Backup created: {backup}")
        live_mutation_started = False
        try:
            live_mutation_started = True
            atomic_replace_file(patched_asar, asar_path)
            atomic_replace_file(patched_plist, info_path)
            run(
                ["/usr/bin/codesign", "--deep", "--force", "--sign", "-", str(app)],
                label="Applying an ad-hoc app signature",
            )
            run(
                [
                    "/usr/bin/codesign",
                    "--verify",
                    "--deep",
                    "--strict",
                    "--verbose=2",
                    str(app),
                ],
                label="Verifying the app signature",
            )
            final_info, _ = load_plist(info_path)
            if asar_header_hash(asar_path) != asar_integrity_hash(final_info):
                raise PatchError("Installed ASAR integrity verification failed")
            if not contains_marker(asar_path, PATCH_MARKER):
                raise PatchError("Installed ASAR is missing the provider-routing marker")
        except Exception:
            if live_mutation_started:
                print(
                    "[RECOVERY] Patch failed after app mutation; restoring backup.",
                    file=sys.stderr,
                )
                try:
                    failed_copy = restore_backup(app, backup)
                    print(
                        f"[RECOVERY] Original app restored. Failed patched copy: {failed_copy}",
                        file=sys.stderr,
                    )
                except Exception as restore_exc:
                    print(
                        f"[RECOVERY ERROR] Automatic restore failed: {restore_exc}",
                        file=sys.stderr,
                    )
                    print(
                        f"[RECOVERY ERROR] Full backup remains at: {backup}",
                        file=sys.stderr,
                    )
            raise

    return backup


def main() -> int:
    args = parse_args()
    app = args.app.expanduser().resolve()
    provider_config = args.config.expanduser().resolve()
    backup_dir = args.backup_dir.expanduser().resolve()
    try:
        if not args.dry_run:
            stop_target_app_processes(app, allow_running=False)
        backup = patch_app(app, provider_config, backup_dir, args.dry_run)
    except PatchError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(
            f"\nERROR: Permission denied: {exc}\n"
            "If /Applications/ChatGPT.app is not writable by your account, "
            "rerun this same command with sudo.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    if args.dry_run:
        print("\nNo files were changed.")
        return 0

    print("\nSUCCESS")
    print(f"New threads now resolve modelProvider from: {provider_config}")
    print("Exact mappings use model_providers; unmapped models use default_provider.")
    print("The native model picker is left unchanged on this compatibility path.")
    if backup is not None:
        print(f"Backup: {backup}")
    print(
        "A future app update may replace this patch; this installer will refuse "
        "to patch a changed request-layer layout."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
