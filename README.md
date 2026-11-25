# MA 均线单边趋势自动交易系统 (MA Auto Trader)

基于 FastAPI + WebSocket + CCXT + ECharts 构建的加密货币自动化量化交易与回测监控系统。专为捕捉 **BTC / ETH 等主流币单边趋势（K线紧贴 MA7/MA25、不破 MA60）** 的短线交易策略设计。

---

## 🌟 核心特性

1. **多指标周期自由切换**：支持 1分钟 (1m)、5分钟 (5m)、15分钟 (15m) 等周期检测与交易。
2. **多重假突破过滤与连续确认**：
   - 支持设置连续 $N$ 根 K 线（如连续 2~3 根）实体站稳均线同侧才触发信号。
   - 自动检测长影线（影线 > 实体 1.5 倍）进行假突破过滤。
   - 高时间框架（如 15m/1h/4h）大趋势对齐过滤，顺大势做小级别。
3. **多交易对并行观察**：支持同时监控 BTCUSDT、ETHUSDT、SOLUSDT 等多个标的，每个标的独立策略参数与确认计数。
4. **历史数据回测系统**：支持自定义日期区间拉取历史 K 线进行逐根回测，输出胜率、总收益、最大回撤、夏普比率及权益增长曲线。
5. **实时 Telegram 通知**：开仓、平仓（止损/止盈/超时）、信号确认等事件秒级推送至 Telegram。
6. **现代化 Web 控制面板**：
   - **实时图表**：ECharts K线蜡烛图 + MA7(黄)/MA25(粉)/MA60(紫) + 成交量 + 买卖标记点 (▲/▼/×)。
   - **在线设置**：界面直观调节均线周期、连续确认根数、杠杆、风险系数、止盈止损比。
   - **手动模拟下单**：一键模拟开多/开空，测试策略逻辑与资金曲线。
   - **收益率统计**：每日盈亏、胜率、利润因子、各交易对收益对比柱状图。
   - **交易记录追踪**：持仓明细展示，点击单笔订单一键跳转并在 K 线上定位高亮，支持手动一键平仓。

---

## 📁 项目目录结构

```
ma-trader/
├── backend/
│   ├── main.py              # FastAPI 主程序入口 + WebSocket 实时广播
│   ├── database.py          # SQLite 数据库模型与初始化
│   ├── models.py            # Pydantic API 请求与响应数据结构
│   ├── config.py            # 配置管理中心
│   ├── strategy.py          # MA7/MA25/MA60 均线策略与假突破过滤引擎
│   ├── data_feed.py         # Binance K线数据与实时行情获取模块 (CCXT)
│   ├── executor.py          # 交易执行器（支持实盘与模拟 Paper 交易）
│   ├── trader.py            # 多标的并行监控与异步执行调度
│   ├── backtester.py        # 历史数据回测引擎与绩效指标计算
│   ├── notifier.py          # Telegram 机器人推送通知
│   └── routers/
│       ├── settings.py      # 系统与交易对参数配置 API
│       ├── trading.py       # 实时交易控制、模拟下单与 K 线 API
│       ├── backtest.py      # 异步回测管理与详情 API
│       └── stats.py         # 收益率与报表统计 API
├── frontend/
│   ├── index.html           # Web 控制面板单页面
│   ├── css/
│   │   └── style.css        # 响应式深色 TradingView 风格样式
│   └── js/
│       ├── app.js           # 前端主控制器与 WebSocket 处理
│       ├── chart.js         # ECharts K线图、均线与交易标记
│       ├── settings.js      # 全局设置与交易对增删改查
│       └── backtest.js      # 回测参数设置与结果渲染
├── data/                    # SQLite 数据库持久化目录
├── logs/                    # 运行日志保存目录
├── requirements.txt         # Python 依赖清单
├── .env.example             # 环境变量示例
├── run.sh                   # 自动化一键启动脚本
└── README.md
```

---

## 🚀 Ubuntu 服务器一键极速部署（推荐）

进入项目目录后，直接执行内置的一键安装脚本，全自动安装依赖、创建虚拟环境并配置 Systemd 后台自启服务：

```bash
cd ma-trader

# 赋予执行权限并运行一键安装脚本
chmod +x install.sh
./install.sh
```

脚本默认仅监听 `127.0.0.1:8080`（避免明文 HTTP 直接暴露到公网）。请通过 **Nginx + HTTPS 反向代理** 或 **SSH 隧道**（`ssh -L 8080:127.0.0.1:8080 user@服务器IP` 后访问 `http://127.0.0.1:8080`）访问控制台。

> 🔐 **管理员凭证与安全策略**：
> - 初始账号：`admin`
> - 初始密码：可在 `.env` 中通过 `ADMIN_PASSWORD` 指定；如未指定，首次启动会自动生成随机强密码并写入 `data/admin_initial_password.txt`（仅当前用户可读 `0600`）。
> - 首次登录后系统会强制要求修改密码（至少 8 位且包含字母和数字）。

详细手动部署与 Nginx + HTTPS 反向代理配置见：[DEPLOY.md](DEPLOY.md)。

---

## 🔄 配置为 Ubuntu 后台守护服务 (systemd)

为了确保服务器重启后自动运行，且在异常退出时自动拉起，建议配置为 systemd 服务：

```bash
# 创建服务配置文件
sudo nano /etc/systemd/system/ma-trader.service
```

粘贴以下内容（将其中的 `/home/ubuntu/ma-trader` 替换为你的实际项目路径）：

```ini
[Unit]
Description=MA Auto Trader Web Service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ma-trader
ExecStart=/home/ubuntu/ma-trader/run.sh
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

激活并启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable ma-trader
sudo systemctl start ma-trader

# 查看实时运行状态与日志
sudo systemctl status ma-trader
journalctl -u ma-trader -f
```

---

## ⚙️ 策略与风控逻辑说明

### 1. 入场规则 (以做多为例)
- **大周期对齐**：高时间框架（如 15m）必须满足 MA7 > MA25 > MA60。
- **当前周期排列**：小周期（如 1m）收盘价 > MA7 > MA25 > MA60，且 MA7、MA25 斜率持续向上。
- **连续确认**：连续满足上述条件达到设定次数（例如 2 根 K 线）。
- **假突破过滤**：K 线影线未超过实体的 1.5 倍（避免冲高回落的上影线扫止损）。

### 2. 止损与止盈
- **硬止损 (Hard SL)**：入场时以 MA60 下方（或信号 K 线低点）作为严格止损线，破位无条件市价离场。
- **动态止盈 (TP)**：依据盈亏比（默认 1.5 倍风险距离）挂出止盈单。
- **时间止损**：入场后若超过设定时间（如 5 分钟）价格未能按预期脱离成本区，则平仓观望。

---

## 📱 Telegram 机器人设置

1. 在 Telegram 中搜索 `@BotFather`，发送 `/newbot` 根据提示创建机器人并获取 `API Token`。
2. 将机器人加入你的通知群组或频道，或给机器人发送一条消息。
3. 访问 `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` 获取你的 `chat_id`。
4. 在 Web 控制面板的【设置】选项卡中填入 Token 与 Chat ID 并点击保存，即可开启推送。
# ma-trader
