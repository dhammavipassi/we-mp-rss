"""隔离刷新回归：不访问网络、浏览器或生产配置。"""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('cookie_refresh_under_test', Path(__file__).with_name('weread_cookie_refresh.py'))
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class CookieRefreshRegression(unittest.TestCase):
    def test_multi_account_renewal_preserves_other_accounts(self):
        import tempfile
        import yaml
        with tempfile.TemporaryDirectory() as folder:
            lic = str(Path(folder) / 'wx.lic')
            original = {'other_setting': True, 'weread_data': {
                'cookie_refresh_url': 'https://example.invalid',
                'accounts': [
                    {'vid': '1', 'cookie': 'wr_vid=1; wr_skey=old', 'name': 'primary'},
                    {'vid': '2', 'cookie': 'wr_vid=2; wr_skey=other', 'name': 'secondary'},
                ]}}
            Path(lic).write_text(yaml.safe_dump(original))
            _, data = refresh._load_weread_data(lic)
            self.assertEqual(data['cookie'], 'wr_vid=1; wr_skey=old')
            refresh._save_cookie('wr_vid=1; wr_skey=renewed', lic_path=lic)
            saved = yaml.safe_load(Path(lic).read_text())
            self.assertEqual(saved['weread_data']['accounts'][0]['cookie'], 'wr_vid=1; wr_skey=renewed')
            self.assertEqual(saved['weread_data']['accounts'][1], original['weread_data']['accounts'][1])
            self.assertNotIn('cookie', saved['weread_data'])
            self.assertTrue(saved['other_setting'])

    def test_actual_requests_use_full_headers(self):
        page, context = MagicMock(), MagicMock()
        request = MagicMock(url='https://weread.qq.com/web/mp/content?reviewId=test')
        request.all_headers.return_value = {'cookie': 'synthetic'}
        page.on.side_effect = lambda event, callback: callback(request)
        self.assertEqual(refresh._extract_cookie_from_page(page, context, 'https://example.invalid'), 'synthetic')
        context.cookies.assert_not_called()

    def test_verify_requires_html_and_uses_browser_proxy(self):
        requests = MagicMock()
        cover = MagicMock()
        cover.json.return_value = {'reviewId': 'synthetic'}
        content = MagicMock(status_code=200, text='<article id="js_content">synthetic</article>')
        content.json.side_effect = ValueError()
        requests.get.side_effect = [cover, content]
        with patch.dict(sys.modules, {'requests': requests}), patch.dict(os.environ, {'WEREAD_BROWSER_PROXY': 'http://proxy.invalid:1082'}), patch.object(refresh, '_verify_probe_book_ids', return_value=['synthetic']):
            self.assertTrue(refresh._verify_cookie('synthetic'))
        for call in requests.get.call_args_list:
            self.assertEqual(call.kwargs['proxies'], {'http': 'http://proxy.invalid:1082', 'https': 'http://proxy.invalid:1082'})

    def test_verify_accepts_new_page_forms_without_js_content_id(self):
        """新版页面（图集/短文/客户端渲染壳）没有 id="js_content"，仍应判定有效。"""
        for text in (
            "<script>var dom = document.getElementById('js_content_container')</script>",
            "<script>picture_page_info_list: [1,2,3]</script>",
            "<script>text_page_info: {content: 'synthetic'}</script>",
        ):
            with self.subTest(text=text[:30]):
                requests = MagicMock()
                cover = MagicMock()
                cover.json.return_value = {'reviewId': 'synthetic'}
                content = MagicMock(status_code=200, text=text)
                content.json.side_effect = ValueError()
                requests.get.side_effect = [cover, content]
                with patch.dict(sys.modules, {'requests': requests}), patch.object(refresh, '_verify_probe_book_ids', return_value=['synthetic']):
                    self.assertTrue(refresh._verify_cookie('synthetic'))

    def test_verify_rejects_error_json_missing_content_and_network_failure(self):
        cover = MagicMock()
        cover.json.return_value = {'reviewId': 'synthetic'}
        error = MagicMock(status_code=200, text='{"errCode":-2012}')
        error.json.return_value = {'errCode': -2012}
        for responses in ([cover, error], [MagicMock(json=lambda: {'name': 'public'})], [OSError('offline')]):
            with self.subTest(responses=len(responses)):
                requests = MagicMock()
                requests.get.side_effect = responses
                with patch.dict(sys.modules, {'requests': requests}), patch.object(refresh, '_verify_probe_book_ids', return_value=['synthetic']):
                    self.assertFalse(refresh._verify_cookie('synthetic'))

    def test_profile_precedes_seed_and_system_chrome_is_selected(self):
        context, page, chromium = MagicMock(), MagicMock(), MagicMock()
        context.new_page.return_value = page
        chromium.launch_persistent_context.return_value = context
        playwright = MagicMock()
        playwright.__enter__.return_value = SimpleNamespace(chromium=chromium)
        api = SimpleNamespace(sync_playwright=lambda: playwright)
        config = {'cookie_refresh_url': 'https://example.invalid', 'cookie': 'wr_vid=synthetic'}
        with patch.dict(sys.modules, {'playwright.sync_api': api}), patch.object(refresh, '_load_weread_data', return_value=({}, config)), patch.object(refresh.os, 'makedirs'), patch.object(refresh, '_extract_cookie_from_page', return_value='wr_vid=synthetic'), patch.object(refresh, '_verify_cookie', return_value=True), patch.object(refresh, '_save_cookie') as save:
            self.assertTrue(refresh.refresh_weread_cookie(verbose=False, cooldown_hours=0))
        context.add_cookies.assert_not_called()
        self.assertEqual(chromium.launch_persistent_context.call_args.kwargs['channel'], 'chrome')
        save.assert_called_once()

    def test_failed_validation_never_saves_and_explicit_bundled_is_preserved(self):
        context, chromium = MagicMock(), MagicMock()
        chromium.launch_persistent_context.return_value = context
        playwright = MagicMock()
        playwright.__enter__.return_value = SimpleNamespace(chromium=chromium)
        api = SimpleNamespace(sync_playwright=lambda: playwright)
        config = {'cookie_refresh_url': 'https://example.invalid', 'cookie': 'wr_vid=synthetic'}
        with patch.dict(sys.modules, {'playwright.sync_api': api}), patch.object(refresh, '_load_weread_data', return_value=({}, config)), patch.object(refresh.os, 'makedirs'), patch.object(refresh, '_extract_cookie_from_page', return_value='wr_vid=synthetic'), patch.object(refresh, '_verify_cookie', return_value=False), patch.object(refresh, '_save_cookie') as save:
            self.assertFalse(refresh.refresh_weread_cookie(verbose=False, headless_only=True, force_bundled=True, cooldown_hours=0))
        save.assert_not_called()
        self.assertNotIn('channel', chromium.launch_persistent_context.call_args.kwargs)
        self.assertEqual(chromium.launch_persistent_context.call_count, 2)
        context.add_cookies.assert_called_once()


if __name__ == '__main__':
    unittest.main()
