"""
artworkdb_writer.py — Generate iPod ArtworkDB + .ithmb files directly

Writes the complete artwork database to the iPod filesystem, bypassing
iTunes COM entirely. Format matches what iTunes COM generates:

ArtworkDB v6 layout (from COM-generated database analysis):
    mhfd  (132 bytes header, 3 children, version=6)
    ├── mhsd type=1  →  mhli  →  [mhii, mhii, …]
    │     Each mhii has image_id, song_dbid (8-byte),
    │     and N children: mhod(type=2) → mhni → mhod(type=3)
    ├── mhsd type=2  →  mhla  (empty album list)
    └── mhsd type=3  →  mhlf  →  [mhif, mhif]
          Each mhif defines a thumbnail format (1028, 1029)

The mhni record maps each image to its .ithmb file:
  - format_id: which format (1028 or 1029)
  - ithmb_offset: byte offset within the .ithmb file
  - image_size: bytes of image data
  - width/height: pixel dimensions

Key insight: The song_dbid in mhii is the 8-byte persistent ID
from iTunesDB mhit+112, NOT the COM TrackDatabaseID.
"""

import struct
from pathlib import Path
from PIL import Image
from ithmb_writer import IthmBuilder, IPOD_5G_FORMATS


def parse_itunesdb_dbids(ipod_drive: Path) -> list:
    """Parse iTunesDB on the iPod and return track info list.

    Args:
        ipod_drive: iPod mount point (e.g. Path('D:/'))

    Returns:
        List of dicts: [{'track_id': int, 'dbid': int}, ...]
        The dbid is the 8-byte persistent ID at mhit+112.
    """
    itunesdb_path = ipod_drive / "iPod_Control" / "iTunes" / "iTunesDB"
    if not itunesdb_path.exists():
        return []

    data = itunesdb_path.read_bytes()
    if len(data) < 244 or data[:4] != b'mhbd':
        return []

    tracks = []
    hdr_len = struct.unpack_from('<I', data, 4)[0]
    num_sections = struct.unpack_from('<I', data, 20)[0]

    pos = hdr_len
    for _ in range(num_sections):
        if pos + 16 > len(data):
            break
        s_hdr, s_total = struct.unpack_from('<II', data, pos + 4)
        s_type = struct.unpack_from('<I', data, pos + 12)[0]

        if s_type == 1:  # Track list
            cpos = pos + s_hdr
            if cpos + 12 > len(data) or data[cpos:cpos+4] != b'mhlt':
                pos += s_total
                continue
            c_hdr = struct.unpack_from('<I', data, cpos + 4)[0]
            c_count = struct.unpack_from('<I', data, cpos + 8)[0]

            it_pos = cpos + c_hdr
            for _ in range(c_count):
                if it_pos + 120 > len(data) or data[it_pos:it_pos+4] != b'mhit':
                    break
                t_hdr = struct.unpack_from('<I', data, it_pos + 4)[0]
                t_total = struct.unpack_from('<I', data, it_pos + 8)[0]
                track_id = struct.unpack_from('<I', data, it_pos + 16)[0]

                dbid = 0
                if t_hdr >= 120:
                    dbid = struct.unpack_from('<Q', data, it_pos + 112)[0]

                tracks.append({
                    'track_id': track_id,
                    'dbid': dbid,
                })
                it_pos += t_total
            break

        pos += s_total

    return tracks


