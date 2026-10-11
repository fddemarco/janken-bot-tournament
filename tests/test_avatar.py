import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import avatar

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:                                      # pragma: no cover
    HAVE_PIL = False

needs_pil = unittest.skipUnless(
    HAVE_PIL, "Pillow is the dashboard's dependency, not the engine's")


def make_image(size=(300, 200), colour=(255, 0, 0), fmt="PNG"):
    """An image in memory, so the tests carry no binary fixtures."""
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format=fmt)
    return out.getvalue()


@needs_pil
class NormalizeTest(unittest.TestCase):
    def test_returns_a_square_png_at_the_declared_size(self):
        png = avatar.normalize(make_image((300, 200)))
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.format, "PNG")
            self.assertEqual(img.size, (avatar.SIZE, avatar.SIZE))

    def test_upscales_a_small_image_rather_than_leaving_it_small(self):
        # One size on every surface is the point; a 16px logo that stayed 16px
        # would render as a smudge next to everyone else's.
        png = avatar.normalize(make_image((16, 16)))
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.size, (avatar.SIZE, avatar.SIZE))

    def test_crops_to_the_centre_instead_of_squashing(self):
        # A wide image with a distinct centre stripe: after a centre crop the
        # middle colour must still be the middle colour, which squashing would
        # also preserve — so check an edge too, which cropping must discard.
        source = Image.new("RGB", (300, 100), (255, 0, 0))
        for x in range(100, 200):
            for y in range(100):
                source.putpixel((x, y), (0, 0, 255))
        raw = io.BytesIO()
        source.save(raw, format="PNG")

        png = avatar.normalize(raw.getvalue())
        with Image.open(io.BytesIO(png)) as img:
            rgb = img.convert("RGB")
            self.assertEqual(rgb.getpixel((avatar.SIZE // 2, avatar.SIZE // 2)),
                             (0, 0, 255))
            # The red flanks fell outside the centre square, so they are gone.
            # Tested as "blue wins" rather than exact blue: LANCZOS samples a
            # little past the crop box, so the pixel hard against the old seam
            # picks up a few counts of red. A squash would leave it saturated.
            red, _green, blue = rgb.getpixel((2, avatar.SIZE // 2))
            self.assertGreater(blue, 200)
            self.assertLess(red, 60)

    def test_accepts_jpeg_and_re_encodes_it_as_png(self):
        png = avatar.normalize(make_image(fmt="JPEG"))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_keeps_transparency_rather_than_flattening_it(self):
        # The tables are white and the broadcast is navy, so baking either
        # background into the file would be wrong on the other surface.
        source = io.BytesIO()
        Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(source, format="PNG")
        png = avatar.normalize(source.getvalue())
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.getpixel((32, 32))[3], 0)

    def test_drops_whatever_was_bolted_onto_the_file(self):
        # The re-encode is the sanitiser: bytes appended past the PNG end
        # marker survive in the upload and must not survive in what we store.
        payload = b"<script>alert(1)</script>" * 40
        png = avatar.normalize(make_image() + payload)
        self.assertNotIn(b"<script>", png)

    def test_fit_mode_keeps_the_whole_image_and_pads_the_rest(self):
        # A wide logo: cropping would cut the ends off, so fit shrinks it in
        # and leaves transparent bands above and below.
        png = avatar.normalize(make_image((800, 200), (0, 69, 251)),
                               avatar.FIT_CONTAIN)
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.size, (avatar.SIZE, avatar.SIZE))
            # Full width at the middle row...
            self.assertEqual(img.getpixel((2, avatar.SIZE // 2))[:3], (0, 69, 251))
            self.assertEqual(img.getpixel((avatar.SIZE - 3, avatar.SIZE // 2))[:3],
                             (0, 69, 251))
            # ...and nothing but transparency top and bottom.
            self.assertEqual(img.getpixel((avatar.SIZE // 2, 1))[3], 0)
            self.assertEqual(img.getpixel((avatar.SIZE // 2, avatar.SIZE - 2))[3], 0)

    def test_fit_mode_scales_a_small_image_up_to_fill_the_square(self):
        # ImageOps.contain only shrinks, so without the pre-upscale a 40px logo
        # would sit as a speck in the middle of the canvas.
        png = avatar.normalize(make_image((40, 20)), avatar.FIT_CONTAIN)
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.size, (avatar.SIZE, avatar.SIZE))
            opaque = [x for x in range(avatar.SIZE)
                      if img.getpixel((x, avatar.SIZE // 2))[3] > 0]
            self.assertGreater(len(opaque), avatar.SIZE * 0.9,
                               "the image did not grow to span the square")

    def test_the_two_modes_agree_on_an_already_square_image(self):
        square = make_image((300, 300))
        self.assertEqual(avatar.normalize(square, avatar.FIT_CROP),
                         avatar.normalize(square, avatar.FIT_CONTAIN))

    def test_crop_is_what_you_get_by_default(self):
        wide = make_image((800, 200))
        self.assertEqual(avatar.normalize(wide),
                         avatar.normalize(wide, avatar.FIT_CROP))

    def test_rejects_an_unknown_fit_mode(self):
        with self.assertRaises(avatar.BadImage):
            avatar.normalize(make_image(), "squash")

    def test_rejects_bytes_that_are_not_an_image(self):
        with self.assertRaises(avatar.BadImage):
            avatar.normalize(b"def play(m, o): return 'rock'")

    def test_rejects_an_empty_upload(self):
        with self.assertRaises(avatar.BadImage):
            avatar.normalize(b"")

    def test_rejects_an_oversized_upload_without_decoding_it(self):
        with self.assertRaises(avatar.BadImage) as caught:
            avatar.normalize(b"\x89PNG" + b"\0" * avatar.MAX_UPLOAD_BYTES)
        self.assertIn("limit", str(caught.exception))

    def test_leaves_pillows_bomb_guard_as_it_found_it(self):
        before = Image.MAX_IMAGE_PIXELS
        avatar.normalize(make_image())
        self.assertEqual(Image.MAX_IMAGE_PIXELS, before)
        with self.assertRaises(avatar.BadImage):
            avatar.normalize(b"not an image")
        self.assertEqual(Image.MAX_IMAGE_PIXELS, before)


class DataUriTest(unittest.TestCase):
    def test_encodes_png_bytes(self):
        self.assertTrue(avatar.data_uri(b"\x89PNG\r\n").startswith(
            "data:image/png;base64,"))

    def test_no_avatar_is_the_empty_string_not_a_broken_uri(self):
        self.assertEqual(avatar.data_uri(None), "")
        self.assertEqual(avatar.data_uri(b""), "")


class RelPathTest(unittest.TestCase):
    def test_is_relative_so_the_renderer_owns_the_prefix(self):
        path = avatar.rel_path("ab12cd34ef56")
        self.assertEqual(path, "avatars/ab12cd34ef56.png")
        self.assertFalse(path.startswith("/"))


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def out(self, *parts):
        return os.path.join(self.dir.name, avatar.DIR_NAME, *parts)

    def test_writes_one_png_per_bot_and_counts_them(self):
        written = avatar.export({"aaa": b"one", "bbb": b"two"}, self.dir.name)
        self.assertEqual(written, 2)
        with open(self.out("aaa.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"one")

    def test_overwrites_rather_than_trusting_what_is_on_disk(self):
        avatar.export({"aaa": b"old"}, self.dir.name)
        avatar.export({"aaa": b"new"}, self.dir.name)
        with open(self.out("aaa.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"new")

    def test_leaves_no_half_written_file_behind(self):
        avatar.export({"aaa": b"one"}, self.dir.name)
        self.assertEqual(sorted(os.listdir(self.out())), ["aaa.png"])

    def test_nothing_to_export_creates_nothing(self):
        self.assertEqual(avatar.export({}, self.dir.name), 0)
        self.assertFalse(os.path.exists(self.out()))

    def test_an_unwritable_static_dir_costs_the_pictures_not_the_show(self):
        blocked = os.path.join(self.dir.name, "file-not-a-dir")
        with open(blocked, "w") as fh:
            fh.write("")
        self.assertEqual(avatar.export({"aaa": b"one"}, blocked), 0)


if __name__ == "__main__":
    unittest.main()
