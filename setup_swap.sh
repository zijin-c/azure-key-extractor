#!/usr/bin/env bash
# 将系统已启用的 Swap 补到至少 8 GiB，不关闭/覆盖已有 Swap。
set -euo pipefail

swap_total_kib() {
    awk '$1 == "SwapTotal:" { print $2; found=1 } END { if (!found) exit 1 }' "$1"
}

persist_swap() {
    local swap_file="$1" fstab="$2"
    if awk -v path="$swap_file" '$1 == path && $3 == "swap" { found=1 } END { exit !found }' "$fstab"; then
        return 0
    fi
    if [[ ! -e "$fstab.azure-key.bak" ]]; then
        cp -p -- "$fstab" "$fstab.azure-key.bak" || return 1
    fi
    # 前置换行兼容原文件末尾没有换行的情况；不改写其他挂载项。
    printf '\n%s none swap sw,nofail 0 0\n' "$swap_file" >> "$fstab"
}

setup_swap() (
    # 可传临时路径供无特权测试使用；命令行入口固定使用系统路径。
    local base="${1:-/swapfile-azure-key}" fstab="${2:-/etc/fstab}" meminfo="${3:-/proc/meminfo}"
    local target_kib=$((8 * 1024 * 1024)) current_kib needed_mib free_mib fs_type
    # 此函数在子 Shell 中运行；退出 trap 需要该值在函数返回后仍可见。
    swap_file=""
    local active_files candidate source mountpoint type options rest

    [[ -f "$fstab" ]] || { echo "[错误] 找不到 fstab: $fstab" >&2; return 1; }
    active_files="$(swapon --show --noheadings --raw --output NAME)" || return 1
    # 若上次在启用成功后、写 fstab 前中断，重新执行时修复持久化。
    while IFS= read -r candidate; do
        if [[ "$candidate" == "$base".* && -f "$candidate" && ! -L "$candidate" ]]; then
            persist_swap "$candidate" "$fstab" || return 1
        fi
    done <<< "$active_files"

    # 复用此前由本脚本配置但本次尚未启用的文件，不重建或格式化它。
    while read -r source mountpoint type options rest; do
        if [[ "$source" == "$base".* && "$type" == "swap" && -f "$source" && ! -L "$source" ]]; then
            if ! grep -Fxq -- "$source" <<< "$active_files"; then
                chmod 600 -- "$source" || return 1
                swapon -- "$source" || { echo "[错误] 无法重新启用 $source，请检查内核日志" >&2; return 1; }
                active_files="$(swapon --show --noheadings --raw --output NAME)" || return 1
            fi
        fi
    done < "$fstab"

    current_kib="$(swap_total_kib "$meminfo")" || return 1
    [[ "$current_kib" =~ ^[0-9]+$ ]] || { echo "[错误] 无法读取 Swap 容量" >&2; return 1; }
    # 标准 8 GiB Swap 文件有少量头部开销，允许 1 MiB 差值。
    if (( current_kib >= target_kib - 1024 )); then
        echo "[OK] 已启用的 Swap 已达到 8 GiB，无需新增。"
        return 0
    fi

    # 按不足的容量向上取整到 MiB，并留 1 MiB 给 Swap 头部。
    needed_mib=$(((target_kib - current_kib + 1023) / 1024 + 1))
    free_mib="$(df -Pm -- "$(dirname "$base")" | awk 'NR == 2 {print $4}')" || return 1
    [[ "$free_mib" =~ ^[0-9]+$ ]] || { echo "[错误] 无法读取磁盘剩余空间" >&2; return 1; }
    if (( free_mib < needed_mib + 2048 )); then
        echo "[错误] 磁盘空间不足：需新增约 ${needed_mib} MiB Swap，并保留至少 2 GiB 空间。" >&2
        return 1
    fi
    fs_type="$(findmnt -n -o FSTYPE -T "$(dirname "$base")")" || return 1
    case "$fs_type" in
        ext2|ext3|ext4|xfs) ;;
        btrfs)
            command -v chattr >/dev/null || { echo "[错误] Btrfs 创建 Swap 需要 chattr（e2fsprogs）" >&2; return 1; }
            ;;
        *) echo "[错误] 暂不自动在 $fs_type 文件系统上创建 Swap，请配置磁盘分区 Swap 后重试。" >&2; return 1 ;;
    esac

    swap_file="$(mktemp "${base}.XXXXXX")" || return 1
    # 只清理由本次 mktemp 创建且未启用的文件，绝不删除活动 Swap。
    cleanup_swap() {
        local enabled
        if [[ -n "$swap_file" && -f "$swap_file" ]]; then
            enabled="$(swapon --show --noheadings --raw --output NAME)" || return
            if ! grep -Fxq -- "$swap_file" <<< "$enabled"; then
                rm -f -- "$swap_file"
            fi
        fi
    }
    trap cleanup_swap EXIT
    chmod 600 -- "$swap_file" || return 1
    if [[ "$fs_type" == "btrfs" ]]; then
        chattr +C -- "$swap_file" || return 1
    fi
    echo "[Swap] 正在分配 ${needed_mib} MiB：$swap_file"
    # dd 实际写入无空洞文件，避免稀疏文件无法 swapon。
    dd if=/dev/zero of="$swap_file" bs=1M count="$needed_mib" status=progress conv=fsync || return 1
    mkswap -- "$swap_file" || return 1
    if ! swapon -- "$swap_file"; then
        echo "[错误] 无法启用 Swap；请检查 VPS 是否允许 swapon 及内核日志。原有 Swap 保持不变。" >&2
        return 1
    fi
    persist_swap "$swap_file" "$fstab" || return 1
    current_kib="$(swap_total_kib "$meminfo")" || return 1
    if (( current_kib < target_kib )); then
        echo "[错误] 启用后的 Swap 未达到 8 GiB，请检查系统状态。" >&2
        return 1
    fi
    echo "[OK] Swap 已达到至少 8 GiB，已加入 $fstab 开机自动启用。"
    swapon --show
)

main() {
    if (( $# != 0 )); then
        echo "用法：sudo bash setup_swap.sh" >&2
        return 1
    fi
    if [[ "$(id -u)" != "0" ]]; then
        echo "[错误] 请使用 root 或 sudo bash setup_swap.sh" >&2
        return 1
    fi
    local tool
    for tool in swapon mkswap findmnt flock dd df awk mktemp chmod cp grep; do
        command -v "$tool" >/dev/null || { echo "[错误] 缺少命令 $tool，请安装 util-linux 和 coreutils。" >&2; return 1; }
    done
    # 避免两个部署进程同时分配 Swap 或追加 fstab。
    exec 9>/run/lock/azure-key-swap.lock
    flock -x 9
    setup_swap
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
