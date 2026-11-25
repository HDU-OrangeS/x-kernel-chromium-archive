#!/usr/bin/env python3
"""在 AArch64 QEMU 启动 x-kernel Chromium 存档。"""

import argparse
import fcntl
import hashlib
import json
import lzma
import os
import platform
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
REPOSITORY = "HDU-OrangeS/x-kernel-chromium-archive"
RELEASE = "archive-2026-11-24"
ASSET = "rootfs.img.xz"
MANIFEST = json.loads((ROOT / "rootfs.json").read_text())
SEED = MANIFEST["disk_sha256"]
ARCHIVE_SHA256 = MANIFEST["archive_sha256"]
DISK_BYTES = MANIFEST["disk_bytes"]
KERNELS = {
    "functional": "00623030a1d3634d1dc978b106ae7816bfd26fde34c6a90eed176585cd9b74ff",
    "performance": "5acb231235f7bbc85c79e25b8b8574f9cf66babd20ff96b924c63497a645e466",
    "virgl": "94ae37a2b343fd9831aefd23706d008bc4442c3b7d441966a1a64bbb6ee68d68",
}
VIRGL_LIBEXEC = "/usr/local/libexec/xkernel-virgl"
VIRGL_FILES = {
    "virgl-dev": ("/usr/local/bin/virgl-dev", "0100755"),
    "virgl-dev.py": (f"{VIRGL_LIBEXEC}/virgl-dev.py", "0100755"),
    "development_common.py": (f"{VIRGL_LIBEXEC}/development_common.py", "0100644"),
    "guest_webgl_probe.py": (f"{VIRGL_LIBEXEC}/guest_webgl_probe.py", "0100644"),
    "webgl-smoke.html": (f"{VIRGL_LIBEXEC}/webgl-smoke.html", "0100644"),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def user_directory(variable, default):
    return Path(os.environ.get(variable) or Path.home() / default) / "x-kernel-chromium-archive" / RELEASE


def verified_archive(local):
    if local is not None:
        if sha256(local) != ARCHIVE_SHA256:
            raise ValueError("指定的 rootfs 压缩包 SHA-256 与存档不一致。")
        return local

    cache = user_directory("XDG_CACHE_HOME", ".cache")
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ASSET
    with (cache / ".download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if archive.is_file() and sha256(archive) == ARCHIVE_SHA256:
            return archive
        client = shutil.which("gh")
        if client is None:
            raise ValueError("首次下载需要 GitHub CLI（gh）。也可用 --archive 指定已下载的镜像。")
        print("正在下载存档的根文件系统，完成后会检查 SHA-256。", flush=True)
        with tempfile.TemporaryDirectory(prefix="download-", dir=cache) as temporary:
            try:
                subprocess.run([client, "release", "download", RELEASE, "--repo", REPOSITORY,
                                "--pattern", ASSET, "--dir", temporary], check=True)
            except subprocess.CalledProcessError as error:
                raise ValueError("镜像下载失败。请检查 gh 登录状态与仓库访问权限。") from error
            downloaded = Path(temporary) / ASSET
            if sha256(downloaded) != ARCHIVE_SHA256:
                raise ValueError("下载的 rootfs 压缩包 SHA-256 与存档不一致。")
            downloaded.replace(archive)
    return archive


def install_virgl_helpers(disk, hashes):
    if not shutil.which("debugfs") or not shutil.which("e2fsck"):
        raise ValueError("首次启动 3D 版需要 e2fsprogs，以准备独立的客体磁盘。")

    commands = [f"mkdir {VIRGL_LIBEXEC}"]
    for name, (destination, mode) in VIRGL_FILES.items():
        commands.append(f"write {name} {destination}")
        commands.append(f"set_inode_field {destination} mode {mode}")
    subprocess.run(["debugfs", "-w", str(disk)], cwd=ROOT / "virgl",
                   input=("\n".join(commands) + "\n").encode(), capture_output=True, check=True)

    # debugfs can exit successfully when an individual command fails.
    for name, (destination, mode) in VIRGL_FILES.items():
        contents = subprocess.check_output(["debugfs", "-R", f"cat {destination}", str(disk)],
                                           stderr=subprocess.DEVNULL)
        info = subprocess.check_output(["debugfs", "-R", f"stat {destination}", str(disk)],
                                       stderr=subprocess.DEVNULL, text=True,
                                       env={**os.environ, "LC_ALL": "C"})
        mode_matches = f"Mode:  {mode[-4:]}" in info
        if hashlib.sha256(contents).hexdigest() != hashes[name] or not mode_matches:
            raise ValueError(f"3D 客体文件写入校验失败。{destination}")
    check = subprocess.run(["e2fsck", "-fn", str(disk)], capture_output=True, text=True)
    if check.returncode != 0:
        raise ValueError(f"3D 客体磁盘校验失败。{check.stdout}{check.stderr}")


def prepare_disk(archive, directory, identity):
    directory.parent.mkdir(parents=True, exist_ok=True)
    with (directory.parent / (directory.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if directory.exists():
            disk, marker = directory / "disk.img", directory / "identity.json"
            if disk.is_symlink() or not disk.is_file() or not marker.is_file():
                raise ValueError(f"运行目录缺少完整镜像，请指定新的 --workdir。{directory}")
            if json.loads(marker.read_text()) != identity or disk.stat().st_size != DISK_BYTES:
                raise ValueError(f"运行镜像与所选版本不匹配。{directory}")
            return

        if shutil.disk_usage(directory.parent).free < DISK_BYTES + 256 * 1024**2:
            raise ValueError("首次启动需要至少 2.25 GiB 可用磁盘空间。")
        with tempfile.TemporaryDirectory(prefix=directory.name + "-", dir=directory.parent) as temporary:
            checkout = Path(temporary)
            length = 0
            digest = hashlib.sha256()
            with lzma.open(archive, "rb") as source, (checkout / "disk.img").open("xb") as target:
                for chunk in iter(lambda: source.read(4 * 1024**2), b""):
                    length += len(chunk)
                    if length > DISK_BYTES:
                        raise ValueError("解压后镜像超出预期大小。")
                    digest.update(chunk)
                    target.write(chunk)
            if length != DISK_BYTES or digest.hexdigest() != SEED:
                raise ValueError("解压后镜像 SHA-256 不匹配。")
            if "virgl_helpers" in identity:
                install_virgl_helpers(checkout / "disk.img", identity["virgl_helpers"])
            (checkout / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
            checkout.rename(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", choices=KERNELS, nargs="?", default="functional")
    parser.add_argument("--archive", type=Path, help="已有的 rootfs.img.xz，用于离线启动")
    parser.add_argument("--workdir", type=Path, help="专用可写运行目录")
    parser.add_argument("--accel", choices=("tcg", "kvm"), default="tcg")
    parser.add_argument("--display", choices=("gtk", "none", "egl-headless"), default="gtk")
    parser.add_argument("--qmp", type=Path, help="创建一个私有的 QMP Unix socket")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="校验输入并打印 QEMU 命令")
    action.add_argument("--prepare-only", action="store_true", help="只准备运行磁盘")
    args = parser.parse_args()
    directory = (args.workdir or user_directory("XDG_STATE_HOME", ".local/state") / args.variant).resolve()
    if directory.is_relative_to(ROOT) or "," in str(directory):
        parser.error("运行目录必须位于仓库之外，路径中不能含逗号。")
    executable = shutil.which("qemu-system-aarch64")
    if not executable:
        raise ValueError("找不到 qemu-system-aarch64。Ubuntu 可安装 qemu-system-arm。")
    cpu = "cortex-a76"
    if args.accel == "kvm":
        if platform.machine() != "aarch64" or not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise ValueError("AArch64 KVM 需要 AArch64 宿主和可读写的 /dev/kvm。")
        cpu = "host"
    kernel = ROOT / "kernels" / (args.variant + ".bin")
    if sha256(kernel) != KERNELS[args.variant]:
        raise ValueError("内核与存档的冻结版本不匹配。")
    display = args.display
    gpu = "virtio-gpu-pci"
    memory = "2g"
    identity = {"variant": args.variant, "kernel_sha256": KERNELS[args.variant], "seed_sha256": SEED}
    if args.variant == "virgl":
        if display == "none":
            parser.error("3D 版需要 GTK OpenGL 或 egl-headless 显示后端。")
        if display == "gtk":
            display = "gtk,gl=on"
        gpu = "virtio-gpu-gl-pci"
        memory = "4g"
        identity["virgl_helpers"] = {name: sha256(ROOT / "virgl" / name) for name in VIRGL_FILES}
    archive = verified_archive(args.archive)

    disk = directory / "disk.img"
    command = [
        executable, "-m", memory, "-smp", "4", "-cpu", cpu,
        "-machine", "virt,gic-version=3", "-kernel", str(kernel),
        "-device", "virtio-blk-pci,drive=disk0",
        "-drive", f"id=disk0,if=none,format=raw,file={disk}",
        "-object", "rng-random,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
        "-device", gpu, "-vga", "none", "-display", display,
        "-serial", "mon:stdio", "-device", "virtio-keyboard-pci",
        "-device", "virtio-tablet-pci", "-accel", args.accel,
        "-device", "virtio-net-pci,netdev=net0", "-netdev", "user,id=net0",
    ]
    if args.qmp:
        address = args.qmp.absolute()
        if address.exists() or address.is_symlink() or "," in str(address):
            parser.error("请为 QMP 使用一个新的、不含逗号的 Unix socket 路径。")
        command.extend(["-qmp", f"unix:{address},server=on,wait=off"])
    if args.check:
        print(f"版本与文件哈希核对通过，模式 {args.variant}。")
        print(shlex.join(command))
        return 0
    prepare_disk(archive, directory, identity)
    if args.prepare_only:
        print(f"运行磁盘已准备好。{disk}")
        return 0
    environment = os.environ.copy()
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "GTK_PATH", "GTK_MODULES",
                 "GIO_MODULE_DIR", "GIO_EXTRA_MODULES"):
        environment.pop(name, None)
    if args.variant == "virgl":
        for name in ("LIBGL_ALWAYS_SOFTWARE", "GALLIUM_DRIVER", "MESA_LOADER_DRIVER_OVERRIDE",
                     "MESA_GL_VERSION_OVERRIDE", "MESA_GLES_VERSION_OVERRIDE", "EGL_PLATFORM"):
            environment.pop(name, None)
    os.execve(executable, command, environment)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, lzma.LZMAError, subprocess.CalledProcessError) as error:
        print(f"启动失败。{error}", file=sys.stderr)
        raise SystemExit(1)
