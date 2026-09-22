# Azure Key 提取工具 — Linux VPS 纯 Systemd 部署指南

本方案专为在云端 Linux VPS（如 Ubuntu 20.04 / 22.04 / 24.04、Debian 11 / 12 等）上部署设计，采用 **Systemd 生产级守护进程** 管理服务，并全面启用**极致省流量机制**（本地 HTTP 强缓存 + 图片/字体/遥测拦截 + 纯无头模式）。

---

## 目录
1. [服务器要求](#一服务器要求)
2. [第一步：将文件上传到 VPS](#二第一步将文件上传到-vps)
3. [第二步：一键全自动安装与 Systemd 注册](#三第二步一键全自动安装与-systemd-注册)
4. [第三步：配置环境变量（.env）](#四第三步配置环境变量env)
5. [第四步：Systemd 服务日常运维与日志排查](#五第四步systemd-服务日常运维与日志排查)
6. [第五步：防火墙与端口开放](#六第五步防火墙与端口开放)
7. [第六步：配置 Nginx + 域名 + HTTPS（可选但推荐）](#七第六步配置-nginx--域名--https可选但推荐)
8. [第七步：Web 控制台使用](#八第七步web-控制台使用)
9. [常见问题与排查（FAQ）](#九常见问题与排查faq)

---

## 一、服务器要求

- **操作系统**：Ubuntu 20.04 / 22.04 / 24.04 或 Debian 11 / 12（推荐 Ubuntu 22.04 LTS）
- **CPU**：1 核或以上
- **内存**：建议 $\ge$ 1GB（若内存为 1GB，**强烈建议开启 2GB~4GB Swap 虚拟内存**）
- **磁盘**：剩余空间 $\ge$ 5GB
- **网络**：海外 VPS 直连微软，如为国内 VPS 需在 `.env` 中配置 `PROXY` 代理。

---

## 二、第一步：将文件上传到 VPS

推荐将项目部署在 `/opt/azure-key-extractor` 目录。

### 方法 1：直接上传 `vps_deploy.zip` 并解压（推荐）
在 VPS 终端执行：
```bash
# 1. 创建目标目录
sudo mkdir -p /opt/azure-key-extractor
cd /opt/azure-key-extractor

# 2. 安装 unzip 解压工具
sudo apt update && sudo apt install -y unzip

# 3. 将本地的 vps_deploy.zip 上传至该目录后执行解压
sudo unzip vps_deploy.zip
```

### 方法 2：使用 SCP 从本地上传整个文件夹
在 Windows 终端（PowerShell 或 CMD）执行：
```bash
scp -r "d:/Work/azure-key-extractor - outlook/vps_deploy/*" root@<你的VPS_IP>:/opt/azure-key-extractor/
```

---

## 三、第二步：一键全自动安装与 Systemd 注册

登录 VPS 终端，进入项目目录执行一键安装脚本：

```bash
cd /opt/azure-key-extractor

# 赋予执行权限并运行
chmod +x install.sh
bash install.sh
```

### 脚本会自动完成以下全套流程：
1. 更新系统并自动安装 Python 3、pip、venv 以及 Playwright Chromium 必需的底层图形与字体库。
2. 创建独立的 Python 虚拟环境 `venv` 并安装全部依赖。
3. 自动在 VPS 本地下载适用于 Linux 架构的 Chromium 浏览器至 `./ms-playwright`。
4. 自动生成默认的 `.env` 配置文件（预设 `HEADLESS=true` 与 `ENABLE_SAVE_DATA=true`）。
5. **自动生成 `/etc/systemd/system/azure-key.service` 系统服务配置，重载 systemd 并自动启动服务与设置开机自启**。

---

## 四、第三步：配置环境变量（.env）

如果需要修改配置（如配置代理、调整超时时间），请编辑 `.env`：

```bash
cd /opt/azure-key-extractor
nano .env
```

核心配置项说明：

```ini
# =================================================================
# Azure Key Extractor - Linux VPS 配置
# =================================================================

# 1. 浏览器设置 (VPS 必须为 true)
HEADLESS=true
SLOW_MO=20
TIMEOUT=60000

# 2. 省流量模式 (必须保持 true)
# 开启本地 HTTP 静态资源强缓存，拦截非必要图片/字体/遥测与 Copilot AI
ENABLE_SAVE_DATA=true

# 3. 代理设置 (VPS 本身在海外且 IP 干净可留空；需走代理时填写)
# 格式: socks5://user:pass@host:port 或 http://host:port
PROXY=

# 4. 超时配置 (毫秒)
PAGE_LOAD_TIMEOUT=120000
ELEMENT_TIMEOUT=45000
PORTAL_WAIT=180
```

*修改完成后按 `Ctrl + O` 回车保存，`Ctrl + X` 退出，然后重启服务使配置生效：*
```bash
sudo systemctl restart azure-key
```

---

## 五、第四步：Systemd 服务日常运维与日志排查

本项目已完全由 Systemd 守护管理，所有操作统一使用标准 `systemctl` 与 `journalctl` 命令：

| 操作需求 | 执行命令 |
| :--- | :--- |
| **查看服务当前状态** | `sudo systemctl status azure-key` |
| **查看实时运行日志** | `sudo journalctl -u azure-key -f` |
| **查看最近 100 行日志** | `sudo journalctl -u azure-key -n 100 --no-pager` |
| **重启服务** | `sudo systemctl restart azure-key` |
| **停止服务** | `sudo systemctl stop azure-key` |
| **启动服务** | `sudo systemctl start azure-key` |
| **设置开机自启** | `sudo systemctl enable azure-key` |
| **禁止开机自启** | `sudo systemctl disable azure-key` |

---

## 六、第五步：防火墙与端口开放

服务默认监听 **TCP 5010** 端口。

1. **UFW 防火墙放行**：
   ```bash
   sudo ufw allow 5010/tcp
   sudo ufw reload
   ```

2. **云服务商安全组**：
   若使用阿里云、腾讯云、AWS、Oracle Cloud、华为云等，请前往云控制台 -> **安全组 / 规则**，添加入方向规则放行 **5010** 端口。

---

## 七、第六步：配置 Nginx + 域名 + HTTPS（可选但推荐）

若想通过个人域名安全访问后台，建议使用 Nginx 反代并配置 SSL 证书：

### 1. 安装 Nginx 与 Certbot
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 2. 新建 Nginx 站点配置
```bash
sudo nano /etc/nginx/sites-available/azure-key
```

填入以下内容（将 `your-domain.com` 替换为您的域名）：
```nginx
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
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # SSE 实时控制台日志流（禁用代理缓冲）
    location /stream {
        proxy_pass http://127.0.0.1:5010/stream;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }
}
```

### 3. 启用配置并一键签发免费 HTTPS 证书
```bash
sudo ln -s /etc/nginx/sites-available/azure-key /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# 自动配置 HTTPS 证书
sudo certbot --nginx -d your-domain.com
```

---

## 八、第七步：Web 控制台使用

1. 打开浏览器访问：
   - 直接访问：`http://<你的VPS_IP>:5010`
   - 域名访问：`https://your-domain.com`
2. **初始登录信息**：
   - 默认账号：`admin`
   - 默认密码：`admin123`
3. 登录后请进入「用户管理」修改初始密码。
4. 录入微软教育邮箱账号、密码、TOTP Secret 后点击提取，程序将在后台自动完成认证并提取 Key。
5. 提取到的密钥保存在 VPS 上的 `results/` 目录下，并可直接在 Web 界面一键导出 Excel。

---

## 九、常见问题与排查（FAQ）

### Q1: 内存不足（1GB 内存 VPS）导致 Chromium 偶发闪退？
**解决方案**：为 VPS 一键添加 4GB Swap 虚拟内存：
```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### Q2: 提示缺少底层动态库 `missing shared library`？
**解决方案**：执行 Playwright 自动补全库：
```bash
cd /opt/azure-key-extractor
source venv/bin/activate
python -m playwright install-deps chromium
sudo systemctl restart azure-key
```

### Q3: 如何更新 VPS 上的代码？
**解决方案**：
将新代码上传覆盖到 `/opt/azure-key-extractor` 目录后执行：
```bash
cd /opt/azure-key-extractor
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart azure-key
```
