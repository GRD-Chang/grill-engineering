#!/bin/sh
# One-shot host preparation; install.sh owns every Runner lifecycle operation.
set -eu
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
case "${1-}" in
    --help|-h)
        echo '用法：sh setup.sh [--yes]；检查并统一确认一次后准备宿主与安装 Runner。'
        echo '--yes 仅确认已展示的计划，不授予系统权限，也不进行账号登录。'
        exit 0 ;;
    --yes) confirmed=yes; shift ;;
    '') confirmed=no ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
esac
[ "$#" -eq 0 ] || { echo 'setup.sh 不接受安装生命周期参数；回滚请使用 install.sh --rollback。' >&2; exit 1; }
[ "$(uname -s)" = Linux ] || { echo 'Runner Setup 首版仅面向 Linux。'; exit 1; }
ID=unknown
VERSION_ID=unknown
# Read data only: os-release is not executable configuration.
if [ -r /etc/os-release ]; then
    ID=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '\042\047')
    VERSION_ID=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '\042\047')
fi
architecture=$(uname -m)
printf '宿主：%s %s / %s；自动准备使用当前已配置的 apt 软件源。\n' "$ID" "$VERSION_ID" "$architecture"
echo '发行版适配不是完整宿主验证；支持证据见安装文档。'
# Bootstrap only the interpreter with coreutils; once available, all tool probes
# use the same bounded output and descendant cleanup as doctor.
probe_python=no
if command -v timeout >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 &&
    timeout -k 1 5 python3 -c 'import platform, sys; sys.exit(not (platform.python_implementation() == "CPython" and sys.version_info >= (3, 11)))' >/dev/null 2>&1; then
    probe_python=yes
fi
probe() {
    if [ "$probe_python" = yes ]; then
        python3 "$SOURCE_DIR/src/agent_run/runner_setup.py" "$@"
    elif command -v timeout >/dev/null 2>&1; then
        timeout -k 1 5 "$@"
    else
        return 1
    fi
}
packages=''
if ! command -v timeout >/dev/null 2>&1; then
    packages=' coreutils'
    echo '缺失：coreutils timeout；探针暂不能运行，需补齐后重跑。'
fi
need() { packages="$packages $1"; printf '缺失或不兼容：%s；处理：%s\n' "$1" "$2"; }
python_ok() {
    command -v python3 >/dev/null 2>&1 && probe python3 -c 'import platform, sys; sys.exit(not (platform.python_implementation() == "CPython" and sys.version_info >= (3, 11)))' >/dev/null 2>&1
}
git_ok() {
    command -v git >/dev/null 2>&1 || return 1
    version=$(probe git --version 2>/dev/null) || return 1
    printf '%s\n' "$version" | awk 'BEGIN {ok=0} /^git version [0-9]+[.][0-9]+([.]|$)/ {split($3,v,"."); ok=(v[1]>2 || (v[1]==2 && v[2]>=40))} END {exit !ok}'
}
if python_ok; then echo '复用：CPython >= 3.11'; else need python3 'apt 补装；候选版本仍需满足 CPython >= 3.11'; fi
if python_ok && probe python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then echo '复用：venv/ensurepip'; else need python3-venv 'apt 补装 Python 虚拟环境构建能力'; fi
if python_ok && probe python3 -m pip --version >/dev/null 2>&1; then echo '复用：pip'; else need python3-pip 'apt 补装 wheel 下载与构建入口'; fi
if python_ok && probe python3 -c 'import ssl, urllib.request; ssl.create_default_context()' >/dev/null 2>&1; then
    echo '下载：Python TLS 可用；软件源连通性和真实 wheel 构建由现有安装器验证。'
else need ca-certificates 'apt 补装 CA；Python ssl 仍不可用时请修复解释器'; fi
if git_ok; then echo '复用：Git >= 2.40'; else need git 'apt 补装；候选版本仍需满足 Git >= 2.40'; fi
gh_ok() {
    command -v gh >/dev/null 2>&1 || return 1
    capabilities=$(probe gh api --help 2>/dev/null) || return 1
    for flag in --paginate --slurp --method --header; do
        case "$capabilities" in *"$flag"*) ;; *) return 1;; esac
    done
}
if gh_ok; then
    echo '复用：gh api 生产调用选项可用；认证在确认前检查，安装后由 doctor 复验。'
else
    # Distribution candidates may lack --slurp (including Ubuntu 24.04).
    # Keep gh outside automatic APT preparation instead of guessing compatibility.
    echo '人工待办：gh 缺失或不兼容；不自动 apt 补装 gh。请按 https://github.com/cli/cli/blob/trunk/docs/install_linux.md 安装或升级 GitHub CLI，确认 gh api --help 包含 --paginate、--slurp、--method、--header 后重跑 setup.sh；认证另用 gh auth status 检查。'
fi
for tool in bwrap openssl; do
    case "$tool" in bwrap) package=bubblewrap;; *) package=$tool;; esac
    if command -v "$tool" >/dev/null 2>&1 && { if [ "$tool" = openssl ]; then probe "$tool" version; else probe "$tool" --version; fi; } >/dev/null 2>&1; then
        printf '复用：%s（实际执行能力在 doctor 复验）\n' "$tool"
    else need "$package" 'apt 补装'; fi
done
if command -v codex >/dev/null 2>&1 && probe codex --version >/dev/null 2>&1; then
    echo '复用：Codex（登录和实际协议能力在 doctor 复验）'
else echo '人工待办：按官方 Codex CLI 安装说明安装 Codex，再执行 codex login。'; fi
if command -v systemctl >/dev/null 2>&1 && command -v systemd-run >/dev/null 2>&1; then
    echo 'user systemd：入口存在；实际 transient unit 能力在安装后通过短命探针复验。'
