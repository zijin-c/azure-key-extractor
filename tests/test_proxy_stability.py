import asyncio
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules import key_extractor as extractor
from modules.pipeline import DynamicProxyBridge, ProxyController


class ProxyBridgeRegressions(unittest.TestCase):
    def test_delayed_upstream_response_survives_15_second_idle(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        stop = threading.Event()
        failures = []

        def upstream():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    connection.recv(4096)
                    connection.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
                    if not stop.wait(16):
                        connection.sendall(b'delayed-response')
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=upstream, daemon=True)
        worker.start()
        bridge = DynamicProxyBridge(f'http://127.0.0.1:{listener.getsockname()[1]}')
        try:
            bridge.start()
            with socket.create_connection(('127.0.0.1', bridge.local_port), timeout=5) as client:
                client.settimeout(20)
                client.sendall(b'CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid\r\n\r\n')
                self.assertIn(b'200 Connection established', client.recv(4096))
                self.assertEqual(client.recv(4096), b'delayed-response')
        finally:
            stop.set()
            bridge.stop()
            listener.close()
            worker.join(timeout=5)
        self.assertEqual(failures, [])

    def test_same_proxy_preserves_existing_connections(self):
        bridge = DynamicProxyBridge('socks5://127.0.0.1:1080')
        connection = MagicMock()
        bridge._active_sockets.add(connection)
        self.assertFalse(bridge.switch_upstream('socks5://127.0.0.1:1080'))
        connection.shutdown.assert_not_called()
        connection.close.assert_not_called()
        self.assertTrue(bridge.switch_upstream('socks5://127.0.0.1:1081'))
        connection.shutdown.assert_called_once()
        connection.close.assert_called_once()

    def test_stop_wakes_blocked_connections(self):
        bridge = DynamicProxyBridge(None)
        connection = MagicMock()
        bridge._active_sockets.add(connection)
        bridge.stop()
        connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        connection.close.assert_called_once()


class ProxyControllerRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_same_proxy_is_not_reported_as_switched(self):
        bridge = DynamicProxyBridge('socks5://127.0.0.1:1080')
        rotator = SimpleNamespace(get_next_proxy=AsyncMock(return_value=('socks5://127.0.0.1:1080', 0)))
        self.assertIsNone(await ProxyController(bridge, rotator).switch_next())


class SoftwareWaitRegressions(unittest.IsolatedAsyncioTestCase):
    async def run_wait(self, readiness_delay):
        clock = [0.0]
        navigated_at = []
        page = MagicMock()
        page.url = 'https://portal.azure.com/#EducationMenuBlade/~/software'
        page.frames = []
        page.is_closed.return_value = False

        async def sleep(seconds):
            clock[0] += seconds

        async def navigate(*args, **kwargs):
            clock[0] += 12  # Slow navigation consumes time separately from rendering.
            navigated_at.append(clock[0])

        async def evaluate(script):
            if 'has_search' not in script:
                return False
            ready = bool(navigated_at and clock[0] - navigated_at[0] >= readiness_delay)
            return {'ready': ready, 'no_software': False}

        page.goto = AsyncMock(side_effect=navigate)
        page.evaluate = AsyncMock(side_effect=evaluate)
        with patch.object(extractor.config, 'PRODUCTS_TO_EXTRACT', []), \
             patch.object(extractor, 'get_cached_totp', return_value=''), \
             patch.object(extractor, '_is_mfa_login_prompt', AsyncMock(return_value=False)), \
             patch.object(extractor, '_wait_for_portal_ready', AsyncMock(return_value=(True, ''))), \
             patch.object(extractor, '_handle_terms_flow', AsyncMock()), \
             patch.object(extractor, '_ensure_software_blade_active', AsyncMock()), \
             patch.object(extractor.asyncio, 'get_running_loop', return_value=SimpleNamespace(time=lambda: clock[0])), \
             patch.object(extractor.asyncio, 'sleep', side_effect=sleep):
            try:
                await extractor.extract_all_keys(page, None, '', '')
            finally:
                self.assertEqual(page.goto.await_count, 1)
                self.assertTrue(navigated_at)

    async def test_slow_render_after_navigation_gets_full_wait(self):
        await self.run_wait(12)

    async def test_never_ready_still_times_out(self):
        with self.assertRaises(extractor.PortalLoadError):
            await self.run_wait(100)


if __name__ == '__main__':
    unittest.main()
