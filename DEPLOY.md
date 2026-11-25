# MA Auto Trader — Ubuntu 服务器完整部署文档

本指南将指导你从零开始，将 **MA Auto Trader (均线量化交易系统)** 完整部署到 Ubuntu 20.04 / 22.04 / 24.04 服务器上，并实现开机自启与后台持久运行。

---

## 目录
1. [极速部署：一键安装脚本（推荐）](#0-极速部署一键安装脚本推荐)
2. [服务器准备与网络要求](#1-服务器准备与网络要求)
3. [上传项目代码到服务器](#2-上传项目代码到服务器)
4. [手动分步部署指南](#3-手动分步部署指南)
5. [（可选）配置 Nginx 反向代理与域名访问](#7-可选配置-nginx-反向代理与域名访问)
6. [日常维护命令与日志查看](#9-日常维护命令与日志查看)

---

## 0. 极速部署：一键安装脚本（推荐）

将项目代码上传到服务器后，进入项目目录，直接运行内置的自动化部署脚本：

```bash
cd /opt/ma-trader   # 或者是你的项目所在目录
chmod +x install.sh
./install.sh
```

**该脚本会自动完成：**
1. 自动检测并安装 Python3、pip、venv、curl、ufw 等系统包。
2. 自动创建 Python 虚拟环境并安装所有依赖（带国内高速镜像与官方源自动回退）。
3. 自动生成默认 `.env` 配置文件与持久化数据目录。
4. 自动放行 8080 端口。
5. 自动注册并启动 Systemd 服务（开机自启、崩溃自动拉起）。
6. 输出服务器外网访问地址与日常维护命令。

---

---

## 1. 服务器准备与网络要求

- **操作系统**：Ubuntu 20.04 LTS / 22.04 LTS / 24.04 LTS
- **规格推荐**：1 核 CPU / 1GB 内存以上即可流畅运行
- **网络节点建议**：
  - **首选海外服务器**（如 AWS 东京/新加坡、DigitalOcean、Vultr、Linode 等）：可直接秒级直连币安官方 API、Binance WebSocket 及 Telegram Bot，无需配置任何代理。
  - **国内服务器**：连接币安或 Telegram 会受限，需在 Web 界面全局设置中配置 `http_proxy`（例如 `socks5://127.0.0.1:7890` 或 `http://127.0.0.1:7890`）。

---

## 2. 上传项目代码到服务器

### 方式 A：通过 Git（推荐）
在服务器上直接拉取代码仓库：
```bash
# 切换到部署目录（例如 /opt 或 /home/ubuntu）
cd /opt
sudo git clone <你的git仓库地址> ma-trader
sudo chown -R $USER:$USER /opt/ma-trader
cd /opt/ma-trader
```

### 方式 B：从本地电脑通过 rsync / scp 直接上传
在**本地 Mac 电脑**终端中执行（排除掉本地虚拟环境和临时文件）：
```bash
# 请将 user@your-server-ip 替换为你服务器的真实用户名与公网 IP
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  /Users/tony/Documents/playground/freedom/ma-trader/ \
  user@your-server-ip:/opt/ma-trader/
```

---

## 3. 安装基础依赖与 Python 环境

登录进入 Ubuntu 服务器终端：

```bash
# 1. 更新 apt 软件源
sudo apt update && sudo apt upgrade -y

# 2. 安装 Python3、pip、venv 以及基础构建工具
sudo apt install -y python3 python3-pip python3-venv git curl
```

---

## 4. 配置环境变量 (.env)

进入项目目录，从模板复制一份 `.env`：

```bash
cd /opt/ma-trader
cp .env.example .env
nano .env   # 或使用 vim .env
```

在 `.env` 中按需填入你的配置信息（未配置的可在部署完成后直接在 Web 界面“设置”中修改）：
```ini
BINANCE_API_KEY=你的币安API_Key
BINANCE_SECRET=你的币安Secret
TELEGRAM_TOKEN=你的Telegram_Bot_Token
TELEGRAM_CHAT_ID=你的Telegram_Chat_ID
TRADING_MODE=paper        # 模式：paper (模拟) 或 live (实盘)
```
> **提示**：SQLite 数据库会自动保存在 `data/trading.db`，所有在 Web 页面修改的设置也会持久化在此数据库中。

---

## 5. 快速启动与效果验证（开发/测试）

项目自带自动安装依赖并启动的脚本 `run.sh`：

```bash
cd /opt/ma-trader
chmod +x run.sh

# 启动程序（首次运行会自动创建 .venv 并安装 requirements.txt 依赖）
./run.sh
```

控制台看到如下输出即表示启动成功：
```
[run.sh] Dependencies already satisfied.
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

此时打开浏览器，访问 `http://<你的服务器公网IP>:8080` 即可看到实时交易与回测控制台！

*(按 `Ctrl + C` 可停止前台测试进程)*

---

## 6. 生产环境部署：Systemd 守护进程（推荐）

在生产服务器上，推荐使用 Ubuntu 原生 **Systemd** 服务将程序托管在后台，支持：
- 进程崩溃自动重启
- 服务器重启开机自启
- 统一日志管理

### 步骤 1：创建服务文件
```bash
sudo nano /etc/systemd/system/ma-trader.service
```

粘贴以下配置内容（注意检查 `User` 和项目路径，假设放在 `/opt/ma-trader`，运行用户为当前用户如 `ubuntu`）：

```ini
[Unit]
Description=MA Auto Trader Service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/ma-trader
ExecStart=/opt/ma-trader/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5
UMask=0077
StandardOutput=journal
StandardError=journal
EnvironmentFile=-/opt/ma-trader/.env

[Install]
WantedBy=multi-user.target
```

### 步骤 2：启动并启用服务
```bash
# 重新加载 systemd
sudo systemctl daemon-reload

# 设置开机自启
sudo systemctl enable ma-trader

# 启动服务
sudo systemctl start ma-trader

# 查看服务状态
sudo systemctl status ma-trader
```

看到 `Active: active (running)` 绿色字样说明运行正常！

---

## 7. （可选）配置 Nginx 反向代理与域名访问

如果希望通过标准 80/443 端口或者域名（带 HTTPS）访问，并正确支持 WebSocket 实时通信：

### 安装 Nginx
```bash
sudo apt install -y nginx
```

### 配置虚拟主机
```bash
sudo nano /etc/nginx/sites-available/ma-trader.conf
```

填入以下内容：
```nginx
server {
    listen 80;
    server_name your-domain.com;  # 如果没有域名，可直接写 _ 或你的公网 IP

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 支持（非常重要，图表与告警依赖 WS 通信）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

启用配置并重启 Nginx：
```bash
sudo ln -s /etc/nginx/sites-available/ma-trader.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## 8. 防火墙与安全组配置

**强烈建议**不要将明文 8080 端口直接暴露到公网，仅通过 Nginx + HTTPS（443 端口）或 SSH 隧道访问：

- **通过 Nginx + HTTPS（推荐）**：仅放行 **TCP 80** 与 **TCP 443**（可用 `certbot --nginx` 免费申请 Let's Encrypt 证书）

如果服务器启用了 UFW 防火墙：
```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw reload
```

---

## 9. 日常维护命令与日志查看

```bash
# 查看实时运行日志
sudo journalctl -u ma-trader -f

# 查看最近 100 行日志
sudo journalctl -u ma-trader -n 100 --no-pager

# 重启服务（如修改了后端代码）
sudo systemctl restart ma-trader

# 停止服务
sudo systemctl stop ma-trader

# 查看端口监听状态
sudo ss -tulpn | grep 8080
```
