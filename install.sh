#!/usr/bin/env bash
# ==============================================================================
# MA Auto Trader — Ubuntu 服务器一键安装与部署脚本
# 适用系统: Ubuntu 20.04 / 22.04 / 24.04 (Debian系通用)
# 默认仅监听 127.0.0.1:8080，建议配合 Nginx + HTTPS 反向代理或 SSH 隧道访问。
# 如确需指定监听地址，可执行: BIND_HOST=0.0.0.0 ./install.sh
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}      🚀 MA Auto Trader 量化交易系统一键安装脚本      ${NC}"
echo -e "${BLUE}======================================================${NC}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/requirements.txt" ] || [ ! -d "$SCRIPT_DIR/backend" ]; then
    echo -e "${RED}[错误] 未检测到有效的项目文件结构，请确保在 ma-trader 根目录下执行该脚本！${NC}"
    exit 1
fi

ACTUAL_USER="${SUDO_USER:-$USER}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"
BIND_PORT="${BIND_PORT:-8080}"

echo -e "${GREEN}[1/6] 检查运行环境与当前用户: ${YELLOW}${ACTUAL_USER}${NC}"
echo -e "${GREEN}      项目安装路径: ${YELLOW}${SCRIPT_DIR}${NC}"
echo -e "${GREEN}      监听地址: ${YELLOW}${BIND_HOST}:${BIND_PORT}${NC}"

# 2. 安装系统依赖
echo -e "\n${GREEN}[2/6] 安装系统依赖 (python3, pip, venv, curl)...${NC}"
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv git curl

# 3. 创建 Python 虚拟环境与安装项目依赖
echo -e "\n${GREEN}[3/6] 配置 Python 虚拟环境并安装量化依赖...${NC}"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "正在安装 Python 依赖库（fastapi, ccxt, pandas, sqlalchemy 等）..."
pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple
pip install -r "$SCRIPT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple

# 4. 初始化配置文件与数据目录（最小权限，禁止 777/666）
echo -e "\n${GREEN}[4/6] 初始化配置文件与数据目录...${NC}"
mkdir -p "$SCRIPT_DIR/data"
mkdir -p "$SCRIPT_DIR/logs"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "未找到 .env 文件，已从 .env.example 自动生成默认配置..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
fi

sudo chown -R "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPT_DIR"
chmod 700 "$SCRIPT_DIR/data" "$SCRIPT_DIR/logs"
chmod 600 "$SCRIPT_DIR/.env"
if [ -f "$SCRIPT_DIR/data/trading.db" ]; then
    chmod 600 "$SCRIPT_DIR/data/trading.db"
fi

# 5. 安全提示（不再默认开放公网明文 8080 端口）
echo -e "\n${GREEN}[5/6] 网络访问安全检查...${NC}"
if [ "$BIND_HOST" = "127.0.0.1" ]; then
    echo -e "${GREEN}服务仅监听本机 127.0.0.1:${BIND_PORT}，不会将明文 HTTP 直接暴露到公网。${NC}"
    echo -e "请通过 Nginx + HTTPS 反向代理或 SSH 端口转发访问（ssh -L 8080:127.0.0.1:${BIND_PORT} user@server）。"
else
    echo -e "${YELLOW}[警告] 服务监听在 ${BIND_HOST}:${BIND_PORT}，请务必配合 HTTPS 反向代理或私网/VPN 访问！${NC}"
fi

# 6. 配置 Systemd 系统守护服务
echo -e "\n${GREEN}[6/6] 注册并启动 Systemd 后台自启服务...${NC}"
SERVICE_FILE="/etc/systemd/system/ma-trader.service"

sudo bash -c "cat > '$SERVICE_FILE'" <<EOF
[Unit]
Description=MA Auto Trader Service
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/.venv/bin/python -m uvicorn backend.main:app --host $BIND_HOST --port $BIND_PORT
Restart=always
RestartSec=5
UMask=0077
StandardOutput=journal
StandardError=journal
EnvironmentFile=-$SCRIPT_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ma-trader
sudo systemctl restart ma-trader

echo -e "\n${BLUE}======================================================${NC}"
echo -e "${GREEN}🎉 恭喜！MA Auto Trader 已成功部署并启动！${NC}"
echo -e "${BLUE}======================================================${NC}"
if [ -f "$SCRIPT_DIR/data/admin_initial_password.txt" ]; then
    echo -e "${YELLOW}初始管理员密码已自动生成并保存在:${NC} $SCRIPT_DIR/data/admin_initial_password.txt"
    echo -e "查看命令: ${GREEN}cat $SCRIPT_DIR/data/admin_initial_password.txt${NC}"
fi
echo -e "访问方式:"
echo -e "  - 推荐：配置 Nginx + HTTPS 反向代理到 http://127.0.0.1:${BIND_PORT}（见 DEPLOY.md）"
echo -e "  - 临时：本地执行 ${GREEN}ssh -L 8080:127.0.0.1:${BIND_PORT} ${ACTUAL_USER}@<服务器IP>${NC} 后访问 http://127.0.0.1:8080"
echo -e "进程管理命令:"
echo -e "  - 查看运行状态: ${GREEN}sudo systemctl status ma-trader${NC}"
echo -e "  - 查看实时日志: ${GREEN}sudo journalctl -u ma-trader -f${NC}"
echo -e "  - 重启量化服务: ${GREEN}sudo systemctl restart ma-trader${NC}"
echo -e "  - 停止量化服务: ${GREEN}sudo systemctl stop ma-trader${NC}"
echo -e "${BLUE}======================================================${NC}"
