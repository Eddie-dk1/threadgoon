#!/usr/bin/env python3
"""Run Swift Testing with either Xcode or the standalone Command Line Tools."""
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
build = ROOT / ".build"
env = dict(os.environ)
env["THREADGOON_DEV_ROOT"] = str(ROOT)
env["CLANG_MODULE_CACHE_PATH"] = str(build / "clang-cache")
env["SWIFTPM_MODULECACHE_OVERRIDE"] = str(build / "module-cache")
developer = Path(subprocess.check_output(["xcode-select", "-p"], text=True).strip())
frameworks = [developer / "Library/Developer/Frameworks",
              developer / "Platforms/MacOSX.platform/Developer/Library/Frameworks"]
command = ["swift", "test", "--disable-xctest", "--enable-swift-testing", "--disable-sandbox",
           "--package-path", "native", "--scratch-path", str(build / "native-tests"),
           "--cache-path", str(build / "swift-cache"), "--config-path", str(build / "swift-config"),
           "--security-path", str(build / "swift-security")]
for path in frameworks:
    if path.exists():
        command += ["-Xswiftc", f"-F{path}", "-Xlinker", f"-F{path}", "-Xlinker", "-rpath", "-Xlinker", str(path)]
interop = developer / "Library/Developer/usr/lib"
if interop.exists():
    command += ["-Xlinker", "-rpath", "-Xlinker", str(interop)]
subprocess.run(command, cwd=ROOT, env=env, check=True)
