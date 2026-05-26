# iPod ArtworkDB Implementation Guide

> A complete reference for generating the iPod's artwork database directly on the filesystem, bypassing iTunes COM entirely. Derived from reverse-engineering working iPod 5th Gen and iPod Classic devices, verified with hardware testing.

---

## Architecture Overview

The iPod stores artwork in two tightly coupled components:

```
iPod_Control/
├── iTunes/
│   └── iTunesDB          ← Track database (contains persistent track IDs)
└── Artwork/
    ├── ArtworkDB          ← Artwork index (maps track IDs → image locations)
    ├── F1028_1.ithmb      ← [5G] Raw pixel data for format 1028 (100×100)
    ├── F1029_1.ithmb      ← [5G] Raw pixel data for format 1029 (200×200)
    ├── F1055_1.ithmb      ← [Classic] Raw pixel data for format 1055 (128×128)
    ├── F1060_1.ithmb      ← [Classic] Raw pixel data for format 1060 (320×320)
    └── F1061_1.ithmb      ← [Classic] Raw pixel data for format 1061 (55×55)
```

**Flow**: The firmware reads `ArtworkDB` to find which `.ithmb` file and byte offset contains the thumbnail for a given track. The track is identified by its 8-byte `song_dbid` which matches the persistent ID stored at `mhit+112` in the `iTunesDB`.

> [!IMPORTANT]
> The `song_dbid` is the 8-byte value at offset +112 in each `mhit` record of the iTunesDB. This is NOT the iTunes COM `TrackDatabaseID` property — it's a binary-only field that must be parsed directly from the iTunesDB file.

---

## Pixel Format: RGB565 Little-Endian

Each pixel is 2 bytes. The 16-bit value encodes color as:

```
Bit layout: RRRRRGGG GGGBBBBB
            [  5  ] [ 6  ] [5]
```

- **Red**: 5 bits (0-31), shifted left 11
- **Green**: 6 bits (0-63), shifted left 5
- **Blue**: 5 bits (0-31), no shift

### Encoding (Python)

```python
import struct

def rgb_to_pixel(r, g, b):
    """Convert 8-bit RGB to 16-bit RGB565."""
    r5 = (r >> 3) & 0x1F
    g6 = (g >> 2) & 0x3F
    b5 = (b >> 3) & 0x1F
    return (r5 << 11) | (g6 << 5) | b5

# Write as LITTLE-ENDIAN
pixel16 = rgb_to_pixel(255, 0, 0)  # Red = 0xF800
struct.pack('<H', pixel16)  # → bytes: 00 F8
```

> [!CAUTION]
> The iPod reads pixels as **little-endian**. Writing big-endian causes a red↔blue color swap. This was verified by displaying a red test image — BE produced blue, LE produced correct red.

### Image Sizing

Images must be resized to exactly the target dimensions. Use high-quality resampling (Lanczos) for best results. The image data is stored as a flat array of pixels, row by row, top to bottom:

```
Total bytes = width × height × 2
```

| Format ID | Dimensions | Bytes/Image | Device | Usage |
|-----------|-----------|-------------|--------|-------|
| 1028 | 100 × 100 | 20,000 | 5th Gen | List view, album grid |
| 1029 | 200 × 200 | 80,000 | 5th Gen | Now Playing (full screen) |
| 1061 | 55 × 55 | 6,160* | Classic | Tiny thumbnail (list icon) |
| 1055 | 128 × 128 | 32,768 | Classic | Album list thumbnail |
| 1060 | 320 × 320 | 204,800 | Classic | Cover Flow / Now Playing |

*Format 1061 uses a stride-padded row: each row is 56 pixels wide in memory (112 bytes) but only 55 pixels are displayed. Total data = 56 × 55 × 2 = 6,160 bytes.

> [!NOTE]
> These format IDs are device-specific. The `mhlf` section of an existing ArtworkDB can be parsed to discover which formats a device uses. All supported models use RGB565 LE.

---

## .ithmb File Format

`.ithmb` files are raw concatenations of pixel data — no headers, no separators. Each image occupies exactly `width × height × 2` bytes at a sequential offset.

```
F1028_1.ithmb:
  Offset 0:      [Image 0 — 20,000 bytes]
  Offset 20000:  [Image 1 — 20,000 bytes]
  Offset 40000:  [Image 2 — 20,000 bytes]
  ...
```

### File Naming Convention

```
F{format_id}_{file_number}.ithmb
```

Examples: `F1028_1.ithmb`, `F1029_1.ithmb`

In the ArtworkDB, these are referenced as `:F1028_1.ithmb` (with a colon prefix).

