# Azure Key 提取工具 — VPS 部署指南

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

