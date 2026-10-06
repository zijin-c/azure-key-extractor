"""公共静态资源的磁盘缓存；以完整 URL 区分版本，跨账号复用。"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

_AUTO_SIZE = object()
log = logging.getLogger(__name__)


def transfer_body_size(headers, decoded_size=None):
    """压缩响应使用传输 Content-Length；旧缓存未知时不冒充实测尺寸。"""
    headers = {k.lower(): v for k, v in headers.items()}
    try:
        size = int(headers.get("content-length", ""))
        if size >= 0:
            return size
    except (ValueError, TypeError):
        pass
    if not headers.get("content-encoding") and decoded_size is not None:
        return decoded_size
    return None


class LocalHttpCache:
    CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "python_http_cache")
    CANONICAL_DIR = os.path.join(CACHE_DIR, "canonical_manifests")
    HASH_MAP_FILE = os.path.join(CANONICAL_DIR, "manifest_hashes.json")
    CACHEABLE_EXTENSIONS = (".js", ".mjs", ".css", ".woff2", ".woff", ".ttf", ".otf", ".eot", ".svg", ".ico", ".png", ".jpg", ".jpeg", ".webp", ".json")
    AUTH_ASSET_EXTENSIONS = tuple(ext for ext in CACHEABLE_EXTENSIONS if ext != ".json")
    DYNAMIC_SECURITY_PATTERNS = (
        "arkose", "arkoselabs", "funcaptcha", "powseq", "turnstile", "challenges.cloudflare.com",
        "challenge-platform", "hcaptcha", "recaptcha", "geetest", "/fc/", "/challenge", "/enforcement",
        "services.sheerid.com/api/", "services.sheerid.com/rest/", "services.sheerid.com/verify/",
        "services.sheerid.com/submission/", "services.sheerid.com/orgsearch", "sheerid.com/api/",
        "/common/oauth2/", "/oauth20_authorize", "/login.srf", "/kmsi", "/getcredentialtype",
        "/getsessionstate", "/sas/processauth", "/ppsecure/post.srf", "/processauth",
        "/oauth2/token", "/oauth2/v2.0/token", "/reprocess", "/federal/login",
        "/api/authenticationmethods/", "management.azure.com/", "graph.microsoft.com/",
        "signup.azure.com/api/", "signup.azure.com/studentverification", "signup.azure.com/signup?offer=",
        "expires=", "signature=", "key-pair-id=",
    )
    _TYPES = ("extensionConfiguration", "assetTypesBrowse", "assetTypes", "browseMenus", "featureCards", "portalServices", "tourGuide")
    _disk_lock = threading.RLock()
    _fill_guard = threading.Lock()
    _fills = {}
    _manifest_hash_to_type = {}
    _canonical_initialized = False
    _cleanup_times = {}

    @classmethod
    def cleanup(cls, force=False):
        """每小时按闲置时间及容量清理自身缓存文件；不递归访问其他数据目录。"""
        def setting(name, default):
            try:
                value = int(os.getenv(name, default))
                return value if value > 0 else default
            except (TypeError, ValueError):
                return default
        max_bytes = setting("HTTP_CACHE_MAX_MB", 512) * 1048576
        idle_seconds = setting("HTTP_CACHE_IDLE_DAYS", 30) * 86400
        now = time.time()
        root = os.path.abspath(cls.CACHE_DIR)
        removed = reclaimed = 0
        with cls._disk_lock:
            previous = cls._cleanup_times.get(root)
            if not force and previous is not None and time.monotonic() - previous < 3600:
                return {"removed": 0, "bytes": 0}
            cls._cleanup_times[root] = time.monotonic()
            entries = []
            for directory in (cls.CACHE_DIR, cls.CANONICAL_DIR):
                # 即使目录被替换成符号链接，也不跟随它清理其他位置。
                if os.path.islink(directory) or not os.path.isdir(directory):
                    continue
                for name in os.listdir(directory):
                    path = os.path.join(directory, name)
                    if os.path.islink(path) or not os.path.isfile(path):
                        continue
                    if name.startswith(".cache-"):
                        if now - os.path.getmtime(path) > 86400:
                            size = os.path.getsize(path)
                            os.remove(path)
                            removed += 1
                            reclaimed += size
                        continue
                    stem, suffix = os.path.splitext(name)
                    known = bool(re.fullmatch(r"[a-f0-9]{64}", stem)) if directory == cls.CACHE_DIR else stem in cls._TYPES
                    if not known or suffix not in (".body", ".meta"):
                        continue
                    companion = os.path.join(directory, stem + (".meta" if suffix == ".body" else ".body"))
                    if not os.path.isfile(companion) or os.path.islink(companion):
                        # 新下载/旧版写入留出一小时保护窗口。
                        if now - os.path.getmtime(path) > 3600:
                            size = os.path.getsize(path)
                            os.remove(path)
                            removed += 1
                            reclaimed += size
                        continue
                    if suffix != ".body":
                        continue
                    accessed = os.path.getmtime(path)
                    size = os.path.getsize(path) + os.path.getsize(companion)
                    invalid = False
                    try:
                        with open(companion, encoding="utf-8") as f:
                            meta = json.load(f)
                        invalid = not isinstance(meta, dict) or not isinstance(meta.get("headers"), dict) or not meta.get("url")
                    except (OSError, ValueError, TypeError, UnicodeError):
                        invalid = True
                    entries.append((accessed, size, path, companion, invalid))
            total = sum(entry[1] for entry in entries)
            for accessed, size, body, meta, invalid in sorted(entries):
                if not invalid and now - accessed <= idle_seconds and total <= max_bytes:
                    continue
                os.remove(body)
                os.remove(meta)
                total -= size
                removed += 2
                reclaimed += size
            if removed:
                log.info("静态缓存清理：删除 %s 个文件，回收 %.2f MB", removed, reclaimed / 1048576)
        return {"removed": removed, "bytes": reclaimed}

    @staticmethod
    def _touch(path):
        # 用 body 的修改时间记录最近使用，不改变资源 saved_at/版本有效期。
        try:
            if time.time() - os.path.getmtime(path) >= 3600:
                os.utime(path, None)
        except OSError:
            pass

    @staticmethod
    def _identity(url):
        return url.split("#", 1)[0]

    @classmethod
    def _url_to_key(cls, url):
        return hashlib.sha256(cls._identity(url).encode()).hexdigest()

    @classmethod
    def is_cacheable(cls, url, method="GET"):
        if method.upper() != "GET":
            return False
        lower = url.lower()
        if any(pattern in lower for pattern in cls.DYNAMIC_SECURITY_PATTERNS):
            # 验证码 CDN 的明确公共字体/样式可复用，动态挑战始终放行。
            path = urlsplit(lower).path
            return ("/style-manager/fonts/" in path or "/assets/style-manager/" in path or "/fc/assets/" in path) and path.endswith((".css", ".woff", ".woff2", ".ttf", ".otf"))
        parsed = urlsplit(lower)
        if "/api/" in parsed.path:
            return False
        if parsed.hostname in ("mysignins.microsoft.com", "login.microsoftonline.com"):
            return parsed.path.endswith(cls.AUTH_ASSET_EXTENSIONS)
        return parsed.path.endswith(cls.CACHEABLE_EXTENSIONS) or any(path in parsed.path for path in ("/bundle/", "/shared/1.0/", "/ests/2.1/", "/fonts/", "/content/scripts/", "/content/portalrequireconfig/"))

    @classmethod
    def _read(cls, url):
        keys = [cls._url_to_key(url)]
        legacy = hashlib.sha256(url.split("?")[0].split("#")[0].encode()).hexdigest()
        if legacy not in keys:
            keys.append(legacy)
        with cls._disk_lock:
            for key in keys:
                try:
                    with open(os.path.join(cls.CACHE_DIR, key + ".meta"), encoding="utf-8") as f:
                        meta = json.load(f)
                    # 旧缓存只有保存的完整 URL 相同才可迁移，避免把 v1 当作 v2。
                    if cls._identity(meta.get("url", "")) != cls._identity(url):
                        continue
                    with open(os.path.join(cls.CACHE_DIR, key + ".body"), "rb") as f:
                        body = f.read()
                    if not body or not meta.get("headers"):
                        continue
                    digest = meta.get("body_sha256")
                    if digest and hashlib.sha256(body).hexdigest() != digest:
                        continue
                    # 未带内容版本的静态路径定期重新获取，hash/version URL 保持长缓存。
                    parsed = urlsplit(url)
                    versioned = bool(parsed.query) or any(len(part) >= 8 and all(c in "0123456789abcdefABCDEF" for c in part) for part in parsed.path.replace("-", ".").replace("/", ".").split("."))
                    saved = meta.get("saved_at", os.path.getmtime(os.path.join(cls.CACHE_DIR, key + ".meta")))
                    if not versioned and time.time() - saved > 86400:
                        continue
                    cls._touch(os.path.join(cls.CACHE_DIR, key + ".body"))
                    return body, meta
                except (OSError, ValueError, TypeError):
                    continue
        return None

    @classmethod
    def get(cls, url):
        if not cls.is_cacheable(url):
            return None
        entry = cls._read(url)
        return (entry[0], entry[1]["headers"], entry[1].get("status", 200)) if entry else None

    @classmethod
    def get_wire_size(cls, url):
        entry = cls._read(url)
        return entry[1].get("wire_body_bytes") if entry else None

    @staticmethod
    def _atomic_write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cache-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    @classmethod
    def put(cls, url, body, headers, status=200, wire_body_bytes=_AUTO_SIZE):
        if not body or status != 200 or not cls.is_cacheable(url):
            return
        lowered = {k.lower(): v for k, v in headers.items()}
        if "set-cookie" in lowered or "no-store" in lowered.get("cache-control", "").lower():
            return
        if any(word in lowered.get("vary", "").lower() for word in ("cookie", "authorization", "accept-language", "*")):
            return
        # 磁盘 body 已解压，不能重放原 content-encoding/长度。
        skip = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive", "set-cookie"}
        clean = {k: v for k, v in lowered.items() if k not in skip}
        clean.setdefault("content-type", "application/octet-stream")
        if wire_body_bytes is _AUTO_SIZE:
            wire_body_bytes = transfer_body_size(lowered, len(body))
        meta = {"url": cls._identity(url), "headers": clean, "status": status, "saved_at": time.time(), "wire_body_bytes": wire_body_bytes, "body_sha256": hashlib.sha256(body).hexdigest()}
        key = cls._url_to_key(url)
        with cls._disk_lock:
            cls._atomic_write(os.path.join(cls.CACHE_DIR, key + ".body"), body)
            cls._atomic_write(os.path.join(cls.CACHE_DIR, key + ".meta"), json.dumps(meta).encode())
        try:
            cls.cleanup()
        except OSError as error:
            log.warning("静态缓存清理暂未完成，将继续使用缓存: %s", error)

    @classmethod
    @asynccontextmanager
    async def download_lock(cls, url):
        key = (asyncio.get_running_loop(), cls._identity(url))
        with cls._fill_guard:
            entry = cls._fills.setdefault(key, [asyncio.Lock(), 0])
            entry[1] += 1
        acquired = False
        try:
            await entry[0].acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry[0].release()
            with cls._fill_guard:
                entry[1] -= 1
                if not entry[1]:
                    cls._fills.pop(key, None)

    @classmethod
    def detect_manifest_type(cls, data):
        manifest = data.get("manifest") if isinstance(data, dict) else None
        if not isinstance(manifest, dict):
            return None
        for extension in manifest.values():
            if isinstance(extension, dict):
                for kind in cls._TYPES:
                    if kind in extension or kind == "tourGuide" and "tourIds" in extension:
                        return kind
                if "featureCardEnvironmentConfiguration" in extension:
                    return "featureCards"
        return None

    @classmethod
    def init_canonical_manifests(cls):
        with cls._disk_lock:
            if cls._canonical_initialized:
                return
            try:
                with open(cls.HASH_MAP_FILE, encoding="utf-8") as f:
                    cls._manifest_hash_to_type.update(json.load(f))
            except (OSError, ValueError, TypeError):
                pass
            cls._canonical_initialized = True

    @classmethod
    def learn_hash_mapping(cls, hash_name, kind):
        if kind not in cls._TYPES:
            return
        with cls._disk_lock:
            cls._manifest_hash_to_type[hash_name] = kind
            while len(cls._manifest_hash_to_type) > 512:
                cls._manifest_hash_to_type.pop(next(iter(cls._manifest_hash_to_type)))
            cls._atomic_write(cls.HASH_MAP_FILE, json.dumps(cls._manifest_hash_to_type).encode())

    @classmethod
    def learn_hashes_from_html(cls, content):
        # 不再凭 body 大小猜清单类型；已有 HTML 学习仅作候选索引。
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="ignore")
        if not content or "extensionsManifestHash" not in content:
            return
        try:
            start = content.index("{", content.index("extensionsManifestHash"))
            data, _ = json.JSONDecoder().raw_decode(content[start:])
            learned = {}
            for kind, values in data.items():
                if kind not in cls._TYPES:
                    continue
                stack = [values]
                while stack:
                    value = stack.pop()
                    if isinstance(value, list):
                        stack.extend(value)
                    elif isinstance(value, str):
                        learned[value] = kind
            if learned:
                with cls._disk_lock:
                    cls._manifest_hash_to_type.update(learned)
                    while len(cls._manifest_hash_to_type) > 512:
                        cls._manifest_hash_to_type.pop(next(iter(cls._manifest_hash_to_type)))
                    cls._atomic_write(cls.HASH_MAP_FILE, json.dumps(cls._manifest_hash_to_type).encode())
        except (ValueError, TypeError, AttributeError):
            pass

    @classmethod
    def _manifest_kind(cls, url):
        cls.init_canonical_manifests()
        parsed = urlsplit(url)
        kind = parse_qs(parsed.query).get("m_type", [None])[0]
        name = parsed.path.rsplit("/", 1)[-1]
        return kind if kind in cls._TYPES else cls._manifest_hash_to_type.get(name) or cls._manifest_hash_to_type.get(name.removesuffix(".json"))

    @classmethod
    def manifest_candidate(cls, url):
        kind = cls._manifest_kind(url)
        if kind not in cls._TYPES:
            return None
        with cls._disk_lock:
            try:
                with open(os.path.join(cls.CANONICAL_DIR, kind + ".meta"), encoding="utf-8") as f:
                    meta = json.load(f)
                original, requested = urlsplit(meta.get("url", "")), urlsplit(url)
                old_query, new_query = parse_qs(original.query), parse_qs(requested.query)
                old_query.pop("m_type", None)
                new_query.pop("m_type", None)
                if (original.scheme, original.netloc, original.path.rsplit("/", 1)[0], old_query) != (requested.scheme, requested.netloc, requested.path.rsplit("/", 1)[0], new_query):
                    return None
                with open(os.path.join(cls.CANONICAL_DIR, kind + ".body"), "rb") as f:
                    body = f.read()
                digest = meta.get("body_sha256")
                if digest and hashlib.sha256(body).hexdigest() != digest:
                    return None
                if cls.detect_manifest_type(json.loads(body)) != kind:
                    return None
                cls._touch(os.path.join(cls.CANONICAL_DIR, kind + ".body"))
                return body, meta
            except (OSError, ValueError, TypeError):
                return None

    @classmethod
    def get_canonical_manifest(cls, url):
        exact = cls.get(url)
        if exact:
            return exact
        candidate = cls.manifest_candidate(url)
        if candidate and cls._identity(candidate[1].get("url", "")) == cls._identity(url):
            return candidate[0], candidate[1]["headers"], candidate[1].get("status", 200)
        return None

    @classmethod
    def save_canonical_manifest(cls, url, body, headers, wire_body_bytes=_AUTO_SIZE):
        try:
            kind = cls.detect_manifest_type(json.loads(body))
            if not kind:
                return
            lowered = {k.lower(): v for k, v in headers.items()}
            if "set-cookie" in lowered or "no-store" in lowered.get("cache-control", "").lower():
                return
            cls.put(url, body, headers, wire_body_bytes=wire_body_bytes)
            entry = cls._read(url)
            if not entry:
                return
            cls.learn_hash_mapping(urlsplit(url).path.rsplit("/", 1)[-1], kind)
            with cls._disk_lock:
                cls._atomic_write(os.path.join(cls.CANONICAL_DIR, kind + ".body"), body)
                cls._atomic_write(os.path.join(cls.CANONICAL_DIR, kind + ".meta"), json.dumps(entry[1]).encode())
        except (OSError, ValueError, TypeError):
            return
