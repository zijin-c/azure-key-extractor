#!/usr/bin/env bash
# ==============================================================================
# Azure Key Extractor - Linux VPS 全自动安装与 Systemd 守护进程配置脚本
# 支持系统: Ubuntu 20.04+ / 22.04+ / 24.04 / Debian 11+ / 12 / CentOS 8+
# ==============================================================================

set -e

# 颜色定义
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}================================================================${NC}"
echo -e "${BLUE}     Azure Key Extractor — VPS 全自动 Systemd 部署程序         ${NC}"
echo -e "${BLUE}================================================================${NC}"

# 获取当前脚本所在绝对目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 检查当前用户与 sudo 权限
SUDO=""
CURRENT_USER="$(whoami)"
if [ "$CURRENT_USER" != "root" ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
        echo -e "${YELLOW}[提示] 检测到非 root 用户 ($CURRENT_USER)，将使用 sudo 执行系统级配置${NC}"
    else
        echo -e "${RED}[错误] 请以 root 用户运行或安装 sudo${NC}"
        exit 1
    fi
fi

# 1. 更新系统并安装 Playwright 与 Python 依赖
echo -e "\n${GREEN}[1/5] 更新系统源并安装底层系统库与 Python 环境...${NC}"
if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y \
        python3 python3-pip python3-venv curl wget procps lsof xvfb \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        libwayland-client0 fonts-liberation fonts-noto-color-emoji
elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf update -y
    $SUDO dnf install -y python3 python3-pip python3-virtualenv curl wget procps-ng lsof xorg-x11-server-Xvfb
elif command -v yum >/dev/null 2>&1; then
    $SUDO yum update -y
    $SUDO yum install -y python3 python3-pip curl wget procps lsof xorg-x11-server-Xvfb
fi

# 2. 创建独立虚拟环境
echo -e "\n${GREEN}[2/5] 创建独立虚拟环境 (venv)...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "虚拟环境 venv 初始化完成"
fi
source venv/bin/activate

# 3. 安装依赖与 Playwright Chromium
echo -e "\n${GREEN}[3/5] 安装 Python 依赖库与 Chromium 独立浏览器...${NC}"
pip install --upgrade pip
pip install -r requirements.txt

export PLAYWRIGHT_BROWSERS_PATH="$SCRIPT_DIR/ms-playwright"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
python -m playwright install chromium
if command -v apt-get >/dev/null 2>&1; then
    python -m playwright install-deps chromium || true
fi

# 4. 初始化配置与目录
echo -e "\n${GREEN}[4/5] 检查配置文件与工作目录...${NC}"
mkdir -p data results screenshots
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${GREEN}[OK] 已生成默认 .env 配置文件 (已开启 HEADLESS=true 与省流量)${NC}"
    fi
fi

# 5. 配置并启动 Systemd 守护进程
echo -e "\n${GREEN}[5/5] 配置 Systemd 守护进程服务 (azure-key.service)...${NC}"

SERVICE_FILE="/etc/systemd/system/azure-key.service"

# 动态生成指向当前目录的 Systemd 配置文件
$SUDO bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=Azure Key Extractor Web Service
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
Environment=PLAYWRIGHT_BROWSERS_PATH=$SCRIPT_DIR/ms-playwright
Environment=PATH=$SCRIPT_DIR/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
Environment=HEADLESS=true
Environment=MALLOC_ARENA_MAX=2
Environment=MALLOC_TRIM_THRESHOLD_=131072
ExecStart=$SCRIPT_DIR/venv/bin/python run_gui.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF"

echo -e "已写入 Systemd 服务配置: $SERVICE_FILE"

# 重载并启动服务
$SUDO systemctl daemon-reload
$SUDO systemctl enable azure-key
$SUDO systemctl restart azure-key

sleep 2

# 输出安装完成提示
SERVER_IP=$(curl -s4 ifconfig.me || curl -s4 icanhazip.com || echo "你的VPS_IP")

echo -e "\n${BLUE}================================================================${NC}"
echo -e "${GREEN}        🎉 Systemd 守护进程服务已成功安装并启动！              ${NC}"
echo -e "${BLUE}================================================================${NC}"
echo -e "  🌐 Web 控制台访问:   ${CYAN}http://${SERVER_IP}:5010${NC}"
echo -e "  👤 默认管理员账号:   ${YELLOW}admin${NC}"
echo -e "  🔑 默认初始密码:     ${YELLOW}admin123${NC} (登录后请尽快在后台修改)"
echo -e ""
echo -e "  ⚙️  ${GREEN}Systemd 常用管理命令:${NC}"
echo -e "   • 查看运行状态:     ${YELLOW}$SUDO systemctl status azure-key${NC}"
echo -e "   • 查看实时运行日志: ${YELLOW}$SUDO journalctl -u azure-key -f${NC}"
echo -e "   • 重启服务:         ${YELLOW}$SUDO systemctl restart azure-key${NC}"
echo -e "   • 停止服务:         ${YELLOW}$SUDO systemctl stop azure-key${NC}"
echo -e "   • 启动服务:         ${YELLOW}$SUDO systemctl start azure-key${NC}"
echo -e "   • 设置开机自启:     ${YELLOW}$SUDO systemctl enable azure-key${NC}"
echo -e "   • 禁用开机自启:     ${YELLOW}$SUDO systemctl disable azure-key${NC}"
echo -e "${BLUE}================================================================${NC}\n"
