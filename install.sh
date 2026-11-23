#!/usr/bin/env bash
# ==============================================================================
# MA Auto Trader — Ubuntu 服务器一键安装与部署脚本
# 适用系统: Ubuntu 20.04 / 22.04 / 24.04 (Debian系通用)
# ==============================================================================

set -euo pipefail

# 颜色输出定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}      🚀 MA Auto Trader 量化交易系统一键安装脚本      ${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. 检查是否在项目根目录下执行
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/requirements.txt" ] || [ ! -d "$SCRIPT_DIR/backend" ]; then
    echo -e "${RED}[错误] 未检测到有效的项目文件结构，请确保在 ma-trader 根目录下执行该脚本！${NC}"
    exit 1
fi

# 获取当前非 root 用户（如果是以 sudo 执行）或当前用户
ACTUAL_USER="${SUDO_USER:-$USER}"
echo -e "${GREEN}[1/6] 检查运行环境与当前用户: ${YELLOW}${ACTUAL_USER}${NC}"
echo -e "${GREEN}      项目安装路径: ${YELLOW}${SCRIPT_DIR}${NC}"

# 2. 安装系统依赖
echo -e "\n${GREEN}[2/6] 安装系统依赖 (python3, pip, venv, curl)...${NC}"
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv git curl ufw

# 3. 创建 Python 虚拟环境与安装项目依赖
echo -e "\n${GREEN}[3/6] 配置 Python 虚拟环境并安装量化依赖...${NC}"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 升级 pip 并安装依赖（国内镜像 + 官方回退，自适应服务器所在区域）
echo "正在安装 Python 依赖库（fastapi, ccxt, pandas, sqlalchemy 等）..."
pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple
pip install -r "$SCRIPT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple

# 4. 初始化配置文件与数据目录
echo -e "\n${GREEN}[4/6] 初始化配置文件与数据目录...${NC}"
mkdir -p "$SCRIPT_DIR/data"
mkdir -p "$SCRIPT_DIR/logs"

# 严格保证 data 目录及其中的 SQLite 数据库对当前用户具备读写权限
sudo chmod 777 "$SCRIPT_DIR/data"
if [ -f "$SCRIPT_DIR/data/trading.db" ]; then
    sudo chmod 666 "$SCRIPT_DIR/data/trading.db"
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "未找到 .env 文件，已从 .env.example 自动生成默认配置..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
fi

# 修正目录归属为实际运行用户
sudo chown -R "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPT_DIR"

# 5. 防火墙端口放行 (8080)
echo -e "\n${GREEN}[5/6] 检查防火墙设置 (开放 8080 端口)...${NC}"
if sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow 8080/tcp || true
    echo "已成功在 UFW 中放行 8080 端口。"
else
    echo "UFW 防火墙未激活，请确认云服务器控制台的安全组已放行 TCP 8080 端口。"
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
ExecStart=$SCRIPT_DIR/.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
EnvironmentFile=-$SCRIPT_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

# 重载并启动服务
sudo systemctl daemon-reload
sudo systemctl enable ma-trader
sudo systemctl restart ma-trader

# 获取服务器公网 IP
SERVER_IP=$(curl -s --max-time 3 https://api.ipify.org || curl -s --max-time 3 https://ifconfig.me || echo "你的服务器公网IP")

echo -e "\n${BLUE}======================================================${NC}"
echo -e "${GREEN}🎉 恭喜！MA Auto Trader 已成功部署并启动！${NC}"
echo -e "${BLUE}======================================================${NC}"
echo -e "访问控制台地址: ${YELLOW}http://${SERVER_IP}:8080${NC}"
echo -e "进程管理命令:"
echo -e "  - 查看运行状态: ${GREEN}sudo systemctl status ma-trader${NC}"
echo -e "  - 查看实时日志: ${GREEN}sudo journalctl -u ma-trader -f${NC}"
echo -e "  - 重启量化服务: ${GREEN}sudo systemctl restart ma-trader${NC}"
echo -e "  - 停止量化服务: ${GREEN}sudo systemctl stop ma-trader${NC}"
echo -e "${BLUE}======================================================${NC}"
