#!/usr/bin/env python3
"""Build the Apple Silicon test .app and a portable ZIP, without developer paths."""
from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
DIST = ROOT / "dist"


def run(*args: str, env: dict | None = None) -> None:
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def main() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Build this release on an Apple Silicon Mac")
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["CLANG_MODULE_CACHE_PATH"] = str(BUILD / "clang-cache")
    env["SWIFTPM_MODULECACHE_OVERRIDE"] = str(BUILD / "module-cache")
    run("swift", "build", "--disable-sandbox", "--package-path", "native", "--scratch-path", str(BUILD / "native"),
        "--cache-path", str(BUILD / "swift-cache"), "--config-path", str(BUILD / "swift-config"),
        "--security-path", str(BUILD / "swift-security"), "-c", "release", env=env)
    env["PYINSTALLER_CONFIG_DIR"] = str(BUILD / "pyinstaller-cache")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "threadgoon-engine", "--target-arch", "arm64", "--paths", str(ROOT),
        "--exclude-module", "textual", "--exclude-module", "pytest", "--exclude-module", "tkinter",
        "--distpath", str(BUILD / "frozen"), "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD), "packaging/engine_entry.py", env=env)
    app = DIST / "Threadgoon.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(BUILD / "native" / "release" / "Threadgoon", macos / "Threadgoon")
    helper = BUILD / "frozen" / "threadgoon-engine"
    embedded = resources / "engine"
    embedded.mkdir()
    for item in helper.iterdir():
        if item.is_dir():
            shutil.copytree(item, embedded / item.name, symlinks=True)
        else:
            shutil.copy2(item, embedded / item.name)
    plist = {
        "CFBundleName": "Threadgoon", "CFBundleDisplayName": "Threadgoon",
        "CFBundleIdentifier": "org.threadgoon.desktop", "CFBundleExecutable": "Threadgoon",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "6.1.0",
        "CFBundleDevelopmentRegion": "ru", "CFBundleLocalizations": ["ru", "en"],
        "CFBundleVersion": "610", "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Threadgoon contributors. MIT license.",
    }
    iconset = BUILD / "Threadgoon.iconset"
    iconset.mkdir(exist_ok=True)
    run("swift", "packaging/make_icon.swift", str(iconset), env=env)
    # Modern ICNS stores PNG payloads directly; avoid iconutil's external conversion service.
    chunks = []
    for kind, filename in [(b"icp4", "icon_16x16.png"), (b"icp5", "icon_32x32.png"),
                           (b"icp6", "icon_32x32@2x.png"), (b"ic07", "icon_128x128.png"),
                           (b"ic08", "icon_256x256.png"), (b"ic09", "icon_512x512.png"),
                           (b"ic10", "icon_512x512@2x.png")]:
        payload = (iconset / filename).read_bytes()
        chunks.append(struct.pack(">4sI", kind, len(payload) + 8) + payload)
    payload = b"".join(chunks)
    (resources / "Threadgoon.icns").write_bytes(struct.pack(">4sI", b"icns", len(payload) + 8) + payload)
    plist["CFBundleIconFile"] = "Threadgoon"
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump(plist, stream)
    shutil.copy2(ROOT / "LICENSE", resources / "LICENSE")
    shutil.copy2(ROOT / "README.md", resources / "README.md")
    # Test distribution only. Developer ID signing/notarization is a separate release step.
    run("codesign", "--force", "--sign", "-", "--timestamp=none", str(app))
    run("codesign", "--verify", "--deep", "--strict", str(app))
    archive = DIST / "Threadgoon-6.1.0-arm64.zip"
    if archive.exists():
        archive.unlink()
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive))
    print(f"Built {app}\nPortable test archive: {archive}")


if __name__ == "__main__":
    main()
