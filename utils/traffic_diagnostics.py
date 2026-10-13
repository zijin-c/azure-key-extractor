"""被动流量诊断：只读浏览器尺寸，不下载响应正文或记录认证资料。"""
import asyncio
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit


def safe_traffic_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if parsed.port:
            host += f':{parsed.port}'
        path = '/'.join('[redacted]' if '@' in part or len(part) > 100 else part
                        for part in parsed.path.split('/'))
        return urlunsplit((parsed.scheme, host, path, '', '')).replace('\n', '').replace('\r', '')
    except (ValueError, TypeError):
        return '[invalid URL]'


class HttpSizeDiagnostics:
    def __init__(self, stats):
        self.stats = stats
        self.pending = {}
        self.synthetic = {}
        self.tasks = set()
        self.measured_count = 0
        self.recovered_count = 0
        self.measurement_errors = 0
        self.failures = []
        self.fetch_failures = []
        self.unresolved = []
        self.attached = False

    def recorded(self, request, index, unknown, native):
        # request 引用避免尚未完成时 Python id 被复用；完成后释放。
        self.pending[id(request)] = (request, index, unknown, native)

    def mark_synthetic(self, request):
        self.synthetic[id(request)] = request

    def fetch_failed(self, request, error):
        self.fetch_failures.append((safe_traffic_url(request.url), type(error).__name__))

    def attach(self, ctx):
        if self.attached or not callable(getattr(ctx, 'on', None)):
            return
        self.attached = True
        ctx.on('requestfinished', self._finished)
        ctx.on('requestfailed', self._failed)

    def _finished(self, request):
        entry = self.pending.pop(id(request), None)
        synthetic = self.synthetic.pop(id(request), None)
        if synthetic is not None or entry is None:
            # route.fetch 的真实响应已单独计数，不能再计浏览器的合成响应。
            if entry and entry[2]:
                self.unresolved.append(safe_traffic_url(request.url))
            return
        task = asyncio.create_task(self._measure(request, entry))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _failed(self, request):
        entry = self.pending.pop(id(request), None)
        if self.synthetic.pop(id(request), None) is not None:
            return  # 本地媒体拦截不当成网络故障。
        self.failures.append((safe_traffic_url(request.url),
                              (request.failure or 'unknown').splitlines()[0][:100]))
        if entry and entry[2]:
            self.unresolved.append(safe_traffic_url(request.url))

    async def _measure(self, request, entry):
        _, index, unknown, native = entry
        if not native:
            return
        try:
            sizes = await request.sizes()
            fields = ('requestBodySize', 'requestHeadersSize', 'responseBodySize', 'responseHeadersSize')
            if any(not isinstance(sizes.get(key), int) or sizes[key] < 0 for key in fields):
                raise ValueError('Unavailable request sizes')
            # 浏览器内部缓存可能返回 responseBodySize=0；它不是遗漏的下载。
            old = self.stats.network_requests[index]
            total = sum(sizes[key] for key in fields)
            self.stats.network_requests[index] = (total, *old[1:])
            self.stats.net_transfer_bytes += total - old[0]
            self.measured_count += 1
            if unknown:
                self.stats.transfer_size_unknown_responses -= 1
                self.recovered_count += 1
        except Exception:
            self.measurement_errors += 1
            if unknown:
                self.unresolved.append(safe_traffic_url(request.url))

    async def drain(self, timeout=2):
        # 仅等待读取浏览器计数，不发送请求；保护取消，不阻断账号退出。
        if self.tasks:
            await asyncio.wait(tuple(self.tasks), timeout=timeout)

    def report_lines(self):
        stats = self.stats
        rows = sorted(stats.network_requests, reverse=True, key=lambda row: row[0])
        top = sum(row[0] for row in rows[:5])
        yield (f'  🧮 [流量诊断] HTTP 合计 {stats.net_transfer_bytes / 1048576:.2f} MB / {len(rows)} 个响应；'
               f'Top 5 {top / 1048576:.2f} MB；Top 5 之外 {(stats.net_transfer_bytes - top) / 1048576:.2f} MB')
        yield (f'  📏 [流量诊断] 浏览器尺寸实测 {self.measured_count} 个，补齐无长度响应 {self.recovered_count} 个；'
               f'仍未知 {stats.transfer_size_unknown_responses} 个，读取失败 {self.measurement_errors} 个，'
               f'尚未完成 {len(self.pending) + len(self.tasks)} 个')
        domains = defaultdict(lambda: [0, 0])
        paths = defaultdict(lambda: [0, 0])
        for size, url, kind, status in rows:
            host = urlsplit(url).hostname or '[unknown]'
            domains[host][0] += size
            domains[host][1] += 1
            paths[url][0] += size
            paths[url][1] += 1
        yield '  🌍 [流量诊断] HTTP 域名 Top 10（请求及响应大小，不含 TLS）：'
        for host, (size, count) in sorted(domains.items(), key=lambda item: item[1][0], reverse=True)[:10]:
            yield f'    • {host}: {size / 1048576:.2f} MB / {count} 个响应'
        yield '  🔎 [流量诊断] 单次请求 Top 20（URL 已去除查询参数）：'
        for size, url, kind, status in rows[:20]:
            yield f'    • {size / 1024:.1f} KB [{kind}] HTTP {status} {url[:180]}'
        repeated = [(url, value) for url, value in paths.items() if value[1] > 1]
        if repeated:
            yield '  🔁 [流量诊断] 重复路径 Top 5（合并查询参数；次数不等于重试次数）：'
            for url, (size, count) in sorted(repeated, key=lambda item: item[1][0], reverse=True)[:5]:
                yield f'    • {size / 1048576:.2f} MB / {count} 次 {url[:180]}'
        yield (f'  ⚠️ [流量诊断] 原生请求失败 {len(self.failures)} 次；'
               f'缓存 fetch 失败后回退原生 {len(self.fetch_failures)} 次（失败的部分下载未准确计入 HTTP）')
        for url, reason in (self.failures + self.fetch_failures)[:10]:
            yield f'    • {reason}: {url[:180]}'
        unresolved = list(dict.fromkeys(self.unresolved + [
            safe_traffic_url(entry[0].url) for entry in self.pending.values() if entry[2]]))
        if unresolved:
            yield '  ❓ [流量诊断] 尚不能确定响应体传输量的 URL（最多 10 条）：'
            for url in unresolved[:10]:
                yield f'    • {url[:180]}'