> [!TIP]
> For image deduplication, tracks sharing the same album art can point to the same offset in the `.ithmb` file. Hash the image content to detect duplicates and avoid writing redundant pixel data.

---

## ArtworkDB Binary Format (Version 6)

The ArtworkDB is a binary file using a tagged record structure similar to iTunesDB. All multi-byte integers are **little-endian**.

### Record Hierarchy

```
mhfd  (Database header — 132 bytes)
├── mhsd type=1  (Image list section)
│   └── mhli     (Image list container)
│       ├── mhii  (Image record — one per track)
│       │   ├── mhod type=2  (Image data container for format 1028)
│       │   │   └── mhni     (Image info — maps to .ithmb offset)
│       │   │       └── mhod type=3  (Filename string: ":F1028_1.ithmb")
│       │   ├── mhod type=2  (Image data container for format 1029)
│       │   │   └── mhni     (Image info — maps to .ithmb offset)
│       │   │       └── mhod type=3  (Filename string: ":F1029_1.ithmb")
│       │   └── mhod type=6  (Placeholder container)
│       │       └── mhaf     (Artwork format placeholder — 96 bytes, mostly zeros)
│       └── mhii  ...
├── mhsd type=2  (Album list section)
│   └── mhla     (Album list — typically empty, count=0)
└── mhsd type=3  (Format list section)
    └── mhlf     (Format list container)
        ├── mhif  (Format definition for 1028)
        └── mhif  (Format definition for 1029)
```

> [!IMPORTANT]
> Each `mhii` must have **(N + 1) children**: N `mhod type=2` containers (one per format) plus one `mhod type=6`/`mhaf` placeholder. For 5th Gen (2 formats): 3 children. For Classic (3 formats): 4 children. The firmware requires all of them.

---

### Record Specifications

#### `mhfd` — Database Header (132 bytes)

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhfd` (0x6D686664) |
| 4 | 4 | Header size | 132 |
| 8 | 4 | Total size | Size of entire ArtworkDB file |
| 12 | 4 | Unknown | 0 |
| 16 | 4 | DB version | **6** |
| 20 | 4 | Child count | 3 (mhsd sections) |
| 24 | 4 | Padding | 0 |
| 28 | 4 | Next image ID | Next available `image_id` to assign |
| 32-64 | 32 | Hash/checksum | Optional — zeros work fine |
| 48 | 4 | Unknown flag | 2 |
| 68-131 | 64 | Padding | Zeros |

#### `mhsd` — Section Container (96 bytes)

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhsd` |
| 4 | 4 | Header size | 96 |
| 8 | 4 | Total size | Header + all children |
| 12 | 4 | Type | 1=images, 2=albums, 3=formats |
| 16-95 | 80 | Padding | Zeros |

#### `mhli` — Image List (92 bytes)

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhli` |
| 4 | 4 | Header size | 92 |
| 8 | 4 | Count | Number of `mhii` children |
| 12-91 | 80 | Padding | Zeros |

#### `mhii` — Image Record (152 bytes header)

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 4 | Tag | `mhii` |
| 4 | 4 | Header size | 152 |
| 8 | 4 | Total size | Header + all children |
| 12 | 4 | Child count | 3 (two mhod type=2 + one mhod type=6) |
| 16 | 4 | Image ID | Unique identifier (starts at 101) |
| 20 | 8 | Song DBID | 8-byte persistent ID from iTunesDB mhit+112 |
| 28-47 | 20 | Padding | Zeros |
| 48 | 4 | Source size | Original JPEG size in bytes (0 is OK) |
| 52-55 | 4 | Padding | Zeros |
| 56 | 4 | Flag | 1 |
| 60 | 4 | Flag | 1 |
| 64-75 | 12 | Padding | Zeros |
| 76 | 4 | Float marker | 0x7FF80000 (NaN) |
| 80-83 | 4 | Padding | Zeros |
| 84 | 4 | Float marker | 0x7FF80000 (NaN) |
| 88-151 | 64 | Padding | Zeros |

> [!NOTE]
> The `image_id` is unique per image, starting at 101. Multiple tracks can share the same `image_id` (same album art = same image). The `image_id` does NOT need to match any field in iTunesDB — it's internal to ArtworkDB.

#### `mhod type=2` — Image Data Container (24 bytes header)

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhod` |
| 4 | 4 | Header size | 24 |
| 8 | 4 | Total size | 24 + mhni total size |
| 12 | 4 | Type | 2 |
| 16-23 | 8 | Padding | Zeros |

