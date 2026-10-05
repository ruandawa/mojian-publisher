"""Public receipt evidence must include decoded, lazy-loaded article images."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from playwright.async_api import async_playwright

from publisher.browser import BrowserDriver
from publisher.receipts import body_hash, text_key
from publisher.store import Store


PUBLIC = 'https://mp.weixin.qq.com/s/abcdefghijklmnopqrstuv'


class PublicImageLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.driver = BrowserDriver(Store(Path(self.temp.name)))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='msedge', headless=True)
        self.contexts = []
        self.article_html = ''
        self.requests = []
        self.broken = False
        self.image_dimensions = (100, 80)
        self.redirect_url = ''
        self.fast_visibility = False

        async def new_context(**options):
            context = await self.browser.new_context(**options)
            self.contexts.append(context)

            async def route_request(route):
                url = route.request.url
                if url == PUBLIC and self.redirect_url:
                    await route.fulfill(status=302, headers={'location': self.redirect_url}, body='')
                elif url == PUBLIC or url == self.redirect_url:
                    await route.fulfill(body=self.article_html, content_type='text/html; charset=utf-8')
                elif url.startswith('https://fixture.test/image'):
                    self.requests.append(url)
                    if self.broken:
                        await route.fulfill(status=404, body='missing')
                    else:
                        await route.fulfill(content_type='image/svg+xml', body=(
                            '<svg xmlns="http://www.w3.org/2000/svg" '
                            f'width="{self.image_dimensions[0]}" height="{self.image_dimensions[1]}">'
                            '<rect width="100" height="80" fill="green"/></svg>'))
                else:
                    await route.abort()

            await context.route('**/*', route_request)
            page = await context.new_page()
            self.page = page
            self.wrapped_page = Mock(wraps=page)
            self.wrapped_page.url = page.url

            async def goto(*args, **options):
                result = await page.goto(*args, **options)
                self.wrapped_page.url = page.url
                return result

            self.wrapped_page.goto = AsyncMock(side_effect=goto)

            def locator(*args, **options):
                real_locator = page.locator(*args, **options)
                if not self.fast_visibility:
                    return real_locator
                wrapped_locator = Mock(wraps=real_locator)

                async def wait_for(**wait_options):
                    wait_options['timeout'] = min(wait_options.get('timeout', 500), 500)
                    await real_locator.wait_for(**wait_options)

                wrapped_locator.wait_for = AsyncMock(side_effect=wait_for)
                return wrapped_locator

            self.wrapped_page.locator = Mock(side_effect=locator)

            async def screenshot(**options):
                self.capture_scroll = await page.evaluate('window.scrollY')
                return await page.screenshot(**options)

            self.wrapped_page.screenshot = AsyncMock(side_effect=screenshot)
            wrapped_context = Mock(wraps=context)
            wrapped_context.new_page = AsyncMock(return_value=self.wrapped_page)
            return wrapped_context

        self.driver.context = Mock(browser=Mock(new_context=AsyncMock(side_effect=new_context)))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    def article(self, body):
        self.article_html = '<h1 id="activity-name">图片验证</h1><div id="js_content">'+body+'</div>'+'''
          <script>
          window.imageLoads=0;
          const observer=new IntersectionObserver(items=>{
            for(const item of items) if(item.isIntersecting){
              const image=item.target;
              image.src=image.dataset.src;
              image.onload=()=>window.imageLoads++;
              observer.unobserve(image);
            }
          });
          document.querySelectorAll('img[data-src]').forEach(image=>observer.observe(image));
          </script>'''
        return {'job_id': 'a'*32, 'title': text_key('图片验证'),
                'body_sha256': body_hash(body), 'images': self.driver._receipt_image_keys(body)}

    async def test_public_evidence_loads_images_below_fold_before_screenshot(self):
        receipt = self.article('<p>完整正文</p>'+''.join(
            '<div style="height:1100px"></div>'
            f'<img data-src="https://fixture.test/image{index}.svg" '
            'src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%221%22 height=%221%22/%3E" '
            'width="100" height="80">'
            for index in range(3)))
        self.assertTrue(await self.driver._read_public_article(PUBLIC, receipt))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.capture_scroll, 0)
        self.wrapped_page.screenshot.assert_awaited_once()
        self.assertTrue((self.driver.store.root/'evidence'/(receipt['job_id']+'-public-mobile.png')).exists())
        self.assertEqual(self.driver.context.browser.new_context.await_args.kwargs,
                         {'viewport': {'width': 390, 'height': 844}})
        self.assertTrue(self.page.is_closed())

    async def test_genuine_single_pixel_network_image_remains_supported(self):
        self.image_dimensions = (1, 1)
        receipt = self.article('<p>完整正文</p><img data-src="https://fixture.test/image0.svg" width="100" height="80">')
        self.assertTrue(await self.driver._read_public_article(PUBLIC, receipt))
        self.wrapped_page.screenshot.assert_awaited_once()

    async def test_matching_image_url_with_failed_download_does_not_confirm_publication(self):
        self.broken = True
        receipt = self.article('<p>完整正文</p><img data-src="https://fixture.test/image0.svg" width="100" height="80">')
        self.assertFalse(await self.driver._read_public_article(PUBLIC, receipt))
        self.wrapped_page.screenshot.assert_not_awaited()
        self.assertTrue(self.page.is_closed())

    async def test_decoded_svg_placeholder_is_not_the_loaded_article_image(self):
        context = await self.browser.new_context(viewport={'width': 390, 'height': 844})
        page = await context.new_page()
        await page.set_content('''<div id="js_content"><img data-src="https://fixture.test/image0.svg"
          src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%221%22 height=%221%22/%3E"></div>''')
        await page.wait_for_function('document.querySelector("img").complete')
        self.assertEqual(await page.locator('img').evaluate('e=>e.naturalWidth'), 1)
        self.assertFalse(await self.driver._load_public_images(page, timeout_seconds=.25))
        await context.close()

    async def test_url_less_editor_decoration_is_not_a_broken_article_image(self):
        receipt = self.article('<p>完整正文</p><img class="ProseMirror-separator">')
        self.assertTrue(await self.driver._read_public_article(PUBLIC, receipt))
        self.wrapped_page.screenshot.assert_awaited_once()
        self.assertEqual(self.requests, [])

    async def test_unmatched_content_never_reaches_image_loading_or_screenshot(self):
        receipt = self.article('<p>完整正文</p>')
        receipt['body_sha256'] = body_hash('<p>另一篇正文</p>')
        self.driver._load_public_images = AsyncMock(return_value=True)
        self.assertFalse(await self.driver._read_public_article(PUBLIC, receipt))
        self.driver._load_public_images.assert_not_awaited()
        self.wrapped_page.screenshot.assert_not_awaited()

    async def test_image_wait_has_global_deadline_and_restores_top(self):
        context = await self.browser.new_context(viewport={'width': 390, 'height': 844})
        page = await context.new_page()
        await page.set_content('<div id="js_content"><div style="height:1800px"></div><img src="data:bad"></div>')
        await page.evaluate('window.scrollTo(0,1400)')
        self.assertFalse(await self.driver._load_public_images(page, timeout_seconds=0))
        self.assertEqual(await page.evaluate('window.scrollY'), 0)
        await context.close()

    async def test_hidden_body_is_not_readable_publication(self):
        receipt = self.article('<p>完整正文</p>')
        self.article_html = self.article_html.replace('id="js_content"', 'id="js_content" style="display:none"')
        self.fast_visibility = True
        self.assertFalse(await self.driver._read_public_article(PUBLIC, receipt))
        self.wrapped_page.screenshot.assert_not_awaited()
        self.assertTrue(self.page.is_closed())

    async def test_hidden_title_is_not_readable_publication(self):
        receipt = self.article('<p>完整正文</p>')
        self.article_html = self.article_html.replace('id="activity-name"', 'id="activity-name" style="display:none"')
        self.fast_visibility = True
        self.assertFalse(await self.driver._read_public_article(PUBLIC, receipt))
        self.wrapped_page.screenshot.assert_not_awaited()
        self.assertTrue(self.page.is_closed())

    async def test_public_link_redirected_to_preview_is_not_publication(self):
        receipt = self.article('<p>完整正文</p>')
        self.redirect_url = 'https://mp.weixin.qq.com/s?__biz=fixture&mid=200&idx=1&sn=fixture&tempkey=preview'
        self.assertFalse(await self.driver._read_public_article(PUBLIC, receipt))
        self.assertEqual(self.wrapped_page.url, self.redirect_url)
        self.wrapped_page.screenshot.assert_not_awaited()

    async def test_public_link_redirected_to_login_is_not_publication(self):
        receipt = self.article('<p>完整正文</p>')
        self.redirect_url = 'https://mp.weixin.qq.com/cgi-bin/loginpage'
        self.assertFalse(await self.driver._read_public_article(PUBLIC, receipt))
        self.assertEqual(self.wrapped_page.url, self.redirect_url)
        self.wrapped_page.screenshot.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
