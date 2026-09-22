"""
VLESS → SOCKS5 代理链管理器
通过 Xray-core 子进程实现代理链:
  本地浏览器 → Xray 本地 SOCKS5 → VLESS 前置代理 → 远程 SOCKS5 → 目标网站
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)


def parse_vless_url(url: str) -> dict:
    p = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    return {
        "uuid":        p.username or "",
        "host":        p.hostname or "",
        "port":        int(p.port or 443),
        "encryption":  q.get("encryption", "none"),
        "flow":        q.get("flow", ""),
        "security":    q.get("security", "none"),
        "sni":         q.get("sni", ""),
        "fp":          q.get("fp", "chrome"),
        "pbk":         q.get("pbk", ""),
        "sid":         q.get("sid", ""),
        "net_type":    q.get("type", "tcp"),
        "header_type": q.get("headerType", "none"),
    }


def parse_socks5_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host": p.hostname or "",
        "port": int(p.port or 1080),
        "user": p.username or "",
        "pass": p.password or "",
    }


def build_xray_config(vless: dict, socks5: dict, local_port: int) -> dict:
    stream: dict = {"network": vless["net_type"]}
    if vless["security"] == "reality":
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName":  vless["sni"],
            "fingerprint": vless["fp"],
            "publicKey":   vless["pbk"],
            "shortId":     vless["sid"],
        }
    elif vless["security"] == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = {
            "serverName":  vless["sni"],
            "fingerprint": vless.get("fp", "chrome"),
        }

    vless_user: dict = {"id": vless["uuid"], "encryption": vless["encryption"]}
    if vless["flow"]:
        vless_user["flow"] = vless["flow"]

    vless_outbound = {
        "tag":      "vless-front",
        "protocol": "vless",
        "settings": {"vnext": [{"address": vless["host"], "port": vless["port"], "users": [vless_user]}]},
        "streamSettings": stream,
    }

    socks5_server: dict = {"address": socks5["host"], "port": socks5["port"]}
    if socks5["user"]:
        socks5_server["users"] = [{"user": socks5["user"], "pass": socks5["pass"]}]

    socks5_outbound = {
        "tag":      "socks5-chain",
        "protocol": "socks",
        "settings": {"servers": [socks5_server]},
        "proxySettings": {"tag": "vless-front", "transportLayer": True},
    }

    inbound = {
        "tag": "local-in", "port": local_port, "listen": "127.0.0.1",
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False},
        "sniffing": {"enabled": False},
    }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [socks5_outbound, vless_outbound],
        "routing": {"rules": [{"type": "field", "inboundTag": ["local-in"], "outboundTag": "socks5-chain"}]},
    }


def find_xray() -> str | None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = ["xray.exe", "xray"] if sys.platform == "win32" else ["xray"]
    candidates = [os.path.join(root, n) for n in names]
    for n in names:
        found = shutil.which(n)
        if found:
            candidates.insert(0, found)
    candidates += [r"C:\Program Files\xray\xray.exe", r"C:\xray\xray.exe"]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class XrayProxyChain:
    def __init__(self, vless_url: str, socks5_url: str):
        self.vless_cfg  = parse_vless_url(vless_url)
        self.socks5_cfg = parse_socks5_url(socks5_url)
        self.local_port = _free_port()
        self._proc: subprocess.Popen | None = None
        self._cfg_path: str | None = None

    @property
    def local_proxy_url(self) -> str:
        return f"socks5://127.0.0.1:{self.local_port}"

    def start(self) -> str:
        xray = find_xray()
        if not xray:
            raise RuntimeError(
                "未找到 xray 可执行文件！\n"
                "请从 https://github.com/XTLS/Xray-core/releases 下载 xray.exe\n"
                "并放到项目根目录（与 run_gui.py 同级）。"
            )
        cfg = build_xray_config(self.vless_cfg, self.socks5_cfg, self.local_port)
        fd, path = tempfile.mkstemp(suffix=".json", prefix="xray_chain_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        self._cfg_path = path

        popen_kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._proc = subprocess.Popen([xray, "run", "-c", path], **popen_kwargs)
        try:
            from utils.process_manager import BrowserProcessManager
            if self._proc and self._proc.pid:
                BrowserProcessManager.register_driver(self._proc.pid)
        except Exception:
            pass

        for _ in range(34):
            time.sleep(0.3)
            if self._proc.poll() is not None:
                raise RuntimeError(f"Xray 进程意外退出，code={self._proc.returncode}")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", self.local_port)) == 0:
                    return self.local_proxy_url

        raise RuntimeError(f"Xray 启动超时，端口 {self.local_port} 未就绪")

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._cfg_path and os.path.exists(self._cfg_path):
            try:
                os.unlink(self._cfg_path)
            except Exception:
                pass
            self._cfg_path = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
