"""Bot avatars: normalising an upload, and getting it in front of a browser.

An avatar is decoration. Nothing here touches identity, eligibility or rating —
a bot with a picture and a bot without one play exactly the same tournament —
so every function in this module is allowed to fail soft and leave the bot
alone. That is deliberate: an unreadable PNG must cost a submitter their
picture, never their entry.

Two things need saying about the shape of this file.

**Uploads are re-encoded, never stored as sent.** `normalize` decodes the bytes,
crops them square and writes a fresh PNG. Rejecting bad input is the smaller
half of why: the file we serve is one Pillow *wrote*, so whatever a `.png` had
bolted onto it — EXIF, a trailing ZIP, a comment block with a script in it — is
gone, because none of it survives a decode/encode round trip. It also means one
predictable size on every surface instead of somebody's 4000px phone photo.

**The engine must not need Pillow.** The bots and the tournament are
stdlib-only, and `janken cup run` imports this module to export avatars for the
broadcast. So Pillow is imported *inside* `normalize` — the one function that
genuinely needs to decode an image, and the one only the dashboard calls.
Importing this module stays free.
"""

import base64
import io
import os
from typing import Dict, Optional

# Sized for the largest surface, not the common one. Table cells and bracket
# rows want ~40px and the hero cards ~50px, all of which 128 covered — but the
# champion card runs to 7rem, which is 224 device pixels on a 2x projector, and
# that is the one frame of the whole show nobody should see soft. 256 covers it
# with headroom; the cost is a few tens of KB per bot, paid once.
SIZE = 256

# Refused before decoding, so a huge file is never handed to Pillow at all.
# Generous for a picture that ends up as a small square — the point is a
# ceiling, not a squeeze.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# A decompression bomb is small on disk and enormous in memory, so the byte cap
# above does not cover it; this is the pixel cap that does. Pillow warns past
# its own threshold and we would rather refuse than warn.
MAX_PIXELS = 50_000_000

# What the file uploader accepts. WebP is in because phones and design tools
# emit it by default now; anything else is a conversion the submitter can do.
UPLOAD_TYPES = ["png", "jpg", "jpeg", "webp"]

# Where exported avatars live under the app's static directory. Streamlit
# serves `static/` at `/app/static/`, which is how the broadcast — a sandboxed
# iframe that can fetch but cannot read the database — gets at them.
DIR_NAME = "avatars"

# How a non-square upload is made square. Neither is right for both kinds of
# picture people submit, which is why it is the submitter's call:
#
#   crop — fill the square and lose the edges. Right for a photo, where the
#          subject is in the middle and the margins are nothing.
#   fit  — shrink the whole image inside the square and pad the rest with
#          transparency. Right for a wide logo or wordmark, where cropping
#          would cut the name in half.
#
# Padding is transparent rather than a colour because the surfaces behind an
# avatar are white in the tables and navy in the broadcast; any colour baked in
# here would be a visible box on one of them.
FIT_CROP = "crop"
FIT_CONTAIN = "fit"
FIT_MODES = (FIT_CROP, FIT_CONTAIN)


class BadImage(Exception):
    """The upload could not be read as an image, with a reason worth showing."""


def rel_path(bot_id: str) -> str:
    """An avatar's path relative to the static directory.

    Kept relative on purpose. It is stored in the cup payload, which is written
    by the CLI and read by the browser, and only the dashboard knows the URL
    prefix Streamlit serves static files under.
    """
    return "%s/%s.png" % (DIR_NAME, bot_id)


