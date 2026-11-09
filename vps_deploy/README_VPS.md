# Azure Key 提取工具 — Linux VPS 纯 Systemd 部署指南

本方案专为在云端 Linux VPS（如 Ubuntu 20.04 / 22.04 / 24.04、Debian 11 / 12 等）上部署设计，采用 **Systemd 生产级守护进程** 管理服务，并全面启用**极致省流量机制**（本地 HTTP 强缓存 + 图片/字体/遥测拦截 + 纯无头模式）。

### 管理后台与累计提取统计

操作区仅保留「导出成功」「导出失败」「清除历史」。导出会从服务器读取该用户全部未清除的成功或失败记录，包含之前批次，不受列表最近 500 条限制；保留各行原始时间，且不使用最近任务缓存。重复拉取导致整批结果重复追加的问题已修复，数据库中不同任务的真实历史记录继续保留。

并发框在输入完成后自动纠正到允许的整数范围，启动前获取后台最新上限，服务器保存和运行时再校准。例如后台上限 4、输入 10，显示和实际运行均为 4。更新并重启服务后刷新网页生效。

点击用户名或用户行可进入该用户的提取详情（含已删除用户的保留统计），支持自选日期、今天/近 7 天/近 30 天/全部时间，以及按小时/天/周/月分组。每个时间段显示新增 Key、首次成功账号数及产品类型数量，右侧显示类型分布和占比。仍然仅管理员可访问。

时段按首次记录归档，重复提取不增加或移动原有数量。升级会从现存历史补录首次时间；旧数据日期可能是估算，无法确定的单独列为「日期未知」。日期未知仅计入「全部时间」，不纳入限定日期的范围；详情不返回账号凭据和 Key 明文。新增模板及首次时间字段会随整个 `gui` 目录更新和服务重启自动生效。

后台每位用户的「代理与并发」可设置强制填写代理池开关和最大并发线程数（1–20，默认允许直连、最多 20）。服务端对新任务、恢复任务、后续批次和代理检测执行限制，普通用户不能通过改页面绕过；当前运行批次不被中断。

新增「累计成功账号」，按每位用户成功过的邮箱忽略大小写去重，删除历史/用户后仍保留；启动时从全部现存成功历史补录，更新前已删除的历史无法追溯。网页不再将账号、代理凭据、Key 持久保存在浏览器；切换账号时旧标签页会重载，旧页面的请求不能操作新用户。删除用户后旧登录会话失效，未完成队列不再恢复。

管理员登录后可从首页进入「管理后台」或访问 `/admin`。普通用户不能访问后台页面和接口。后台支持用户搜索、创建、重置密码、删除普通用户，显示每位用户及各产品的累计 Key 数。

累计数量按「用户 + 产品 + Key」去重，空 Key 不计数；部分成功任务中已取得的 Key 也计入。删除或清空历史不会扣减累计数量，删除用户后也保留统计，服务重启不会丢失。首次升级启动时会自动补录全部现存历史，**升级前已删除的历史无法追溯**。

更新时同步整个 `gui` 目录（包含模板和 `static/session.js`），并重启服务。保留和备份 `data/users.db`；累计统计独立保存在其中的 `key_extractions` 和 `successful_accounts` 表，策略在 `user_policies` 表中。统计仅记录 Key 和邮箱指纹，不记录其明文。

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
