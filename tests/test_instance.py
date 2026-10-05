import unittest
import urllib.error
from unittest.mock import Mock, patch

from publisher.instance import handle_existing


class InstanceTests(unittest.TestCase):
    def test_same_version_reuses_without_shutting_down(self):
        with patch('publisher.instance.get_json',return_value={'app':'mojian-publisher','version':'1.0.3'}) as get:
            open_page=Mock();confirm=Mock()
            self.assertTrue(handle_existing('http://127.0.0.1:9999','1.0.3',confirm=confirm,open_page=open_page))
            open_page.assert_called_once();confirm.assert_not_called();self.assertEqual(get.call_count,1)

    def test_same_version_brings_existing_desktop_window_forward(self):
        with patch('publisher.instance.get_json', side_effect=[
                {'app':'mojian-publisher','version':'1.0.3'},
                {'token':'local-test-token'}, {'ok':True}]) as get:
            open_page=Mock(); show=Mock(return_value=True)
            self.assertTrue(handle_existing('http://127.0.0.1:9999','1.0.3',
                show_existing=show, open_page=open_page))
            show.assert_called_once_with('http://127.0.0.1:9999')
            open_page.assert_not_called()

    def test_older_version_is_stopped_before_new_launch(self):
        with patch('publisher.instance.get_json',side_effect=[{'app':'mojian-publisher','version':'1.0.1'},{'token':'local-test-token'},{'ok':True},urllib.error.URLError('stopped')]) as get:
            confirm=Mock(return_value=True);open_page=Mock()
            self.assertFalse(handle_existing('http://127.0.0.1:9999','1.0.3',confirm=confirm,open_page=open_page))
            confirm.assert_called_once();open_page.assert_not_called()
            self.assertTrue(get.call_args_list[2].args[0].endswith('/api/system/exit'))
            self.assertEqual(get.call_args_list[2].kwargs['method'],'POST')
            self.assertEqual(get.call_args_list[2].kwargs['data'],b'{"confirmed":true}')
            self.assertEqual(get.call_args_list[2].kwargs['headers']['Content-Type'],'application/json')

    def test_declining_upgrade_keeps_old_service_and_does_not_open_it_silently(self):
        with patch('publisher.instance.get_json',return_value={'app':'mojian-publisher','version':'1.0.1'}) as get:
            open_page=Mock()
            self.assertTrue(handle_existing('http://127.0.0.1:9999','1.0.3',confirm=lambda _:False,open_page=open_page))
            self.assertEqual(get.call_count,1);open_page.assert_not_called()

    def test_failed_shutdown_prevents_new_launch(self):
        with patch('publisher.instance.get_json',side_effect=[{'app':'mojian-publisher','version':'1.0.1'},urllib.error.URLError('unavailable')]):
            error=Mock()
            self.assertTrue(handle_existing('http://127.0.0.1:9999','1.0.3',confirm=lambda _:True,show_error=error))
            error.assert_called_once()

    def test_headless_check_cannot_stop_user_instance(self):
        with patch('publisher.instance.get_json',return_value={'app':'mojian-publisher','version':'1.0.1'}) as get:
            confirm=Mock()
            self.assertTrue(handle_existing('http://127.0.0.1:9999','1.0.3',headless=True,confirm=confirm))
            confirm.assert_not_called();self.assertEqual(get.call_count,1)