#### `mhni` — Image Info (76 bytes header)

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 4 | Tag | `mhni` |
| 4 | 4 | Header size | 76 |
| 8 | 4 | Total size | 76 + mhod3 total |
| 12 | 4 | Child count | 1 (the mhod type=3 string) |
| 16 | 4 | Format ID | e.g. 1028 or 1029 |
| 20 | 4 | ithmb offset | Byte offset in the .ithmb file |
| 24 | 4 | Image size | Bytes of pixel data (w×h×2) |
| 28 | 2 | Vertical padding | 0 (signed) |
| 30 | 2 | Horizontal padding | 0 (signed) |
| 32 | 2 | Height | Image height in pixels |
| 34 | 2 | Width | Image width in pixels |
| 36-39 | 4 | Padding | Zeros |
| 40 | 4 | Image size (repeat) | Same as offset 24 |
| 44-75 | 32 | Padding | Zeros |

#### `mhod type=3` — Filename String (variable size)

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 4 | Tag | `mhod` |
| 4 | 4 | Header size | 24 |
| 8 | 4 | Total size | **36 + string_length** |
| 12 | 4 | Type | 3 |
| 16-23 | 8 | Padding | Zeros |
| 24 | 4 | String length | Byte length of UTF-16LE string |
| 28 | 4 | Encoding | 2 (UTF-16LE) |
| 32-35 | 4 | Padding | Zeros |
| **36** | var | **String data** | UTF-16LE encoded filename |

> [!CAUTION]
> **String data starts at offset +36, NOT +40.** Total record size = 36 + string_length. Getting this wrong corrupts the entire chain.

The filename string format is: `:F{format_id}_{file_number}.ithmb`

Examples: `:F1028_1.ithmb`, `:F1029_1.ithmb`

#### `mhod type=6` + `mhaf` — Placeholder (120 bytes total)

The `mhod type=6` is a 24-byte container wrapping a 96-byte `mhaf` record:

**mhod type=6 (24 bytes):**

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhod` |
| 4 | 4 | Header size | 24 |
| 8 | 4 | Total size | 120 |
| 12 | 4 | Type | 6 |
| 16-23 | 8 | Padding | Zeros |

**mhaf (96 bytes):**

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhaf` |
| 4 | 4 | Header size | 96 |
| 8 | 4 | Unknown | 60 |
| 12-95 | 84 | Padding | Zeros |

#### `mhla` — Album List (92 bytes, empty)

Same structure as `mhli` but with count=0.

#### `mhlf` — Format List (92 bytes)

Same structure as `mhli` but contains `mhif` children.

#### `mhif` — Format Definition (124 bytes)

| Offset | Size | Field | Value |
|--------|------|-------|-------|
| 0 | 4 | Tag | `mhif` |
| 4 | 4 | Header size | 124 |
| 8 | 4 | Total size | 124 (no children) |
| 12 | 4 | Unknown | 0 |
| 16 | 4 | Format ID | e.g. 1028, 1029 |
| 20 | 4 | Image size | Bytes per image (w×h×2) |
| 24-123 | 100 | Padding | Zeros |

---

## iTunesDB: Reading Track IDs

To link artwork to tracks, you need the 8-byte `song_dbid` from each `mhit` record in the `iTunesDB`:

```python
import struct

def parse_dbids(itunesdb_bytes):
    """Extract (track_id, dbid) pairs from iTunesDB binary."""
    data = itunesdb_bytes
    hdr_len = struct.unpack_from('<I', data, 4)[0]
    num_sections = struct.unpack_from('<I', data, 20)[0]
    
    pos = hdr_len
    for _ in range(num_sections):
        s_hdr = struct.unpack_from('<I', data, pos + 4)[0]
        s_total = struct.unpack_from('<I', data, pos + 8)[0]
        s_type = struct.unpack_from('<I', data, pos + 12)[0]
        
        if s_type == 1:  # Track list
            cpos = pos + s_hdr  # mhlt
            c_hdr = struct.unpack_from('<I', data, cpos + 4)[0]
            c_count = struct.unpack_from('<I', data, cpos + 8)[0]
            
            it_pos = cpos + c_hdr
            for _ in range(c_count):
                t_hdr = struct.unpack_from('<I', data, it_pos + 4)[0]
                t_total = struct.unpack_from('<I', data, it_pos + 8)[0]
                track_id = struct.unpack_from('<I', data, it_pos + 16)[0]
                dbid = struct.unpack_from('<Q', data, it_pos + 112)[0]
                yield track_id, dbid
                it_pos += t_total
            break
        pos += s_total
```

### iTunesDB Navigation

