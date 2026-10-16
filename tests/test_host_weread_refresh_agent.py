import importlib.util
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/host_weread_refresh_agent.py'
spec = importlib.util.spec_from_file_location('host_weread_refresh_agent', SCRIPT)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class HostRefreshRegression(unittest.TestCase):
    def test_qr_prompt_creates_cooldown_and_opens_updating_page(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / 'qr_mark'
            with (patch.object(agent, 'QR_MARK', str(marker)),
                  patch.object(agent, 'QR_WAIT_S', 0),
                  patch.object(agent, '_api_token', return_value='test-token'),
                  patch.object(agent, '_http_json', return_value={'data': {'uid': 'test-uid'}}),
                  patch.object(agent, '_notify'),
                  patch.object(agent, '_push_omnifocus_scan_needed'),
                  patch('subprocess.run') as run):
                agent._qr_login_flow()
                self.assertTrue(marker.exists())
                self.assertFalse(agent._qr_recently_prompted())
                self.assertEqual(run.call_args.args[0], ['open', f'http://127.0.0.1:{agent.PORT}/login'])

    def test_login_written_during_notification_is_detected(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            lic = Path(td) / 'lic'
            lic.write_text('synthetic')
            prior = lic.stat().st_mtime
            def login_during_notification(*args):
                os.utime(lic, (prior + 1, prior + 1))
            with (patch.object(agent, 'LIC_PATH', str(lic)),
                  patch.object(agent, 'QR_MARK', str(Path(td) / 'qr')),
                  patch.object(agent, '_api_token', return_value='test-token'),
                  patch.object(agent, '_http_json', return_value={'data': {'uid': 'test-uid'}}),
                  patch.object(agent, '_notify', side_effect=login_during_notification),
                  patch.object(agent, '_push_omnifocus_scan_needed'),
                  patch.object(agent, '_load_weread_data', return_value=({}, {'cookie': 'synthetic'})),
                  patch.object(agent, '_verify_cookie', return_value=True),
                  patch.object(agent, '_clear_alert_mark') as clear,
                  patch('subprocess.run'), patch.object(agent.time, 'sleep')):
                agent._qr_login_flow()
                clear.assert_called_once()

    def test_existing_qr_worker_is_not_duplicated(self):
        with (patch.object(agent, '_load_weread_data', return_value=({}, {})),
              patch.object(agent, 'refresh_weread_cookie', return_value=False),
              patch.object(agent, '_qr_thread', Mock(is_alive=Mock(return_value=True))),
              patch.object(agent.threading, 'Thread') as thread):
            self.assertTrue(agent.do_refresh()['needs_scan'])
            thread.assert_not_called()

    def test_login_page_refreshes_qr_without_credentials(self):
        server = agent.ThreadingHTTPServer(('127.0.0.1', 0), agent._Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/login') as response:
                body = response.read().decode()
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn('setInterval(refresh,5000)', body)
                self.assertNotIn('test-token', body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
