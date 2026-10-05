import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from random import Random
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from publisher.image_receipts import frozen_body_images, image_content_matches


def encoded(image, *, quality=90):
    stream = io.BytesIO()
    image.save(stream, 'JPEG', quality=quality)
    return stream.getvalue()


def illustration():
    image = Image.new('RGB', (1200, 510), '#e3e9db')
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, 110, 130), fill='#244c3e')
    draw.ellipse((900, -180, 1390, 290), fill='#1d493b')
    for row in range(4):
        for column in range(18):
            x, y = 130 + column * 28, 180 + row * 32
            draw.rectangle((x, y, x + 14, y + 20), fill='#264c3c')
    draw.line((140, 365, 570, 365), fill='#4e6153', width=3)
    return image


class PixelReceiptTests(unittest.TestCase):
    def setUp(self):
        self.original = illustration()
        self.raw = encoded(self.original)

    def test_same_pixels_and_jpeg_rescale(self):
        self.assertTrue(image_content_matches(self.raw, self.raw))
        resized = self.original.resize((1080, 459), Image.Resampling.LANCZOS)
        self.assertTrue(image_content_matches(self.raw, encoded(resized, quality=75)))

    def test_complex_picture_with_moderate_jpeg_compression(self):
        random = Random(1)
        picture = Image.frombytes('RGB', (640, 480), random.randbytes(640 * 480 * 3)).filter(ImageFilter.GaussianBlur(2))
        draw = ImageDraw.Draw(picture)
        draw.ellipse((100, 100, 400, 400), fill='#693aa2')
        draw.line((20, 430, 600, 50), fill='#ffab13', width=4)
        raw = encoded(picture, quality=95)
        for quality in (85, 75):
            with self.subTest(quality=quality):
                self.assertTrue(image_content_matches(raw, encoded(picture.resize((576, 432), Image.Resampling.LANCZOS), quality=quality)))

    def test_watermark_requires_explicit_allowance(self):
        image = self.original.resize((1080, 459), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        for x in range(820, 990, 16):
            draw.rectangle((x, 419, x + 8, 429), fill='#9da797')
        public = encoded(image, quality=80)
        self.assertFalse(image_content_matches(self.raw, public))
        self.assertTrue(image_content_matches(self.raw, public, allow_watermark=True))

    def test_blank_replacement_and_other_picture_rejected(self):
        for color in ('#e3e9db', 'white', '#244c3e'):
            with self.subTest(color=color):
                self.assertFalse(image_content_matches(self.raw, encoded(Image.new('RGB', (1200, 510), color)), allow_watermark=True))
        self.assertFalse(image_content_matches(self.raw, encoded(self.original.transpose(Image.Transpose.FLIP_LEFT_RIGHT)), allow_watermark=True))

    def test_crop_rejected_even_when_aspect_is_restored(self):
        crop = self.original.crop((100, 50, 1100, 475)).resize(self.original.size)
        self.assertFalse(image_content_matches(self.raw, encoded(crop), allow_watermark=True))
        self.assertFalse(image_content_matches(self.raw, encoded(self.original.crop((0, 0, 1200, 500))), allow_watermark=True))

    def test_small_subject_edit_rejected_by_spatial_block(self):
        changed = self.original.copy()
        ImageDraw.Draw(changed).rectangle((175, 230, 190, 246), fill='white')
        self.assertFalse(image_content_matches(self.raw, encoded(changed), allow_watermark=True))

    def test_small_body_digit_change_rejected_including_block_boundaries(self):
        for size in (12, 18, 24):
            try:
                font = ImageFont.truetype('arial.ttf', size)
            except OSError:
                font = ImageFont.load_default(size=size)
            for offset in (0, 3, 6):
                first, second = self.original.copy(), self.original.copy()
                ImageDraw.Draw(first).text((140 + offset, 80), 'Count: 1', font=font, fill='black')
                ImageDraw.Draw(second).text((140 + offset, 80), 'Count: 9', font=font, fill='black')
                with self.subTest(size=size, offset=offset):
                    self.assertFalse(image_content_matches(encoded(first, quality=95), encoded(second, quality=95), allow_watermark=True))

    def test_watermark_elsewhere_and_large_roi_changes_rejected(self):
        elsewhere = self.original.copy()
        ImageDraw.Draw(elsewhere).rectangle((500, 300, 550, 320), fill='#879181')
        self.assertFalse(image_content_matches(self.raw, encoded(elsewhere), allow_watermark=True))
        changed = self.original.copy()
        ImageDraw.Draw(changed).rectangle((816, 448, 1199, 509), fill='white')
        self.assertFalse(image_content_matches(self.raw, encoded(changed), allow_watermark=True))

    def test_invalid_missing_and_thumbnail_rejected(self):
        for raw in (b'', b'not a picture', None):
            self.assertFalse(image_content_matches(self.raw, raw, allow_watermark=True))
        self.assertFalse(image_content_matches(self.raw, encoded(self.original.resize((120, 51))), allow_watermark=True))


class FrozenImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assets = Path(self.tmp.name)
        self.a, self.b = '/assets/' + 'a' * 32 + '.jpg', '/assets/' + 'b' * 32 + '.jpg'
        for ref, color in ((self.a, 'red'), (self.b, 'blue')):
            (self.assets / ref.rsplit('/', 1)[-1]).write_bytes(encoded(Image.new('RGB', (32, 24), color)))
        self.html = '<p><img src="' + self.b + '"></p><img src="' + self.a + '"><img src="' + self.b + '">'
        self.plan = {'html': self.html, 'html_sha256': hashlib.sha256(self.html.encode()).hexdigest(), 'body_image_count': 3,
                     'images': [{'src': ref, 'sha256': hashlib.sha256((self.assets / ref.rsplit('/', 1)[-1]).read_bytes()).hexdigest()} for ref in (self.a, self.b)]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_duplicates_and_path(self):
        refs = frozen_body_images({'_prepared': self.plan}, self.assets)
        self.assertEqual([ref['src'] for ref in refs], [self.b, self.a, self.b])
        self.assertTrue(all(isinstance(ref['path'], Path) and ref['path'].is_absolute() for ref in refs))

    def test_cover_only_and_no_body_images(self):
        empty = {**self.plan, 'html': '<p>文字</p>', 'body_image_count': 0}
        empty['html_sha256'] = hashlib.sha256(empty['html'].encode()).hexdigest()
        self.assertEqual(frozen_body_images({'_prepared': empty}, self.assets), [])

    def test_changed_or_missing_local_file_and_html_rejected(self):
        bad = {**self.plan, 'html': self.html + 'changed'}
        with self.assertRaises(ValueError):
            frozen_body_images({'_prepared': bad}, self.assets)
        target = self.assets / self.a.rsplit('/', 1)[-1]
        target.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            frozen_body_images({'_prepared': self.plan}, self.assets)
        target.unlink()
        with self.assertRaises(ValueError):
            frozen_body_images({'_prepared': self.plan}, self.assets)

    def test_missing_manifest_and_duplicate_manifest_rejected(self):
        for images in (self.plan['images'][:1], self.plan['images'] + [self.plan['images'][0]], None):
            with self.subTest(images=images), self.assertRaises(ValueError):
                frozen_body_images({'_prepared': {**self.plan, 'images': images}}, self.assets)

    def test_count_external_and_traversal_rejected(self):
        for markup in ('<img src="https://mmbiz.qpic.cn/a">', '<img src="/assets/../x.jpg">', '<img>'):
            bad = {**self.plan, 'html': markup, 'html_sha256': hashlib.sha256(markup.encode()).hexdigest(), 'body_image_count': 1}
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                frozen_body_images({'_prepared': bad}, self.assets)
        with self.assertRaises(ValueError):
            frozen_body_images({'_prepared': {**self.plan, 'body_image_count': 2}}, self.assets)
        with self.assertRaises(ValueError):
            frozen_body_images({}, self.assets)

    def test_packaged_app_directory_alias_uses_filesystem_identity(self):
        # Simulate Windows Store's parent AppData / child LocalCache spelling
        # mismatch. The file resolves into its real directory; directory
        # samefile is the authoritative alias test.
        virtual_root = self.assets.parent / 'virtual-appdata-alias'
        real_resolve, real_samefile = Path.resolve, Path.samefile

        def resolve(path):
            if path == self.assets:
                return virtual_root
            if path.parent == virtual_root:
                return self.assets / path.name
            return real_resolve(path)

        def samefile(path, other):
            if path == self.assets and other == virtual_root:
                return True
            return real_samefile(path, other)

        with patch.object(Path, 'resolve', side_effect=resolve, autospec=True), patch.object(Path, 'samefile', side_effect=samefile, autospec=True) as identity:
            refs = frozen_body_images({'_prepared': self.plan}, self.assets)
        self.assertEqual([ref['src'] for ref in refs], [self.b, self.a, self.b])
        self.assertEqual(identity.call_count, 2)

    def test_resolved_child_in_different_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere)
            for item in self.plan['images']:
                name = item['src'].rsplit('/', 1)[-1]
                (outside / name).write_bytes((self.assets / name).read_bytes())
            real_resolve = Path.resolve

            def resolve(path):
                if path.parent == self.assets and path.name.endswith('.jpg'):
                    return outside / path.name
                return real_resolve(path)

            with patch.object(Path, 'resolve', side_effect=resolve, autospec=True), self.assertRaises(ValueError):
                frozen_body_images({'_prepared': self.plan}, self.assets)


if __name__ == '__main__':
    unittest.main()
