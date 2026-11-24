# x-kernel Chromium 运行存档

在 Linux 宿主的 AArch64 QEMU 客体中启动 x-kernel、Weston 和 Chromium。这里保存个人复现所用的冻结内核、对应源码补丁和根文件系统。功能版用于打开三个本地页面，性能版是另一个内核快照。

## 克隆和启动

需要 Linux 宿主、Python 3.9+、QEMU AArch64 8.0+ 及 GTK 显示后端。首次下载镜像需要 GitHub CLI 和本仓库的读取权限。Ubuntu 可用以下命令安装运行依赖。

```bash
sudo apt install python3 qemu-system-arm qemu-system-gui gh
gh auth login
gh repo clone HDU-OrangeS/x-kernel-chromium-archive
cd x-kernel-chromium-archive
python3 run.py
```

首次运行从 Releases 下载 `rootfs.img.xz`，核对压缩包、解压镜像和内核的 SHA-256，再创建 2 GiB 的可写工作磁盘。建议留出 3 GiB 磁盘空间。后续使用同一工作磁盘。默认以 4 vCPU、2 GiB 内存和 TCG 启动，可在 x86_64 或 AArch64 Linux 上使用。

在客体串口看到 `kylin-x` 后，运行下面的命令。第一条启动 seatd 和 Weston，第二条指定 Chromium 使用刚启动的 Wayland 会话。

```sh
sh /usr/local/libexec/xkernel-desktop/start-desktop.sh
export XDG_RUNTIME_DIR=/run/user/0 WAYLAND_DISPLAY=wayland-0
chromium --ozone-platform=wayland --no-sandbox \
  --disable-gpu --disable-software-rasterizer \
  --ipc-connection-timeout=120 --disable-background-networking \
  --disable-component-update --no-first-run \
  --user-data-dir=/tmp/xkernel-archive-profile \
  file:///usr/share/kiosk/testpages/index.html
```

同目录还有 `layout.html` 和 `interaction.html`。这个启动方式用于隔离的测试客体，浏览器打开本地页面。关闭浏览器后，在客体执行 `sync`，再在宿主串口按 `Ctrl+A`、`X` 退出 QEMU。

| 操作 | 命令 |
| --- | --- |
| 校验资源和查看启动参数 | `python3 run.py --check` |
| 启动性能快照 | `python3 run.py performance` |
| 离线使用已下载的 Release 镜像 | `python3 run.py --archive /path/to/rootfs.img.xz` |
| 新建独立的工作磁盘 | `python3 run.py --workdir /path/to/fresh-workdir` |
| 在可用的 AArch64 KVM 宿主上尝试启动 | `python3 run.py --accel kvm` |

## 源码快照

功能版源码以 `fa00ae75d8c05e86127e5475a05159123262ebbe` 为基点。解包 `source/x-kernel-functional-base.tar.gz`，依次应用 `control.patch`、`cache-hit.patch`、`cpu-timer-poll.patch`、`preempt-check.patch` 和 `kvfs-readahead-test.patch`。

```bash
archive="$(pwd)"
(
  set -e
  tar -xzf "$archive/source/x-kernel-functional-base.tar.gz" -C /path/to/workspace
  cd /path/to/workspace/x-kernel
  for patch in control cache-hit cpu-timer-poll preempt-check kvfs-readahead-test; do
    git apply --check "$archive/source/$patch.patch"
    git apply "$archive/source/$patch.patch"
  done
  cp "$archive/source/aarch64-qemu.config" .config
  make defconfig
  make build
)
```

性能版以 `799107c7d5b5db18276132af48ad76cc0fc3c30f` 为基点，解包 `source/x-kernel-performance-base.tar.gz` 后应用 `source/performance.patch`。两版冻结构建信息在 `source/*.toml`。首个可运行版本的 Git 标签及历史在 `source/first-runnable.bundle`，对应内核在 `kernels/first-runnable.bin`。

x-kernel 源码保留[上游许可证](LICENSE)和[版权声明](NOTICE)。根文件系统基于提供的 Alpine AArch64 镜像，包含 Chromium、Weston、seatd 和测试页。存档镜像清理了账户历史、浏览器配置与运行日志，并锁定镜像里的密码登录。镜像和内核身份见 [rootfs.json](rootfs.json) 和 [SHA256SUMS](SHA256SUMS)。