```
mhbd (244 bytes) — Database header
├── mhsd type=4 — Podcast list (skip)
├── mhsd type=1 — Track list
│   └── mhlt — Track list container
│       ├── mhit — Track record (624 bytes header on 5G)
│       │   +16: track_id (4 bytes)
│       │   +112: song_dbid (8 bytes) ← THIS IS WHAT YOU NEED
│       └── mhit ...
├── mhsd type=3 — Playlist list
├── mhsd type=2 — Playlist list (alt)
└── mhsd type=5 — Unknown
```

> [!TIP]
> The COM `lib.Tracks` order matches the iTunesDB `mhit` order. So `lib.Tracks.Item(1)` corresponds to the first `mhit` record, allowing you to use COM metadata (artist, album) alongside the binary `song_dbid`.

---

## What Does NOT Need to be Set

Through extensive testing, we confirmed these are **not required** for artwork display:

| Field | Offset in mhit | Status |
|-------|----------------|--------|
| `mhii_link` | +156 | Not needed (stays 0) |
| `has_artwork` | +160 | Not needed (stays 0) |
| `artwork_count` | +228 | Not needed (stays 0) |
| `artwork_size` | +232 | Not needed (stays 0) |
| mhfd hash fields | +32 to +64 | Optional (zeros work) |
| mhii source_size | +48 | Optional (0 works) |

The firmware finds artwork purely through the `song_dbid` match between `mhii` and `mhit` records — no linking flags required.

---

## Adapting for Other Devices

When implementing for a new iPod model:

1. **Discover format IDs**: Parse the `mhlf` section of an existing ArtworkDB (from iTunes sync) to find the format IDs and image sizes the device uses.

2. **Verify pixel format**: Create a solid red (255,0,0) test image. If it displays blue, swap endianness. If colors are completely wrong, the device may use a different pixel format (e.g., RGB555 or UYVY).

3. **Check DB version**: Parse the `mhfd` header to confirm the version number. Older devices may use version 2.

4. **Verify record sizes**: Header sizes may vary by firmware version. Parse existing records to confirm `mhii`, `mhni`, `mhif` header sizes before generating new ones.

### Known Format IDs by Device

| Device | Format IDs | Pixel Format | Notes |
|--------|-----------|--------------|-------|
| iPod 5th/5.5th Gen | 1028 (100×100), 1029 (200×200) | RGB565 LE | ✅ Verified on hardware |
| iPod Classic (all gens) | 1055 (128×128), 1060 (320×320), 1061 (55×55) | RGB565 LE | ✅ Verified on 160GB Classic |
| iPod Nano 1G/2G | 1027 (100×100), 1031 (42×42) | RGB565 LE | From libgpod |
| iPod Nano 3G | Same as Classic | RGB565 LE | From libgpod (shared config) |
| iPod Nano 4G/5G | 1055 (128×128), 1068 (128×128), 1071 (240×240), etc. | RGB565 LE | From libgpod |
| iPod 4th Gen (Color) | 1016 (140×140), 1017 (56×56) | RGB565 LE | From libgpod |

> [!NOTE]
> iPod Classic uses format 1067 (720×480, I420/YCbCr 4:2:0) for **photos only**, not for cover art. Cover art on all tested models is exclusively RGB565 LE.

### Diagnostic Approach

1. Sync one track with artwork via iTunes
2. Parse the resulting `ArtworkDB` to discover format IDs, header sizes, and record structure
3. Read `.ithmb` pixel data and verify endianness with a solid-color test
4. Replicate the exact structure in your writer

---

## Quick Reference: Generating Artwork

```python
from artworkdb_writer import ArtworkDB, parse_itunesdb_dbids
from ithmb_writer import IPOD_5G_FORMATS, IPOD_CLASSIC_FORMATS
from PIL import Image
from pathlib import Path

ipod = Path('D:/')
artwork_dir = ipod / 'iPod_Control' / 'Artwork'

# 1. Get track IDs from iTunesDB
tracks = parse_itunesdb_dbids(ipod)

# 2. Build the artwork database (pick formats for your device)
db = ArtworkDB(formats=IPOD_CLASSIC_FORMATS)  # or IPOD_5G_FORMATS
for track in tracks:
    img = Image.open('album_art.jpg').convert('RGB')
    db.add_artwork(track['dbid'], img)

# 3. Write to iPod
stats = db.write(artwork_dir)
# Creates: ArtworkDB, F1055_1.ithmb, F1060_1.ithmb, F1061_1.ithmb
```

The entire process takes ~1-2 seconds for 50 tracks, vs 5-10 minutes with COM `AddArtworkFromFile`.