def normalize(raw: bytes, mode: str = FIT_CROP) -> bytes:
    """An upload as a SIZE-square PNG, or raise BadImage.

    `mode` is how a non-square picture is made square — FIT_CROP or
    FIT_CONTAIN, see FIT_MODES. Never squashed either way: a portrait photo of
    a dog should lose its edges or gain a margin, not turn the dog oval.
    """
    if mode not in FIT_MODES:
        raise BadImage("unknown fit mode %r" % mode)
    if not raw:
        raise BadImage("the file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise BadImage("the image is %.1f MB — the limit is %d MB"
                       % (len(raw) / 1048576.0, MAX_UPLOAD_BYTES // 1048576))

    try:
        from PIL import Image, ImageOps
    except ImportError:                                  # pragma: no cover
        raise BadImage("image support is not installed on this server")

    # Pillow's own guard raises DecompressionBombError past twice this. Setting
    # it per-call rather than at import keeps the module side-effect free.
    previous, Image.MAX_IMAGE_PIXELS = Image.MAX_IMAGE_PIXELS, MAX_PIXELS
    try:
        try:
            with Image.open(io.BytesIO(raw)) as img:
                img.load()                # force a full decode while we hold it
                # Phone cameras store the picture sideways and an orientation
                # tag beside it. The tag does not survive the re-encode, so it
                # has to be applied to the pixels first or the avatar is rotated.
                img = ImageOps.exif_transpose(img)
                # Flatten to RGB against nothing: transparent corners are the
                # normal case for a logo, and the surfaces behind an avatar are
                # navy in the broadcast and white in the tables, so baking in
                # either one would be wrong on the other. RGBA it is.
                img = img.convert("RGBA")
                if mode == FIT_CROP:
                    square = ImageOps.fit(
                        img, (SIZE, SIZE), method=Image.LANCZOS,
                        centering=(0.5, 0.5))
                else:
                    # Shrink the whole thing inside the square, then centre it
                    # on a transparent canvas. `contain` only ever scales down,
                    # so a picture smaller than SIZE is scaled up first — one
                    # stored size regardless of what was uploaded.
                    scaled = ImageOps.contain(
                        img.resize(_upscaled(img.size), Image.LANCZOS),
                        (SIZE, SIZE), method=Image.LANCZOS)
                    square = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
                    square.paste(scaled, ((SIZE - scaled.width) // 2,
                                          (SIZE - scaled.height) // 2))
                out = io.BytesIO()
                square.save(out, format="PNG", optimize=True)
                return out.getvalue()
        except BadImage:
            raise
        except Exception as exc:
            # Pillow raises a wide spread of types for a bad file — UnidentifiedImageError,
            # OSError mid-decode, DecompressionBombError. They all mean the same
            # thing to a submitter, and none of them should reach a traceback.
            raise BadImage("could not be read as an image (%s)"
                           % type(exc).__name__) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _upscaled(size):
    """`size` grown so its longest edge is at least SIZE, keeping the ratio.

    Only ever grows. `ImageOps.contain` refuses to enlarge, so without this a
    64px logo submitted in fit mode would sit as a 64px speck in the middle of
    a 256px canvas — technically square, useless on the champion card.
    """
    width, height = size
    longest = max(width, height)
    if longest >= SIZE:
        return size
    scale = SIZE / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def data_uri(png: Optional[bytes]) -> str:
    """A stored PNG as a `data:` URI, or "" for a bot with no avatar.

    Used by the dashboard tables, whose image column takes a URL per cell and
    is rendered by Streamlit rather than served by it.
    """
    if not png:
        return ""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def export(images: Dict[str, bytes], static_dir: str) -> int:
    """Write avatars into `<static_dir>/avatars/` as `<bot id>.png`.

    The database is the source of truth and these files are a cache of it, so
    an export overwrites whatever is there and never reads it back. Bot ids are
    content hashes, so a file's name can never go stale: the picture behind
    `ab12cd34ef56.png` is the avatar of exactly one bot for good.

    Returns how many files were written. Failures are counted out rather than
    raised — an unwritable static directory should cost the show its pictures,
    not the show.
    """
    if not images:
        return 0
    out = os.path.join(static_dir, DIR_NAME)
    try:
        os.makedirs(out, exist_ok=True)
    except OSError:
        return 0
    written = 0
    for bot_id, png in images.items():
        if not png:
            continue
        try:
            path = os.path.join(out, "%s.png" % bot_id)
            # Write-then-rename, because the broadcast polls while this runs and
            # a half-written PNG is a broken image in front of an audience.
            pending = path + ".pending"
            with open(pending, "wb") as fh:
                fh.write(png)
            os.replace(pending, path)
            written += 1
        except OSError:
            continue
    return written