else echo '人工待办：提供可用的 user systemd 会话；不替换 init、不启用 linger。'; fi
host_check() {
    failure=$1
    remedy=$2
    shift 2
    if ! command -v "$1" >/dev/null 2>&1; then
        printf '%s：缺少 %s；%s\n' "$failure" "$1" "$remedy"
    elif probe "$@" >/dev/null 2>&1; then
        printf '宿主检查通过：%s\n' "$*"
    else
        code=$?
        printf '%s（探针退出码 %s）；%s\n' "$failure" "$code" "$remedy"
    fi
}
check_host() {
    echo '只读宿主检查（不依赖新 Runner；不展示认证或会话环境原始输出）：'
    host_check 'gh 认证未就绪' '自行 gh auth login 后重跑 Setup' gh auth status
    host_check 'Codex 认证未就绪' '自行 codex login 后重跑 Setup' codex login status
    host_check 'bubblewrap namespace 探针失败' '请检查宿主 mount/device/PID namespace 权限与安全策略后重跑 Setup' \
        bwrap --die-with-parent --ro-bind / / --dev /dev --unshare-pid --proc /proc -- /bin/true
    host_check 'user systemd 会话不可用' '请在真实用户登录会话检查 user bus 后重跑 Setup' \
        systemctl --user show-environment
    echo '后置检查：统一确认且安装成功后才创建短命 systemd unit，并运行受管 Runner 的 doctor；只读检查通过不等于执行就绪。'
}
check_host
echo '人工待办：复用现有登录；缺失时自行 gh auth login / codex login；Skills、项目工具链、忽略规则、标签和 CI 按接入文档准备。'
apt_supported=no
# These base releases provide Python >= 3.11 and Git >= 2.40. Do not
# silently install known-too-old packages on earlier or derivative releases.
case "$ID:$VERSION_ID:$architecture" in
    ubuntu:24.04:x86_64|ubuntu:24.04:aarch64|debian:13:x86_64|debian:13:aarch64)
        if command -v apt-get >/dev/null 2>&1; then apt_supported=yes; fi ;;
esac
if [ -n "$packages" ]; then
    if [ "$apt_supported" = yes ]; then
        printf '计划：在 Runner 管理租约下刷新已有 APT 索引，再 apt-get install --no-install-recommends%s（不执行全系统升级、不新增软件源）。\n' "$packages"
        if command -v apt-cache >/dev/null 2>&1; then
            # Package names are fixed above, never user-supplied shell text.
            probe apt-cache policy $packages || echo '软件源候选无法读取；请管理员核对 apt-cache policy 后重跑。'
        fi
    else printf '跳过自动补装：当前 Linux 无 apt 适配（自动范围：Ubuntu 24.04 / Debian 13，x86_64 / aarch64，且 apt-get 可用）；请用本发行版包管理器补齐：%s。继续可行步骤。\n' "$packages"; fi
fi
echo '计划：调用现有 install.sh 构建候选、检查兼容性并原子激活；随后运行 doctor 和短命 user systemd 探针（失败时清理本次 unit）。'
if [ "$confirmed" != yes ]; then
    printf '执行以上计划？[y/N] '
    answer=''
    read -r answer || true
    case "$answer" in y|Y|yes|YES) ;; *) echo '已拒绝：未执行安装或系统修改。重新准备时运行 sh setup.sh。'; exit 1;; esac
fi
prepare_packages() (
    # Use the same inode as the installer/Executor, including before Python exists.
    if ! command -v flock >/dev/null 2>&1; then
        echo '缺少 util-linux flock，无法保护宿主准备租约；跳过系统变更，请管理员补齐后重跑。'
        return 1
    fi
    setup_data=${XDG_DATA_HOME:-"$HOME/.local/share"}
    case "$setup_data" in '~'|'~/'*) echo '请将 XDG_DATA_HOME 展开为绝对路径后重跑。'; return 1;; esac
    mkdir -p "$setup_data/agent-run" || return 1
    flock -n "$setup_data/agent-run/install.lock" sh -c '
        packages=$1
        shift
        "$@" update && "$@" install -y --no-install-recommends $packages
    ' agent-run-setup "$packages" "$@"
)
if [ -n "$packages" ] && [ "$apt_supported" = yes ]; then
    # Never prompt for or acquire privileges. Users authorize elevation themselves.
    if [ "$(id -u)" = 0 ]; then
        prepare_packages apt-get || echo '依赖准备失败或 Runner 管理租约忙；旧 Runner 不变，继续检查可行安装步骤。'
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        prepare_packages sudo -n apt-get || echo '依赖准备失败或 Runner 管理租约忙；旧 Runner 不变，继续检查可行安装步骤。'
    else
        printf '权限不足：跳过系统修改。请管理员执行 apt-get install --no-install-recommends%s，然后重跑 sh setup.sh。\n' "$packages"
    fi
fi
if ! python_ok; then
    echo '构建前置条件未满足；重新汇总仍可执行的只读宿主检查：'
    check_host
    echo '结果：Runner 未安装或新版本未激活；缺少 CPython >= 3.11，无法构建。补齐后重跑 sh setup.sh；旧安装未改动。'
    exit 1
fi
if ! sh "$SOURCE_DIR/install.sh"; then
    echo '安装未完成；重新汇总仍可执行的只读宿主检查：'
    check_host
    echo '结果：Runner 未安装或新版本未激活；见安装错误。旧 Active Runner 由现有安装器保留；处理后重跑 sh setup.sh。'
    exit 1
fi
# Replace the shell so signals reach the probe supervisor directly.
exec python3 "$SOURCE_DIR/src/agent_run/runner_setup.py"
