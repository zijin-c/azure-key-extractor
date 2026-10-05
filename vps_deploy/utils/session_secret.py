"""每个部署独立的持久化会话密钥，服务重启后保持登录会话有效。"""
import os
import secrets
import time
from pathlib import Path


def load_session_secret(path=None):
    configured = os.getenv("AZURE_KEY_SESSION_SECRET", "").strip()
    if configured:
        if len(configured) < 32:
            raise ValueError("AZURE_KEY_SESSION_SECRET 至少需要 32 个字符")
        return configured
    path = Path(path) if path else Path(__file__).resolve().parents[1] / "data" / "session_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # 另一个启动进程可能刚创建文件，等待其完成写入。
        for _ in range(20):
            secret = path.read_text(encoding="ascii").strip()
            if len(secret) >= 32:
                return secret
            time.sleep(0.05)
        raise RuntimeError("会话密钥文件为空或损坏，请检查 data/session_secret")
    else:
        secret = secrets.token_hex(32)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secret)
            f.flush()
            os.fsync(f.fileno())
        return secret
