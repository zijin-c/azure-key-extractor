# Azure Key 提取工具 — VPS 部署指南

## 从 GitHub 全新一键部署

使用 Ubuntu 22.04/24.04 或 Debian 12+，以 root 登录全新服务器后执行（目标目录需尚不存在）：

```bash
apt-get update && apt-get install -y git curl ca-certificates && git clone --depth 1 --branch main https://github.com/zijin-c/azure-key-extractor.git /opt/azure-key-extractor && cd /opt/azure-key-extractor && bash install.sh
```

脚本自动安装 Python 依赖、Chromium 和浏览器系统依赖，并启动 `azure-key` Systemd 服务、设置开机自启。浏览器系统依赖使用 [Playwright 官方安装命令](https://playwright.dev/python/docs/browsers#install-system-dependencies) 按发行版选择包名。

完成后访问 `http://服务器IP:5010`，初始管理员为 `admin / admin123`；登录后修改密码。管理后台为 `/admin`。服务器安全组或防火墙需要允许 TCP 5010。查看日志：`journalctl -u azure-key -f`。

全新部署会创建独立数据库，不会自动搬迁旧服务器的账号或累计统计。

## 管理后台与累计统计

- 点击后台的用户名或用户行可进入 `/admin/users/<user_id>`，仅管理员可访问。支持今天、近 7 天、近 30 天、全部时间和自选起止日期，按小时/天/周/月查看新增 Key、首次成功账号数、每个时间段的产品类型及数量，并展示所选范围的产品占比。
- 时间段按首次记录时间归档：同一 Key 或邮箱以后重复成功，不会把原有数量移到新日期。周按周一开始，时间沿用服务器记录的墙上时间；已有无时区记录不猜测 UTC 偏移。详情在同一数据库快照中生成，避免运行中的任务使时段数与累计数不一致。
- 更新自动增加首次时间字段，并从现存历史补录。旧账本仅保存最近时间，其首次时间会标为估算；无历史可补录时标为「日期未知」，不会用最近时间冒充首次日期。日期未知记录在「全部时间」计入总数，限定日期时排除；删除历史或用户后仍保留统计及已知首次日期。
- 详情接口为 `/api/admin/users/<user_id>/details?start=YYYY-MM-DD&end=YYYY-MM-DD&group=day`，支持 `hour/day/week/month`。起止日期均包含当天；不传日期为全部时间。每次最多展示最近 500 个有数据的时间段，汇总及产品数量仍按整个范围计算。接口只返回数量与产品名，不返回密码、TOTP、邮箱或 Key 明文。
- 管理后台新增每位用户和全站的「累计成功账号」：按任务结果 `success` 为真、邮箱去除首尾空白并忽略大小写去重。同一用户多次成功处理同一邮箱只计一次，部分 Key 已获取且任务标记成功也计入；同一邮箱在不同用户下分别计数。
- 成功账号统计保存在独立的 `successful_accounts` 表，仅保存邮箱指纹、用户名及成功时间，删除历史/用户、服务重启后仍保留。升级自动补录全部现存成功历史；升级前已删除的历史无法反推账号数量。
- 点击每位用户的「代理与并发」可设置「强制填写代理池」及最大并发线程数（1–20）。默认允许直连、最大 20，保留原有行为。强制代理时不接受空白/无效节点或仅填写 VLESS 前置代理；新建任务和代理检测均校验，代理池仍支持多行、逗号、分号分隔并轮询。
- 并发输入完成后自动纠正为整数并限制在 1 到管理员上限之间，编辑时允许暂时清空。启动前从 `/api/policy` 读取当前用户的最新限制；保存设置和新建任务也在服务端校准，返回实际使用的并发数供页面显示。例如上限为 4，即使直接提交 10，也只保存和运行 4。非整数或无效类型仍返回错误。
- 并发和代理限制由服务端执行，普通用户不能通过修改页面或提交 `max_concurrency` 等参数绕过；代理检测并发也受限。策略对下次新任务、服务重启恢复和后续批次生效；当前批次不被强制中断。恢复任务的并发自动压到最新上限，新增强制代理要求但队列无代理时停止该任务，待用户补充代理重新启动。
- 管理员登录后，点击首页顶部「管理后台」，或访问 `/admin`。页面及所有后台接口均在服务端检查管理员权限；普通用户访问返回 403，未登录访问页面跳转登录。
- 后台显示每个用户的累计 Key 数、现存历史条数、最近提取时间及最后登录时间，支持搜索、创建用户、重置密码和删除普通用户，并显示各产品累计数量。
- 统计按「用户 + 产品 + Key」去重，空 Key 不计数，部分成功任务中已取得的 Key 也计入。重复提取不会增加数量，删除/清空历史不会减少数量，服务重启后仍保留；删除用户后保留该用户的累计统计，重新创建同名用户会作为新用户统计。
- 首次更新启动时自动从全部现存历史补录，之后的新结果与统计在同一个数据库事务内保存。**更新前已删除的历史无法补录。** 无需手动执行迁移。
- 累计统计位于 `data/users.db` 的独立 `key_extractions` 表，仅保存用户名、产品、Key 的 SHA-256 指纹和提取时间，不保存 Key 明文。更新和备份时请保留整个数据库。
- 普通用户仍可在「个人设置」修改自己的密码，使用 `/auth/password` 接口。

## 网页用户隔离

- 修复旧版浏览器共享 `localStorage` 存放账号/代理/结果的问题：敏感草稿只保存在本页内存，持久化从服务端按当前用户恢复。升级时清除无法确认归属的旧浏览器缓存，不将它导入任何用户；主题设置可继续共享。
- 请求绑定渲染页面时的用户 ID，同一浏览器切换账号后，旧标签页不能将旧表单写入新用户或读取新用户数据；登录/退出会通知其他标签页重载。个人页面、接口和导出使用 `Cache-Control: no-store`。已删除用户的旧 Cookie 请求返回 401，未完成的队列不再恢复。
- 修复结果表格 HTML 属性转义，并用 JSON 安全序列化页面的用户名，避免账号/密码中的引号被解释为网页属性。
- 任务接口从已登录用户派生 `sid`；传入其他用户的 `sid` 返回 403。结果、状态、日志及导出仅读取当前用户的数据，服务重启后的队列结果也验证归属。
- 当前仍为全服务单任务队列。普通用户可以停止自己的任务；停止后的后台清理完成前不能启动新任务。清理及手动重启仅限管理员，而且其他用户正在运行或清理任务时拒绝执行。
- 网页 2FA 缓存按用户存放在 `data/user_totp/<user_id>.json`，不会读取原来的共享密钥缓存或全局 JSONL。已有密钥从该用户自己的最近历史记录（最多 500 条）补充；更早的账号可在输入中携带原 TOTP 密钥。CLI 的旧缓存继续独立使用。
- 导出文件包含用户标识和随机后缀，避免同时导出时覆盖文件；带外站 Origin 或跨站 Fetch 标识的已登录写操作被拒绝。
- 更新时保留数据库、任务记录及 `data` 目录。此隔离不影响公共静态资源跨用户缓存复用。

## 网页导出与重复修复

- 操作区仅保留「导出成功」「导出失败」「清除历史」，不再区分本次和历史；移除全部导出、选中导出、选中删除及表格勾选框。
- 导出包含当前用户全部尚未清除的成功/失败记录，跨批次和页面刷新均有效。每次导出重新请求 `/api/history?all=1&filter=success`（或 `failed`），不会使用本页最近任务缓存，也不受列表最近 500 条的显示限制。查询失败时停止导出，不使用旧缓存冒充完整数据；清除历史后不再导出这些记录，后台累计统计继续保留。
- 导出保留每条服务端记录的原始时间，不再把所有行改成页面拉取时间。历史中的不同任务记录仍逐条保留，不按邮箱擅自合并。
- 修复任务结束时将读取游标清零、却再次向原缓存追加完整结果的错误。现在以 `/api/results` 的完整快照替换运行结果缓存，重复拉取、完成通知重播以及同时拉取都不会重复追加；自动重启或暂时断网时保留已同步运行结果。
- 新任务启动成功后才清空上一轮缓存，启动失败不会丢掉可导出的结果；旧请求迟到或登录失效时不能恢复旧缓存。历史中不同任务真实产生的多条记录继续保留，不按邮箱擅自合并。
- 更新服务后刷新网页以加载新按钮和并发纠正功能，再重新导出。此变更不修改数据库历史，也不会自动修改已经下载的文件。

## 系统要求

- **OS**: Ubuntu 20.04+ / Debian 11+ / CentOS 8+（推荐 Ubuntu 22.04）
- **内存**: 最低 2GB（推荐 4GB，每个浏览器实例约 300-500MB）
- **CPU**: 2 核+
- **磁盘**: 10GB+
- **网络**: 需要能访问 `*.microsoft.com`、`*.azure.com`、`*.live.com`

## 一、安装依赖

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Python 3.11+
sudo apt install -y python3 python3-pip python3-venv

# 安装 Playwright 系统依赖（Chromium 需要的库）
sudo apt install -y \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
  libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
  libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
  fonts-liberation xvfb
```

## 二、部署项目

```bash
# 创建项目目录
mkdir -p /opt/azure-key-extractor
cd /opt/azure-key-extractor

# 上传项目文件（scp / rsync / git clone）
# 例如：
# scp -r ./azure-key-extractor/* user@vps:/opt/azure-key-extractor/

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright Chromium
playwright install chromium
```

## 三、配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑配置
nano .env
```

`.env` 关键配置：

```ini
# VPS 上必须开启无头模式
HEADLESS=true

# 代理（如果 VPS 本身 IP 干净可以不填）
PROXY=socks5://user:pass@proxy-host:port

# 超时（网络慢的 VPS 可以加大）
PAGE_LOAD_TIMEOUT=120000
ELEMENT_TIMEOUT=45000
PORTAL_WAIT=180
```

## 四、运行

### 方式 A：直接运行（前台）

```bash
cd /opt/azure-key-extractor
source venv/bin/activate
python run_gui.py
```

访问 `http://VPS_IP:5010`

### 方式 B：后台运行（推荐）

```bash
# 使用 nohup
nohup python run_gui.py > /var/log/azure-key.log 2>&1 &

# 或使用 screen
screen -S azure-key
python run_gui.py
# Ctrl+A, D 分离
# screen -r azure-key 重新连接
```

### 方式 C：Systemd 服务（生产推荐）

```bash
sudo nano /etc/systemd/system/azure-key.service
```

内容：

```ini
[Unit]
Description=Azure Key Extractor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/azure-key-extractor
Environment=PATH=/opt/azure-key-extractor/venv/bin:/usr/bin
Environment=MALLOC_ARENA_MAX=2
Environment=MALLOC_TRIM_THRESHOLD_=131072
Environment=HEADLESS=true
ExecStart=/opt/azure-key-extractor/venv/bin/python run_gui.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable azure-key
sudo systemctl start azure-key

# 查看状态
sudo systemctl status azure-key

# 查看日志
journalctl -u azure-key -f
```

## 五、Nginx 反向代理（可选，加 HTTPS）

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

```nginx
# /etc/nginx/sites-available/azure-key
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5010;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;  # SSE 长连接
    }

    # SSE 专用（禁用缓冲）
    location /stream {
        proxy_pass http://127.0.0.1:5010/stream;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/azure-key /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 申请 HTTPS 证书
sudo certbot --nginx -d your-domain.com
```

## 六、安全加固

### 1. 添加访问密码（Basic Auth）

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd admin
```

在 Nginx 配置的 `location /` 里加：

```nginx
auth_basic "Azure Key Tool";
auth_basic_user_file /etc/nginx/.htpasswd;
```

### 2. 防火墙

```bash
# 只开放 SSH + HTTP/HTTPS
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable

# 如果不用 Nginx，直接开放 5010
# sudo ufw allow 5010
```

### 3. 限制访问 IP（可选）

```nginx
# Nginx 里限制只允许你的 IP
allow 你的IP;
deny all;
```

## 七、Xvfb 虚拟显示（非无头模式调试用）

如果需要在 VPS 上以非无头模式运行（调试用）：

```bash
# 安装 Xvfb
sudo apt install -y xvfb

# 启动虚拟显示
Xvfb :99 -screen 0 1920x1080x24 &
export DISPLAY=:99

# 然后正常运行
python run_gui.py
```

配合 VNC 可以远程看到浏览器画面：

```bash
sudo apt install -y x11vnc
x11vnc -display :99 -forever -nopw &
# 用 VNC 客户端连接 VPS_IP:5900
```

## 八、常见问题

### Q: Chromium 启动失败

```
Error: Failed to launch browser
```

解决：

```bash
# 安装所有依赖
playwright install-deps chromium

# 或手动安装
sudo apt install -y libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
  libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxcb1 \
  libxkbcommon0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
  libasound2 libatspi2.0-0
```

### Q: 端口被占用

```bash
# 查看占用
lsof -i :5010
# 杀掉
kill -9 <PID>
```

### Q: 内存不足

每个浏览器实例约 300-500MB。如果跑多账号：

```bash
# 增加 swap
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Q: 结果文件在哪

```
/opt/azure-key-extractor/results/
├── keys_20260516.jsonl      # 每日 JSONL 日志
└── azure_keys_*.xlsx        # 自动导出的 Excel
```

## 九、更新部署

```bash
cd /opt/azure-key-extractor
# 上传新文件后
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart azure-key
```

## 十、发送给他人使用（绿色脱机便携版打包）

如果你需要将此工具发送给其他人使用（对方电脑**无需安装 Python、无需配置环境**）：

### 1. 一键生成便携包
在 Windows 环境下直接双击根目录下的 **`一键打包.bat`**：
- **[1] 轻量便携版 (`AzureKeyExtractor_v1.0_Portable.zip`，约 55MB)**：
  - 体积小，适合微信、QQ、邮件极速传输。
  - 运行时自动优先调取接收方系统的 Chrome / Edge 浏览器。
- **[2] 全内置离线版 (`AzureKeyExtractor_v1.0_Full_Portable.zip`，约 240MB) [推荐]**：
  - 内置独立的 Chromium 浏览器内核，接收方电脑**完全脱机离线、100% 解压即用**。
  - 彻底规避系统 Edge 浏览器的“Windows Hello / 通行密钥 Passkey”弹窗干扰。

### 2. 发送与使用
1. 进入项目根目录下的 **`dist/`** 文件夹，直接将生成的 `.zip` 压缩文件发送给对方。
2. 对方收到后，**先解压到任意文件夹**。
3. 双击文件夹内的 **`一键启动.bat`**，系统将自动启动后台并弹出默认浏览器打开 `http://localhost:5010` 图形控制台。