class ArtworkDB:
    """Build and write iPod ArtworkDB + .ithmb files.

    Generates the exact same format that iTunes COM produces (version 6).

    Usage:
        db = ArtworkDB()
        db.add_artwork(song_dbid=12345, image=pil_image)
        db.write(Path('D:/iPod_Control/Artwork'))
    """

    # Header sizes matching COM-generated ArtworkDB (version 6)
    MHFD_HEADER_SIZE = 132
    MHSD_HEADER_SIZE = 96
    MHLI_HEADER_SIZE = 92
    MHII_HEADER_SIZE = 152
    MHOD_HEADER_SIZE = 24   # mhod container header
    MHNI_HEADER_SIZE = 76   # mhni image info
    MHLA_HEADER_SIZE = 92
    MHLF_HEADER_SIZE = 92
    MHIF_HEADER_SIZE = 124
    DB_VERSION = 6

    def __init__(self, formats: dict = None):
        self.formats = formats or IPOD_5G_FORMATS
        self._dbid_order: list[int] = []
        self._image_dedup: dict[int, int] = {}
        self._dbid_to_image_id: dict[int, int] = {}
        self._unique_images: dict[int, Image.Image] = {}
        self._next_image_id = 101  # Match COM behavior (starts at 101)

    def add_artwork(self, song_dbid: int, image: Image.Image) -> int:
        """Register artwork for a track. Returns image_id."""
        if song_dbid in self._dbid_to_image_id:
            return self._dbid_to_image_id[song_dbid]

        img_rgb = image.convert('RGB')
        thumb = img_rgb.resize((32, 32), Image.NEAREST)
        img_hash = hash(thumb.tobytes())

        if img_hash in self._image_dedup:
            image_id = self._image_dedup[img_hash]
        else:
            image_id = self._next_image_id
            self._next_image_id += 1
            self._image_dedup[img_hash] = image_id
            self._unique_images[image_id] = img_rgb

        self._dbid_to_image_id[song_dbid] = image_id
        self._dbid_order.append(song_dbid)
        return image_id

    def write(self, artwork_dir: Path) -> dict:
        """Write ArtworkDB and .ithmb files to iPod."""
        artwork_dir.mkdir(parents=True, exist_ok=True)

        # Build .ithmb files
        builders: dict[int, IthmBuilder] = {}
        for fmt_id, (w, h, bpp) in self.formats.items():
            builders[fmt_id] = IthmBuilder(fmt_id, w, h)

        sorted_ids = sorted(self._unique_images.keys())
        image_offsets: dict[int, dict[int, int]] = {}

        for image_id in sorted_ids:
            img = self._unique_images[image_id]
            image_offsets[image_id] = {}
            for fmt_id, builder in builders.items():
                offset = builder.add_image(img)
                image_offsets[image_id][fmt_id] = offset

        for fmt_id, builder in builders.items():
            ithmb_path = artwork_dir / builder.filename
            ithmb_path.write_bytes(builder.get_data())

        # Build ArtworkDB
        db_data = self._build_artworkdb(image_offsets, builders)
        db_path = artwork_dir / "ArtworkDB"
        db_path.write_bytes(db_data)

        return {
            'tracks': len(self._dbid_to_image_id),
            'unique_images': len(self._unique_images),
            'formats': list(self.formats.keys()),
            'db_size': len(db_data),
            'ithmb_files': {b.filename: len(b.get_data()) for b in builders.values()},
        }

    def _build_artworkdb(self, image_offsets: dict, builders: dict) -> bytes:
        """Serialize the complete ArtworkDB binary (v6 format)."""
        # Build mhii entries
        mhii_data = bytearray()
        mhii_count = 0
        for song_dbid in self._dbid_order:
            image_id = self._dbid_to_image_id[song_dbid]
            source_size = 0  # We don't know original JPEG size
            mhii_bytes = self._pack_mhii(image_id, song_dbid, image_offsets[image_id], source_size)
            mhii_data.extend(mhii_bytes)
            mhii_count += 1

        mhli_data = self._pack_mhli(mhii_count, bytes(mhii_data))
        mhsd1_data = self._pack_mhsd(1, mhli_data)

        mhla_data = self._pack_mhla()
        mhsd2_data = self._pack_mhsd(2, mhla_data)

        mhif_data = bytearray()
        for fmt_id, (w, h, bpp) in sorted(self.formats.items()):
            img_size = w * h * bpp
            mhif_data.extend(self._pack_mhif(fmt_id, img_size))
        mhlf_data = self._pack_mhlf(len(self.formats), bytes(mhif_data))
        mhsd3_data = self._pack_mhsd(3, mhlf_data)

        all_children = mhsd1_data + mhsd2_data + mhsd3_data
        total_size = self.MHFD_HEADER_SIZE + len(all_children)
        mhfd = self._pack_mhfd(total_size, 3, all_children)

        return mhfd

    def _pack_mhfd(self, total_size: int, child_count: int, children: bytes) -> bytes:
        """Pack mhfd header (132 bytes, version 6).

        Matches COM-generated format:
          +0:  'mhfd'
          +4:  132
          +8:  total_size
          +12: 0
          +16: 6 (version)
          +20: child_count
          +24: 0 (padding)
          +28: next_iid (next image ID to assign)
          +32-64: zeros (no hash — COM may add these but they seem optional)
          +48: 2
          +68-131: zeros
        """
        buf = bytearray(self.MHFD_HEADER_SIZE)
        struct.pack_into('<4sIIIII', buf, 0,
                         b'mhfd',
                         self.MHFD_HEADER_SIZE,
                         total_size,
                         0,
                         self.DB_VERSION,
                         child_count)
        # +28: next_iid — observed as the next available image_id
        struct.pack_into('<I', buf, 28, self._next_image_id)
        # +48: observed as 2 in COM-generated db
        struct.pack_into('<I', buf, 48, 2)
        return bytes(buf) + children

    def _pack_mhsd(self, type_id: int, children: bytes) -> bytes:
        """Pack mhsd record (96 bytes header)."""
        total = self.MHSD_HEADER_SIZE + len(children)
        buf = bytearray(self.MHSD_HEADER_SIZE)
        struct.pack_into('<4sIII', buf, 0,
                         b'mhsd',
                         self.MHSD_HEADER_SIZE,
                         total,
                         type_id)
        return bytes(buf) + children

    def _pack_mhli(self, count: int, children: bytes) -> bytes:
        """Pack mhli record (92 bytes header)."""
        buf = bytearray(self.MHLI_HEADER_SIZE)
        struct.pack_into('<4sII', buf, 0,
                         b'mhli',
                         self.MHLI_HEADER_SIZE,
                         count)
        return bytes(buf) + children

    def _pack_mhii(self, image_id: int, song_dbid: int, offsets: dict,
                    source_size: int) -> bytes:
        """Pack mhii record (152 bytes header) with mhod/mhni children.

        Matches COM-generated format:
          +0:   'mhii'
          +4:   152 (header_size)
          +8:   total_size
          +12:  child_count (number of mhod type=2 children)
          +16:  image_id
          +20:  song_dbid (8B LE)
          +28:  0
          +32:  0
          +36:  0
          +40:  0
          +44:  0
          +48:  source_size (original JPEG size in bytes)
          +56:  1 (flag)
          +60:  1 (flag)
          +76:  0x7FF80000 (observed pattern)
          +84:  0x7FF80000 (observed pattern)
          +88-151: zeros
        """
        children_data = bytearray()
        num_children = 0

        for fmt_id in sorted(self.formats.keys()):
            w, h, bpp = self.formats[fmt_id]
            ithmb_offset = offsets.get(fmt_id, 0)
            img_size = w * h * bpp
            filename = ":F" + f"{fmt_id}_1.ithmb"

            mhod2 = self._pack_mhod2_with_mhni(fmt_id, ithmb_offset, img_size,
                                                  w, h, filename)
            children_data.extend(mhod2)
            num_children += 1

        # Add mhod type=6 with mhaf (matching working iPod: 3 children per mhii)
        mhod6 = self._pack_mhod6()
        children_data.extend(mhod6)
        num_children += 1

        total_size = self.MHII_HEADER_SIZE + len(children_data)

        buf = bytearray(self.MHII_HEADER_SIZE)
        struct.pack_into('<4sIII', buf, 0,
                         b'mhii',
                         self.MHII_HEADER_SIZE,
                         total_size,
                         num_children)
        struct.pack_into('<I', buf, 16, image_id)
        struct.pack_into('<Q', buf, 20, song_dbid)
        struct.pack_into('<I', buf, 48, source_size)
        # Flags observed in working iPod
        struct.pack_into('<II', buf, 56, 1, 1)
        struct.pack_into('<I', buf, 76, 0x7FF80000)
        struct.pack_into('<I', buf, 84, 0x7FF80000)

        return bytes(buf) + bytes(children_data)

    def _pack_mhod6(self) -> bytes:
        """Pack mhod type=6 with embedded mhaf placeholder.

        Observed in working iPod: each mhni-style mhii also includes
        an mhod type=6 with an empty mhaf as the last child.

        mhod type=6: 24 bytes header
        mhaf: 96 bytes (tag + hdr_size + value 60 + zeros)
        Total: 120 bytes
        """
        mhod = bytearray(24)
        struct.pack_into('<4sIII', mhod, 0,
                         b'mhod', 24, 120, 6)
        mhaf = bytearray(96)
        struct.pack_into('<4sII', mhaf, 0,
                         b'mhaf', 96, 60)
        return bytes(mhod) + bytes(mhaf)

    def _pack_mhod2_with_mhni(self, fmt_id: int, ithmb_offset: int,
                                img_size: int, width: int, height: int,
                                filename: str) -> bytes:
        """Pack mhod type=2 container wrapping mhni + mhod type=3."""
        mhni_data = self._pack_mhni(fmt_id, ithmb_offset, img_size,
                                     width, height, filename)
        total_size = self.MHOD_HEADER_SIZE + len(mhni_data)
        buf = bytearray(self.MHOD_HEADER_SIZE)
        struct.pack_into('<4sIII', buf, 0,
                         b'mhod',
                         self.MHOD_HEADER_SIZE,
                         total_size,
                         2)
        return bytes(buf) + mhni_data

    def _pack_mhni(self, fmt_id: int, ithmb_offset: int,
                    img_size: int, width: int, height: int,
                    filename: str) -> bytes:
        """Pack mhni record (76 bytes header).

        Matches COM-generated format:
          +0:  'mhni'
          +4:  76 (header_size)
          +8:  total_size
          +12: 1 (child_count — the mhod type=3)
          +16: format_id
          +20: ithmb_offset
          +24: image_size
          +28: vertical_padding (2B signed) = 0
          +30: horizontal_padding (2B signed) = 0
          +32: height (2B unsigned)
          +34: width (2B unsigned)
          +36: 0
          +40: image_size (repeated, observed in COM db)
          +44-75: zeros
        """
        str_child = self._pack_mhod3_string(filename)
        total_size = self.MHNI_HEADER_SIZE + len(str_child)

        buf = bytearray(self.MHNI_HEADER_SIZE)
        struct.pack_into('<4sIII', buf, 0,
                         b'mhni',
                         self.MHNI_HEADER_SIZE,
                         total_size,
                         1)
        struct.pack_into('<III', buf, 16,
                         fmt_id,
                         ithmb_offset,
                         img_size)
        struct.pack_into('<hhHH', buf, 28,
                         0, 0,      # padding
                         height, width)
        # +40: image_size repeated (observed in COM-generated mhni)
        struct.pack_into('<I', buf, 40, img_size)

        return bytes(buf) + str_child

    def _pack_mhod3_string(self, s: str) -> bytes:
        """Pack mhod type=3 string record (UTF-16LE encoded filename).

        Verified from working iPod hex dump:
          +0:  'mhod' (4B)
          +4:  header_size = 24 (4B)
          +8:  total_size (4B)
          +12: type = 3 (4B)
          +16: padding = 0 (8B)
          +24: string_length (4B)
          +28: encoding = 2 (4B) — UTF-16LE
          +32: padding = 0 (4B)
          +36: string data starts here (UTF-16LE)

        total_size = 36 + string_length
        """
        str_bytes = s.encode('utf-16-le')
        header_size = 24
        total_size = 36 + len(str_bytes)

        buf = bytearray(36)
        struct.pack_into('<4sIII', buf, 0,
                         b'mhod',
                         header_size,
                         total_size,
                         3)
        struct.pack_into('<II', buf, 24,
                         len(str_bytes),
                         2)  # encoding = UTF-16LE
        # +32: padding = 0 (already zero)
        return bytes(buf) + str_bytes

    def _pack_mhla(self) -> bytes:
        buf = bytearray(self.MHLA_HEADER_SIZE)
        struct.pack_into('<4sII', buf, 0, b'mhla', self.MHLA_HEADER_SIZE, 0)
        return bytes(buf)

    def _pack_mhlf(self, count: int, children: bytes) -> bytes:
        buf = bytearray(self.MHLF_HEADER_SIZE)
        struct.pack_into('<4sII', buf, 0, b'mhlf', self.MHLF_HEADER_SIZE, count)
        return bytes(buf) + children

    def _pack_mhif(self, format_id: int, image_size: int) -> bytes:
        buf = bytearray(self.MHIF_HEADER_SIZE)
        struct.pack_into('<4sIIIII', buf, 0,
                         b'mhif',
                         self.MHIF_HEADER_SIZE,
                         self.MHIF_HEADER_SIZE,
                         0,
                         format_id,
                         image_size)
        return bytes(buf)
