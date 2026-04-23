"""
ithmb_writer.py — Generate iPod .ithmb thumbnail files

Converts PIL Images to raw RGB565 little-endian pixel data at the exact
dimensions required by the iPod firmware. Multiple images are
concatenated sequentially into a single .ithmb file per format ID.

iPod 5th Gen / 5.5th Gen artwork format IDs and dimensions
(verified on real iPod hardware):
  Format 1028: 100×100 — Small thumbnail (list view, album grid)
  Format 1029: 200×200 — Now Playing artwork (full screen)

Pixel format: RGB565 little-endian
  Each pixel = 2 bytes, total image size = width × height × 2
  16-bit value: RRRRRGGG GGGBBBBB
  In memory (LE): low byte [GGGBBBBB] first, high byte [RRRRRGGG] second
"""

import struct
from PIL import Image

# iPod 5th Gen artwork format configurations
# Verified from real ArtworkDB: F1028_1.ithmb (20000 bytes = 100×100×2)
#                                F1029_1.ithmb (80000 bytes = 200×200×2)
# Format ID → (width, height, bytes_per_pixel)
IPOD_5G_FORMATS = {
    1028: (100, 100, 2),   # Small thumbnail (list view)
    1029: (200, 200, 2),   # Now Playing artwork
}


def image_to_rgb565(img: Image.Image, size: tuple[int, int]) -> bytes:
    """Convert a PIL Image to raw RGB565 big-endian data.

    The image is resized to exactly `size` (width, height) using high-quality
    LANCZOS resampling, converted to RGB, then each pixel is packed as a
    16-bit RGB565 value in big-endian byte order.

    Args:
        img: Source PIL Image (any mode — will be converted to RGB).
        size: Target (width, height) in pixels.

    Returns:
        Raw bytes of length width × height × 2.
    """
    w, h = size
    img = img.resize(size, Image.LANCZOS).convert('RGB')
    pixels = img.getdata()

    # Pre-allocate output buffer
    data = bytearray(w * h * 2)

    for i, (r, g, b) in enumerate(pixels):
        # Pack as RGB565: RRRRRGGG_GGGBBBBB
        r5 = (r >> 3) & 0x1F
        g6 = (g >> 2) & 0x3F
        b5 = (b >> 3) & 0x1F
        pixel16 = (r5 << 11) | (g6 << 5) | b5

        # Write as little-endian 16-bit (iPod 5G reads pixels as LE)
        struct.pack_into('<H', data, i * 2, pixel16)

    return bytes(data)


class IthmBuilder:
    """Accumulates multiple images into a single .ithmb file for one format ID.

    Usage:
        builder = IthmBuilder(format_id=1067, width=320, height=320)
        offset0 = builder.add_image(pil_image)
        offset1 = builder.add_image(another_image)
        # Write to iPod:
        Path('F1067_1.ithmb').write_bytes(builder.get_data())
    """

    def __init__(self, format_id: int, width: int, height: int):
        self.format_id = format_id
        self.width = width
        self.height = height
        self.image_size = width * height * 2  # bytes per image (RGB565)
        self._data = bytearray()
        self._offsets: list[int] = []

    def add_image(self, img: Image.Image) -> int:
        """Add an image and return its byte offset within the .ithmb file.

        Args:
            img: PIL Image to convert and append.

        Returns:
            Byte offset of this image within the .ithmb file.
        """
        offset = len(self._data)
        rgb565 = image_to_rgb565(img, (self.width, self.height))
        self._data.extend(rgb565)
        self._offsets.append(offset)
        return offset

    def get_offset(self, index: int) -> int:
        """Get the byte offset of the image at the given index."""
        return self._offsets[index]

    def get_data(self) -> bytes:
        """Return the complete .ithmb file content."""
        return bytes(self._data)

    @property
    def count(self) -> int:
        """Number of images added."""
        return len(self._offsets)

    @property
    def filename(self) -> str:
        """Standard iPod .ithmb filename for this format."""
        return f"F{self.format_id}_1.ithmb"
