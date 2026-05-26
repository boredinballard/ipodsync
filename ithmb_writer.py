"""
ithmb_writer.py — Generate iPod .ithmb thumbnail files

Converts PIL Images to raw RGB565 little-endian pixel data at the exact
dimensions required by the iPod firmware. Multiple images are
concatenated sequentially into a single .ithmb file per format ID.

Supported devices:
  iPod 5th Gen / 5.5th Gen:
    Format 1028: 100×100 — Small thumbnail (list view, album grid)
    Format 1029: 200×200 — Now Playing artwork (full screen)

  iPod Classic (6th Gen, all sub-generations):
    Format 1061:  55×55  — Tiny thumbnail (list icon, row-padded to 56px stride)
    Format 1055: 128×128 — Album list thumbnail
    Format 1060: 320×320 — Cover Flow / Now Playing artwork

Pixel format: RGB565 little-endian (all models)
  Each pixel = 2 bytes, total image size = width × height × 2
  16-bit value: RRRRRGGG GGGBBBBB
  In memory (LE): low byte [GGGBBBBB] first, high byte [RRRRRGGG] second
"""

import struct
from PIL import Image

# Format ID → (width, height, bytes_per_pixel, image_data_size)
# image_data_size is the total bytes per image as declared in the mhif record.
# For most formats this equals width * height * bpp, but some formats
# (e.g. 1061) have row-stride padding and need more bytes.

# iPod 5th Gen artwork format configurations
# Verified from real ArtworkDB: F1028_1.ithmb (20000 bytes = 100×100×2)
#                                F1029_1.ithmb (80000 bytes = 200×200×2)
IPOD_5G_FORMATS = {
    1028: (100, 100, 2, 20000),    # Small thumbnail (list view)
    1029: (200, 200, 2, 80000),    # Now Playing artwork
}

# iPod Classic (6th Gen) artwork format configurations
# Verified from real ArtworkDB on iPod Classic 160GB:
#   F1061: 55×55 display, 6160 bytes/image (56px row stride × 55 rows × 2 bpp)
#   F1055: 128×128, 32768 bytes/image
#   F1060: 320×320, 204800 bytes/image
# All use RGB565 LE — same pixel encoding as 5th Gen.
IPOD_CLASSIC_FORMATS = {
    1061: (55, 55, 2, 6160),       # Tiny thumbnail (list icon, padded stride)
    1055: (128, 128, 2, 32768),    # Album list thumbnail
    1060: (320, 320, 2, 204800),   # Cover Flow / Now Playing
}


def image_to_rgb565(img: Image.Image, size: tuple[int, int],
                    data_size: int = None) -> bytes:
    """Convert a PIL Image to raw RGB565 little-endian data.

    The image is resized to exactly `size` (width, height) using high-quality
    LANCZOS resampling, converted to RGB, then each pixel is packed as a
    16-bit RGB565 value in little-endian byte order.

    If `data_size` is larger than width*height*2, zero-padding is added per row
    to match the required row stride (e.g. 55→56 pixels for format 1061).

    Args:
        img: Source PIL Image (any mode — will be converted to RGB).
        size: Target (width, height) in pixels.
        data_size: Total output bytes. If None, defaults to width*height*2.

    Returns:
        Raw bytes of length `data_size` (or width*height*2 if not specified).
    """
    w, h = size
    raw_size = w * h * 2
    if data_size is None:
        data_size = raw_size

    img = img.resize(size, Image.LANCZOS).convert('RGB')
    pixels = img.getdata()

    if data_size == raw_size:
        # No stride padding — fast path
        data = bytearray(raw_size)
        for i, (r, g, b) in enumerate(pixels):
            r5 = (r >> 3) & 0x1F
            g6 = (g >> 2) & 0x3F
            b5 = (b >> 3) & 0x1F
            pixel16 = (r5 << 11) | (g6 << 5) | b5
            struct.pack_into('<H', data, i * 2, pixel16)
        return bytes(data)
    else:
        # Stride-padded path: data_size > raw_size
        # Calculate row stride in pixels: data_size / h / 2
        row_stride_px = data_size // (h * 2)
        data = bytearray(data_size)
        for y in range(h):
            for x in range(w):
                r, g, b = pixels[y * w + x]
                r5 = (r >> 3) & 0x1F
                g6 = (g >> 2) & 0x3F
                b5 = (b >> 3) & 0x1F
                pixel16 = (r5 << 11) | (g6 << 5) | b5
                offset = (y * row_stride_px + x) * 2
                struct.pack_into('<H', data, offset, pixel16)
            # Padding pixels (row_stride_px - w) remain zero
        return bytes(data)


class IthmBuilder:
    """Accumulates multiple images into a single .ithmb file for one format ID.

    Usage:
        builder = IthmBuilder(format_id=1060, width=320, height=320,
                              image_data_size=204800)
        offset0 = builder.add_image(pil_image)
        offset1 = builder.add_image(another_image)
        # Write to iPod:
        Path('F1060_1.ithmb').write_bytes(builder.get_data())
    """

    def __init__(self, format_id: int, width: int, height: int,
                 image_data_size: int = None):
        self.format_id = format_id
        self.width = width
        self.height = height
        self.image_size = image_data_size or (width * height * 2)
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
        rgb565 = image_to_rgb565(img, (self.width, self.height),
                                 data_size=self.image_size)
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