def proxy_report_lines(bridge, http_bytes, warmup_bytes=0, startup_bytes=0):
    if not bridge or not hasattr(bridge, 'get_domain_stats'):
        return
    snapshot = bridge.get_domain_stats()
    total = sum(row['upload_bytes'] + row['download_bytes'] for row in snapshot)
    delta = total - http_bytes
    yield (f'  🔌 [流量诊断] 代理连接 {total / 1048576:.2f} MB；与 HTTP 统计差额 {delta / 1048576:+.2f} MB'
           '（不是纯协议开销，也可能含失败下载、后台连接和未测量响应）')
    yield (f'  📦 [流量诊断] 上述代理总量包含：公共缓存预热 {warmup_bytes / 1048576:.2f} MB；'
           f'浏览器启动/出口探测阶段 {startup_bytes / 1048576:.2f} MB（按时间段计量）')
    yield '  🔗 [流量诊断] 代理目标域名 Top 20（隧道字节；不解密 HTTPS）：'
    ranked = sorted(snapshot, key=lambda row: row['upload_bytes'] + row['download_bytes'], reverse=True)
    for row in ranked[:20]:
        yield (f"    • {row['host']}: 下行 {row['download_bytes'] / 1048576:.2f} MB / "
               f"上行 {row['upload_bytes'] / 1048576:.2f} MB / "
               f"连接 {row['connections']} 次 / 建连失败 {row['failed_connections']} 次")
    if len(ranked) > 20:
        remainder = sum(row['upload_bytes'] + row['download_bytes'] for row in ranked[20:])
        yield f'    • 其余 {len(ranked) - 20} 个域名合计 {remainder / 1048576:.2f} MB'
