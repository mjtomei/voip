#!/usr/bin/env python3
"""Build pinned Baresip locally; never install system packages or use sudo."""
import ctypes.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
PREFIX = ROOT / ".tools"
VERSION = "4.11.0"
CHECKSUMS = {
    "re": "2a8cbf13719618d879464617512a80fe2c13fe63cd5461cf01a195fbe46b3ca4",
    "baresip": "e170ad5857994dfed0c84c4c04eb904fa410f3ec2d5a6c789b50b3fda47ba98c",
}


def run(*args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def package(name, destination):
    downloads = BUILD / "downloads"
    run("apt-get", "download", name, cwd=downloads)
    matches = list(downloads.glob(name + "_*.deb"))
    if len(matches) != 1:
        raise SystemExit(f"Expected one {name} package in {downloads}; remove obsolete copies")
    destination.mkdir(parents=True, exist_ok=True)
    run("dpkg-deb", "-x", matches[0], destination)


def source(project):
    archive = BUILD / "downloads" / f"{project}-{VERSION}.tar.gz"
    if not archive.exists():
        url = f"https://codeload.github.com/baresip/{project}/tar.gz/refs/tags/v{VERSION}"
        with urllib.request.urlopen(url, timeout=60) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != CHECKSUMS[project]:
        raise SystemExit("Archive checksum mismatch: " + str(archive))
    destination = BUILD / "sources"
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")
    return destination / f"{project}-{VERSION}"


def main():
    for tool in ("cmake", "cc", "pkg-config", "apt-get", "dpkg-deb", "patch"):
        if not shutil.which(tool):
            raise SystemExit("Install build prerequisites: build-essential cmake pkg-config libssl-dev")
    run("pkg-config", "--exists", "openssl")
    (BUILD / "downloads").mkdir(parents=True, exist_ok=True)
    (PREFIX / "bin").mkdir(parents=True, exist_ok=True)
    pulse_include = Path("/usr/include")
    if not (pulse_include / "pulse" / "pulseaudio.h").exists():
        destination = BUILD / "pulse-dev"
        if not (destination / "usr/include/pulse/pulseaudio.h").exists():
            package("libpulse-dev", destination)
        pulse_include = destination / "usr/include"
    triplet = subprocess.check_output(["cc", "-print-multiarch"], text=True).strip()
    pulse_library = Path("/usr/lib") / triplet / "libpulse.so.0"
    if not pulse_library.exists() or not ctypes.util.find_library("pulse"):
        raise SystemExit("Install libpulse0 and pipewire-pulse first")
    if not shutil.which("pactl"):
        destination = BUILD / "pulse-utils"
        if not (destination / "usr/bin/pactl").exists():
            package("pulseaudio-utils", destination)
        for tool in ("pactl", "pacat", "parec", "paplay"):
            # Existing audio tools may be running; replace their directory
            # entries instead of truncating an executable in use (ETXTBSY).
            staged = PREFIX / "bin" / (tool + ".new")
            shutil.copy2(destination / "usr/bin" / tool, staged)
            os.replace(staged, PREFIX / "bin" / tool)
    common = [f"-DCMAKE_INSTALL_PREFIX={PREFIX}", "-DCMAKE_INSTALL_LIBDIR=lib",
              "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib;$ORIGIN/../.."]
    for project in ("re", "baresip"):
        src = source(project)
        if project == "re":
            # source() extracts the verified pristine archive on every build.
            run("patch", "--batch", "--fuzz=0", "-p1", "-i",
                ROOT / "patches/re-twilio-tls-keepalive.patch", cwd=src)
            run("patch", "--batch", "--fuzz=0", "-p1", "-i",
                ROOT / "patches/re-registration-refresh-guard.patch", cwd=src)
        else:
            run("patch", "--batch", "--fuzz=0", "-p1", "-i",
                ROOT / "patches/baresip-registration-refresh.patch", cwd=src)
        output = BUILD / (project + "-build")
        extra = (["-DLIBRE_BUILD_STATIC=OFF"] if project == "re" else
                 [f"-DCMAKE_PREFIX_PATH={PREFIX}",
                  f"-DPULSE_INCLUDE_DIR={pulse_include}", f"-DPULSE_LIBRARY={pulse_library}",
                  "-DMODULES=account;auconv;auresamp;ctrl_tcp;g711;menu;netroam;pulse;srtp;stdio;uuid"])
        run("cmake", "-S", src, "-B", output, *common, *extra)
        run("cmake", "--build", output, "--parallel", str(min(os.cpu_count() or 2, 4)))
        run("cmake", "--install", output)
    for module in ("pulse", "srtp", "g711", "account", "menu", "ctrl_tcp"):
        if not (PREFIX / "lib/baresip/modules" / (module + ".so")).is_file():
            raise SystemExit("Required module was not built: " + module)
    # Exercise dynamic-library loading as well as checking installed files.
    help_result = subprocess.run([str(PREFIX / "bin/baresip"), "-h"],
                                 capture_output=True, text=True, timeout=10)
    # Baresip 4.11.0 deliberately returns -2 (shell status 254) for --help.
    if help_result.returncode != 254 or "Usage: baresip" not in help_result.stderr:
        raise SystemExit("Installed Baresip cannot start: " + help_result.stderr.strip())
    print("Installed Baresip and audio tools only inside " + str(PREFIX))


if __name__ == "__main__":
    main()
