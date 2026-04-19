import re, sys, os, random, string, subprocess, webbrowser, threading, time, queue, tempfile, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from flask import Flask, request, jsonify, Response, stream_with_context
from network_utils import normalize_path, is_network_path, validate_path as validate_path_util, safe_resolve

try:
    import win32com.client
    import pythoncom
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, APIC, ID3NoHeaderError
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4, MP4Cover
    from PIL import Image
    import io
except ImportError:
    print("❌ Missing dependencies: pip install flask pywin32 mutagen Pillow")
    sys.exit(1)

# iPod-native formats — transferred as-is (no conversion)
IPOD_NATIVE_EXTENSIONS = {'.mp3', '.aac', '.m4a'}
# Formats that require FFmpeg conversion to MP3
CONVERT_EXTENSIONS = {'.flac', '.wav', '.aiff', '.aif', '.alac'}
# All recognised audio extensions
SUPPORTED_EXTENSIONS = IPOD_NATIVE_EXTENSIONS | CONVERT_EXTENSIONS
CONVERSION_WORKERS = min(os.cpu_count() or 6, 6)  # Default parallel FFmpeg processes
ALBUM_ART_SIZE = (500, 500)  # Target album art dimensions in px (fallback default)

# Device-specific profiles: art_size is (w,h) or None to skip art embedding
DEVICE_PROFILES = {
    'nano':       {'name': 'iPod Nano',            'art_size': (320, 320)},
    'mini':       {'name': 'iPod Mini',            'art_size': None},       # Mono screen
    '5gen':       {'name': 'iPod 5th / 5.5th Gen', 'art_size': (500, 500)},
    'classic':    {'name': 'iPod Classic',          'art_size': (500, 500)},
    '4gen-mono':  {'name': 'iPod 4th Gen (Mono)',   'art_size': None},       # Mono screen
    '4gen-color': {'name': 'iPod 4th Gen (Color)',  'art_size': (400, 400)},
}

app = Flask(__name__)
is_busy = False # Server global lock
cancel_event = threading.Event()  # Cancellation signal for sync

# --- TKINTER MANAGEMENT (DEDICATED THREAD) ---
_tk_queue = queue.Queue()
_tk_result = queue.Queue()

def _tk_worker():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    def process():
        try:
            task = _tk_queue.get_nowait()
            if task == 'browse':
                path = filedialog.askdirectory(parent=root)
                _tk_result.put(path.replace('/', '\\') if path else None)
            elif task == 'wake':
                root.update()
                _tk_result.put(True)
        except queue.Empty: pass
        root.after(100, process)
    root.after(100, process)
    root.mainloop()

threading.Thread(target=_tk_worker, daemon=True).start()

def tk_wake():
    _tk_queue.put('wake')
    try: _tk_result.get(timeout=2)
    except: pass

# --- HELPERS (COM INITIALIZATION) ---
def get_ipod():
    """Initialize COM on each request to prevent resource leaks"""
    pythoncom.CoInitialize()
    try:
        itunes = win32com.client.Dispatch("iTunes.Application")
        for source in itunes.Sources:
            if source.Kind == 2: return itunes, source
    except: pass
    return None, None

def slugify(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r'[^a-z0-9]+', '_', slug)
    slug = slug.strip('_')
    return slug if slug else ''.join(random.choices(string.ascii_uppercase, k=7))

def scan_audio_files(root: Path) -> list:
    """Walk directory tree for audio files. Uses os.walk for speed on network shares.
    pathlib.rglob() creates a Path object and stat()s every entry, which is
    extremely slow over SMB. os.walk batches directory reads.
    """
    results = []
    for dirpath, _, filenames in os.walk(str(root)):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                results.append(Path(dirpath) / fname)
    return results

def buffered_copy(src: Path, dst: Path, buffer_size: int = 1024 * 1024):
    """Copy file with a large buffer — significantly faster over network/SMB
    than shutil.copy2's default small buffer.
    """
    with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
        shutil.copyfileobj(fsrc, fdst, length=buffer_size)
    try:
        shutil.copystat(str(src), str(dst))
    except OSError:
        pass  # Metadata copy may fail on some network shares — non-critical

def read_with_retry(func, path, retries=1, delay=0.5):
    """Call func(path) with retry logic for transient network I/O errors."""
    for attempt in range(retries + 1):
        try:
            return func(path)
        except (IOError, OSError) as e:
            if attempt < retries:
                time.sleep(delay)
            else:
                return None

def strip_year(name: str) -> str:
    """Remove year patterns from folder/album names.
    Handles: '2021 - Album', 'Album (2021)', 'Album [2021]', 'Album - 2021'
    """
    # Leading year: "2021 - Album Name" or "2021 Album Name"
    name = re.sub(r'^\d{4}\s*[-–—]\s*', '', name)
    # Trailing year in parens/brackets: "Album Name (2021)" or "Album Name [2021]"
    name = re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '', name)
    # Trailing year with separator: "Album Name - 2021"
    name = re.sub(r'\s*[-–—]\s*\d{4}\s*$', '', name)
    return name.strip()

def get_existing_title(p: Path) -> str:
    """Read the existing title from the file's metadata, or return None."""
    try:
        tags = ID3(p)
        title_frames = tags.getall('TIT2')
        if title_frames and str(title_frames[0]).strip():
            return str(title_frames[0]).strip()
    except:
        pass
    return None

def get_source_title(p: Path) -> str:
    """Read title from any supported audio file (MP3, FLAC, etc.) using mutagen's easy interface."""
    try:
        audio = mutagen.File(str(p), easy=True)
        if audio and 'title' in audio:
            title = audio['title']
            if isinstance(title, list) and title:
                return title[0].strip()
            return str(title).strip()
    except:
        pass
    return None

def get_source_track_number(p: Path) -> str:
    """Read track number from any supported audio file using mutagen's easy interface."""
    try:
        audio = mutagen.File(str(p), easy=True)
        if audio and 'tracknumber' in audio:
            tn = audio['tracknumber']
            if isinstance(tn, list) and tn:
                return tn[0].strip()
            return str(tn).strip()
    except:
        pass
    return None

def get_source_year(p: Path) -> str:
    """Read year/date from any supported audio file using mutagen's easy interface."""
    try:
        audio = mutagen.File(str(p), easy=True)
        if audio and 'date' in audio:
            date = audio['date']
            if isinstance(date, list) and date:
                return date[0].strip()[:4]
            return str(date).strip()[:4]
    except:
        pass
    return None

def extract_album_art(p: Path) -> bytes | None:
    """Extract album art from the folder (cover.jpg/folder.jpg) or embedded in a FLAC/MP3/M4A file."""
    # 1. Check parent folder for images first (case-insensitive)
    # Build a lookup of lowercased filename -> actual path for the directory
    try:
        folder_files = {f.name.lower(): f for f in p.parent.iterdir() if f.is_file()}
    except OSError:
        folder_files = {}
    for img_name in ['cover.jpg', 'folder.jpg', 'cover.png', 'folder.png', 'front.jpg']:
        match = folder_files.get(img_name)
        if match:
            try:
                return match.read_bytes()
            except Exception:
                pass

    # 2. Fall back to embedded metadata artwork
    ext = p.suffix.lower()
    try:
        if ext == '.flac':
            audio = FLAC(str(p))
            if audio.pictures:
                return audio.pictures[0].data
        elif ext == '.mp3':
            tags = ID3(str(p))
            apic_frames = tags.getall('APIC')
            if apic_frames:
                return apic_frames[0].data
        elif ext in ('.m4a', '.aac'):
            mp4 = MP4(str(p))
            covers = mp4.tags.get('covr', [])
            if covers:
                return bytes(covers[0])
    except Exception:
        pass
    return None

def resize_album_art(image_data: bytes, size: tuple = ALBUM_ART_SIZE) -> bytes:
    """Resize album art to target dimensions and return as JPEG bytes."""
    img = Image.open(io.BytesIO(image_data))
    img = img.convert('RGB')  # Ensure RGB (strips alpha, handles palette PNGs)
    if img.size[0] > size[0] or img.size[1] > size[1]:
        img = img.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return buf.getvalue()

def clean_tags(p: Path, title: str, artist: str, album: str, track_number: str = None, artwork_data: bytes = None, year: str = None):
    try:
        # Read existing title from metadata before wiping
        existing_title = get_existing_title(p)
        final_title = existing_title if existing_title else title

        try: tags = ID3(p)
        except ID3NoHeaderError:
            audio = MP3(p); audio.add_tags(); tags = audio.tags
        tags.delete(p, delete_v1=True, delete_v2=True)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=final_title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TALB(encoding=3, text=album))
        tags.add(TRCK(encoding=3, text=track_number if track_number else "1"))
        tags.add(TDRC(encoding=3, text=year if year else "2000"))
        # Embed album art if available
        if artwork_data:
            tags.add(APIC(
                encoding=3,
                mime='image/jpeg',
                type=3,        # 3 = Cover (front)
                desc='Cover',
                data=artwork_data,
            ))
        tags.save(p, v2_version=3)
    except: pass

def clean_tags_m4a(p: Path, title: str, artist: str, album: str, track_number: str = None, artwork_data: bytes = None, year: str = None):
    """Rewrite MP4 atom tags on an AAC/M4A file for consistency with the rest of the library."""
    try:
        mp4 = MP4(str(p))
        # Read existing title — preserve if present
        existing_title = None
        if '\xa9nam' in mp4.tags:
            vals = mp4.tags['\xa9nam']
            if vals and str(vals[0]).strip():
                existing_title = str(vals[0]).strip()
        final_title = existing_title if existing_title else title

        mp4.tags['\xa9nam'] = [final_title]
        mp4.tags['\xa9ART'] = [artist]
        mp4.tags['\xa9alb'] = [album]
        mp4.tags['trkn'] = [(int(track_number.split('/')[0]) if track_number else 1, 0)]
        mp4.tags['\xa9day'] = [year if year else '2000']
        if artwork_data:
            mp4.tags['covr'] = [MP4Cover(artwork_data, imageformat=MP4Cover.FORMAT_JPEG)]
        mp4.save()
    except: pass

def convert_to_mp3(src: Path, dest_dir: Path, bitrate: int = 320) -> Path:
    """Convert any audio file to MP3 using FFmpeg at the specified bitrate. Returns the output path."""
    dest = dest_dir / f"{src.stem}.mp3"
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-codec:a', 'libmp3lame', '-b:a', f'{bitrate}k',
         '-write_id3v2', '1', '-id3v2_version', '3', str(dest)],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=tempfile.gettempdir()  # Avoid UNC path as CWD — cmd.exe rejects it
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "FFmpeg error")
    return dest

def prepare_file(audio: Path, temp_dir: Path, folder: Path, cancel_event: threading.Event, ffmpeg_ok: threading.Event, bitrate: int = 320, art_size: tuple = ALBUM_ART_SIZE, existing_composite: set = None):
    """Worker function: prepare a single audio file for iPod transfer.
    Converts FLAC→MP3 or copies MP3 to temp dir, then applies ID3 tags.
    Also performs duplicate detection against the iPod library using the
    source file's metadata title — this avoids a separate network pass.
    Runs in a thread pool — must NOT touch COM objects.
    Returns a result dict with file info or error details.
    art_size: target album art dimensions, or None to skip art embedding.
    """
    new_stem = slugify(audio.stem)
    ext = audio.suffix.lower()

    # Derive artist/album from folder structure
    relative = audio.relative_to(folder)
    parts = relative.parts  # e.g. ('Artist', 'Album', 'song.mp3')

    if len(parts) >= 3:
        artist_name = strip_year(parts[0])
        album_name = strip_year(parts[1])
    elif len(parts) == 2:
        artist_name = strip_year(parts[0])
        album_name = strip_year(parts[0])
    else:
        artist_name = strip_year(folder.name)
        album_name = strip_year(folder.name)

    # Read the source title early — used for both duplicate detection and tagging
    source_title = read_with_retry(get_source_title, audio)

    # Read the track number and year from source before any conversion
    # Uses retry logic for transient network I/O errors
    track_number = read_with_retry(get_source_track_number, audio)
    year = read_with_retry(get_source_year, audio)

    result = {
        "audio": audio,
        "artist": artist_name,
        "album": album_name,
        "new_stem": new_stem,
        "final_path": None,
        "error": None,
        "ffmpeg_missing": False,
        "skipped": False,
    }

    # --- Duplicate detection (runs inside worker — no extra network I/O) ---
    # Uses composite (artist, album, title) matching only — all three must
    # match to be considered a duplicate.  Broader fallbacks (title-only,
    # slug-only) were removed because common track names like "Introduction"
    # or "Interlude" caused false positives across different artists.
    if existing_composite is not None:
        src_artist = artist_name.lower().strip()
        src_album = album_name.lower().strip()
        src_title = (source_title or "").lower().strip()

        if src_title and (src_artist, src_album, src_title) in existing_composite:
            result["skipped"] = True
            return result

    # Extract and resize album art from source before conversion (skip if art_size is None)
    artwork_data = None
    if art_size is not None:
        raw_art = read_with_retry(extract_album_art, audio)
        artwork_data = resize_album_art(raw_art, size=art_size) if raw_art else None

    # Bail early if cancelled
    if cancel_event.is_set():
        result["error"] = "cancelled"
        return result

    temp_subdir = temp_dir / slugify(artist_name) / slugify(album_name)
    temp_subdir.mkdir(parents=True, exist_ok=True)

    if ext in CONVERT_EXTENSIONS:
        # Formats that need FFmpeg conversion to MP3 (FLAC, WAV, AIFF, ALAC)
        if not ffmpeg_ok.is_set():
            result["error"] = "ffmpeg_missing"
            result["ffmpeg_missing"] = True
            return result
        try:
            mp3_path = convert_to_mp3(audio, temp_subdir, bitrate)
            final_path = temp_subdir / f"{new_stem}.mp3"
            if mp3_path != final_path:
                mp3_path.rename(final_path)
        except FileNotFoundError:
            ffmpeg_ok.clear()  # Signal all other workers to skip conversions
            result["error"] = "ffmpeg_not_found"
            result["ffmpeg_missing"] = True
            return result
        except (RuntimeError, Exception) as e:
            result["error"] = str(e)
            return result
        # Tag the converted MP3 — preserve original track number and album art
        clean_tags(final_path, new_stem, artist_name, album_name, track_number, artwork_data, year)
    elif ext in ('.aac', '.m4a'):
        # AAC/M4A — iPod-native, copy and retag with MP4 atoms
        final_path = temp_subdir / f"{new_stem}{ext}"
        buffered_copy(audio, final_path)
        clean_tags_m4a(final_path, new_stem, artist_name, album_name, track_number, artwork_data, year)
    else:
        # MP3 — copy to temp dir (never modify source folder)
        # Uses buffered copy for better throughput over network shares
        final_path = temp_subdir / f"{new_stem}.mp3"
        buffered_copy(audio, final_path)
        # Tag the file (mutagen, no COM) — preserve original track number and album art
        clean_tags(final_path, new_stem, artist_name, album_name, track_number, artwork_data, year)
    result["final_path"] = final_path
    return result

# --- ROUTES ---

@app.route("/api/ipod-status", methods=["POST"])
def ipod_status():
    tk_wake()
    itunes, ipod = get_ipod()
    res = jsonify({"connected": ipod is not None, "name": ipod.Name if ipod else None, "busy": is_busy})
    pythoncom.CoUninitialize() 
    return res

@app.route("/api/browse-folder", methods=["POST"])
def browse_folder():
    if is_busy: return jsonify({"folder": None})
    while not _tk_result.empty():
        try: _tk_result.get_nowait()
        except: break
    _tk_queue.put('browse')
    try:
        path = _tk_result.get(timeout=60)
        if isinstance(path, bool): path = None
        if path:
            path = str(normalize_path(path))
        return jsonify({"folder": path})
    except:
        return jsonify({"folder": None})

@app.route("/api/cancel-sync", methods=["POST"])
def cancel_sync():
    """Signal the running sync to stop after the current file."""
    if is_busy:
        cancel_event.set()
        return jsonify({"cancelled": True})
    return jsonify({"cancelled": False, "error": "No sync in progress"})

@app.route("/api/validate-path", methods=["POST"])
def validate_path_route():
    """Check reachability of a user-supplied path (local, mapped drive, or UNC)."""
    raw = request.json.get("path", "")
    p = normalize_path(raw)
    result = validate_path_util(p)
    result["normalized"] = str(p)
    return jsonify(result)

@app.route("/api/set-folder", methods=["POST"])
def set_folder():
    """Manually set the source folder (supports UNC and mapped drives)."""
    if is_busy: return jsonify({"folder": None, "count": 0, "error": "Server is busy"})
    raw = request.json.get("folder", "")
    p = normalize_path(raw)
    path_str = str(p)

    # Quick reachability check
    check = validate_path_util(p)
    if not check["reachable"]:
        return jsonify({"folder": path_str, "count": 0, "error": check["error"]})

    files = scan_audio_files(p)
    return jsonify({"folder": path_str, "count": len(files), "error": None})

@app.route("/api/sync", methods=["POST"])
def sync():
    global is_busy
    is_busy = True
    cancel_event.clear()
    data = request.json
    folder = normalize_path(data.get("folder", ""))
    bitrate = data.get("bitrate", 320)
    workers = max(1, min(16, int(data.get("workers", CONVERSION_WORKERS))))  # Clamp 1-16
    device_key = data.get("device", "5gen")
    embed_art = data.get("embed_art", True)
    
    # Resolve device profile for art sizing
    device_profile = DEVICE_PROFILES.get(device_key, DEVICE_PROFILES['5gen'])
    art_size = device_profile['art_size'] if embed_art else None

    def generate():
        global is_busy
        log = lambda m: f"data: {m}\n\n"
        art_label = f"{art_size[0]}×{art_size[1]}px" if art_size else "disabled"
        yield log(f"🎨 Device: {device_profile['name']} | Art: {art_label} | Bitrate: {bitrate}kbps")
        net = is_network_path(folder)
        if net:
            yield log(f"🌐 Source: network share ({folder})")
        yield log(f"🚀 Scanning library: {folder.name}")
        temp_dir = None
        cancelled = False
        try:
            itunes, ipod = get_ipod()
            if not ipod:
                yield log("❌ iPod not found.")
                return

            # --- Pre-scan iPod library for duplicate detection ---
            yield log("🔍 Scanning iPod library for existing tracks...")
            lib = next(pl for pl in ipod.Playlists if pl.Kind == 1)
            # Composite (artist, album, title) set for precise matching —
            # all three must match to skip a file as a duplicate.
            existing_composite = set()  # {(artist_lower, album_lower, title_lower)}
            for t in lib.Tracks:
                try:
                    title = (t.Name or "").lower().strip()
                    artist = (t.Artist or "").lower().strip()
                    album = (t.Album or "").lower().strip()
                    if title:
                        existing_composite.add((artist, album, title))
                except:
                    continue
            yield log(f"📋 iPod has {len(existing_composite)} existing tracks")

            # --- Scan source folder for audio files ---
            audio_files = sorted(scan_audio_files(folder))
            if not audio_files:
                yield log("❌ No supported audio files found (.mp3, .flac, .aac, .m4a, .wav, .aiff, .alac)")
                return

            total = len(audio_files)
            yield log(f"📂 Found {total} audio files")

            temp_dir = Path(tempfile.mkdtemp(prefix="ipodsync_"))
            transfers = 0
            skipped = 0
            errors = 0
            pending_ops = []  # IITOperationStatus objects from AddFile()

            # --- Helper: reconnect COM to iTunes/iPod ---
            # The iTunes COM interface degrades after thousands of sequential
            # AddFile() calls, eventually throwing E_INVALIDARG (0x80070057).
            # This helper tears down and rebuilds the connection.
            COM_RECONNECT_INTERVAL = 500  # Proactive reconnect every N transfers

            def reconnect_com():
                """Reinitialize COM and re-acquire iTunes/iPod/library refs."""
                nonlocal itunes, ipod, lib
                try:
                    pythoncom.CoUninitialize()
                except:
                    pass
                pythoncom.CoInitialize()
                itunes = win32com.client.Dispatch("iTunes.Application")
                ipod = None
                for source in itunes.Sources:
                    if source.Kind == 2:
                        ipod = source
                        break
                if ipod:
                    lib = next(pl for pl in ipod.Playlists if pl.Kind == 1)
                return ipod is not None

            # --- Combined duplicate-check + conversion pipeline ---
            # Duplicate detection is performed inside each prepare_file worker
            # using the metadata title that's already being read. This avoids
            # a separate metadata-scan pass over the network.
            ffmpeg_ok = threading.Event()
            ffmpeg_ok.set()  # Assume FFmpeg is available until proven otherwise
            yield log(f"⚡ Starting pipeline ({workers} workers, {total} files, {bitrate}kbps)...")

            with ThreadPoolExecutor(max_workers=workers) as executor:
                # Submit ALL files — workers will self-filter duplicates
                future_list = []  # [(future, idx, audio), ...] — maintains submission order
                for idx, audio in enumerate(audio_files, 1):
                    future = executor.submit(
                        prepare_file, audio, temp_dir, folder, cancel_event,
                        ffmpeg_ok, bitrate, art_size, existing_composite
                    )
                    future_list.append((future, idx, audio))

                # Consume results in order — blocks on each future until ready
                ffmpeg_error_logged = False
                for process_idx, (future, idx, audio) in enumerate(future_list, 1):
                    if cancel_event.is_set():
                        cancelled = True
                        yield log(f"⏹ Sync cancelled by user at file {idx}/{total}.")
                        # Cancel remaining pending futures
                        for f, _, _ in future_list:
                            f.cancel()
                        break

                    tag = f"[{process_idx}/{total}]"
                    try:
                        result = future.result()  # Blocks until this file's prep is done
                    except Exception as e:
                        yield log(f"  {tag} ❌ Unexpected error preparing: {audio.name} — {e}")
                        errors += 1
                        continue

                    # Handle duplicates detected by worker
                    if result["skipped"]:
                        yield log(f"  {tag} 🔗 Already on iPod: {audio.name}")
                        skipped += 1
                        continue

                    # Handle worker errors
                    if result["error"]:
                        if result["error"] == "cancelled":
                            cancelled = True
                            break
                        elif result["ffmpeg_missing"] and not ffmpeg_error_logged:
                            yield log(f"  ❌ FFmpeg not found! Install FFmpeg and add it to PATH to convert audio files.")
                            yield log(f"  ⏭️ Skipping all files that require conversion.")
                            ffmpeg_error_logged = True
                            errors += 1
                            continue
                        elif result["ffmpeg_missing"]:
                            # Already logged the FFmpeg error, silently skip
                            continue
                        else:
                            yield log(f"  {tag} ❌ Conversion failed: {audio.name} — {result['error']}")
                            errors += 1
                            continue

                    # --- Transfer to iPod on main thread (COM) ---
                    final_path = result["final_path"]
                    artist_name = result["artist"]
                    album_name = result["album"]

                    if cancel_event.is_set():
                        cancelled = True
                        yield log(f"⏹ Sync cancelled by user at file {idx}/{total}.")
                        break

                    # --- Proactive COM reconnect to prevent staleness ---
                    if transfers > 0 and transfers % COM_RECONNECT_INTERVAL == 0:
                        yield log(f"  🔄 Refreshing iTunes connection ({transfers} transfers)...")
                        # Wait briefly for pending ops before reconnecting
                        time.sleep(1)
                        if reconnect_com():
                            yield log(f"  ✅ Connection refreshed.")
                        else:
                            yield log(f"  ❌ Lost iPod connection after refresh!")
                            break

                    yield log(f"  {tag} ✅ Transfer: {final_path.name}  ← {artist_name} / {album_name}")

                    # --- AddFile with retry + COM reconnect on failure ---
                    add_ok = False
                    resolved_path = str(final_path.resolve())
                    for attempt in range(4):  # Up to 4 attempts (1 initial + 3 retries)
                        try:
                            op_status = lib.AddFile(resolved_path)
                            if op_status is not None:
                                pending_ops.append((op_status, final_path.name))
                            add_ok = True
                            break
                        except Exception as add_err:
                            if attempt < 3:
                                wait = (attempt + 1) * 2  # 2s, 4s, 6s backoff
                                yield log(f"  ⚠️ AddFile failed (attempt {attempt+1}/4), retrying in {wait}s...")
                                time.sleep(wait)
                                # On 2nd+ retry, fully reconnect COM
                                if attempt >= 1:
                                    yield log(f"  🔄 Reconnecting iTunes COM interface...")
                                    if not reconnect_com():
                                        yield log(f"  ❌ Could not reconnect to iPod!")
                                        break
                            else:
                                yield log(f"  ❌ AddFile failed after 4 attempts: {final_path.name} — {add_err}")
                                errors += 1

                    if not add_ok:
                        continue  # Skip to next file

                    time.sleep(0.3)
                    transfers += 1

            if transfers == 0 and skipped > 0 and errors == 0:
                yield log("✅ All files already on iPod, nothing to sync.")

            # --- Finalization: Wait for iPod transfer to complete ---
            # AddFile() returns an IITOperationStatus whose .InProgress
            # property is True while iTunes is still copying the file to
            # the iPod.  We poll every pending operation until all have
            # finished, keeping the temp directory (source files) and the
            # COM connection alive the entire time.
            if transfers > 0 and pending_ops and not cancelled:
                yield log(f"💾 Finalizing — waiting for {len(pending_ops)} iPod transfers to complete...")

                POLL_INTERVAL = 3        # seconds between checks
                MAX_WAIT = 3600          # 1-hour safety timeout
                elapsed = 0

                while elapsed < MAX_WAIT and pending_ops:
                    time.sleep(POLL_INTERVAL)
                    elapsed += POLL_INTERVAL

                    # Filter out completed operations
                    still_pending = []
                    for op, name in pending_ops:
                        try:
                            if op.InProgress:
                                still_pending.append((op, name))
                        except:
                            pass  # COM error — treat as complete
                    pending_ops = still_pending

                    # Log progress every ~15 s
                    if elapsed % 15 == 0 and pending_ops:
                        yield log(f"⏳ {len(pending_ops)} transfers still in progress... ({elapsed}s elapsed)")

                if elapsed >= MAX_WAIT and pending_ops:
                    yield log(f"⚠️ Transfer wait timed out — {len(pending_ops)} ops still pending.")
                else:
                    yield log("🔌 All transfers complete.")

            elif transfers > 0 and not cancelled:
                # AddFile didn't return status objects — brief fallback wait
                yield log("💾 Finalizing (no operation status available)...")
                time.sleep(15)
                yield log("🔌 Done.")

            elif transfers > 0 and cancelled:
                # Give in-flight transfers a chance to finish
                yield log("💾 Stabilizing iPod after partial sync...")
                POLL_INTERVAL = 3
                MAX_WAIT = 300
                elapsed = 0
                while elapsed < MAX_WAIT and pending_ops:
                    time.sleep(POLL_INTERVAL)
                    elapsed += POLL_INTERVAL
                    still_pending = []
                    for op, n in pending_ops:
                        try:
                            if op.InProgress:
                                still_pending.append((op, n))
                        except:
                            pass
                    pending_ops = still_pending
                    if elapsed % 15 == 0 and pending_ops:
                        yield log(f"⏳ Stabilizing — {len(pending_ops)} ops pending ({elapsed}s)")

            # --- Summary ---
            summary_parts = [f"{transfers} synced"]
            if skipped > 0:
                summary_parts.append(f"{skipped} already on iPod")
            if errors > 0:
                summary_parts.append(f"{errors} error{'s' if errors != 1 else ''}")
            summary = ", ".join(summary_parts)

            if cancelled:
                yield log(f"⚠️ CANCELLED — {summary}")
            else:
                yield log(f"☑️ DONE ☑️ {summary}. Enjoy your music 🎵")
        except Exception as e:
            tb = traceback.format_exc()
            yield log(f"❌ Error: {str(e)}")
            for line in tb.strip().splitlines():
                yield log(f"  📋 {line}")
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            cancel_event.clear()
            is_busy = False
            pythoncom.CoUninitialize()
    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/api/ipod-library", methods=["POST"])
def ipod_library():
    """Read all tracks from iPod library, return as Artist->Album->Track tree."""
    tk_wake()
    itunes, ipod = get_ipod()
    if not ipod:
        pythoncom.CoUninitialize()
        return jsonify({"connected": False, "library": []})

    tree = {}  # { artist: { album: [track_dicts] } }
    lib = next((pl for pl in ipod.Playlists if pl.Kind == 1), None)
    if lib:
        for i in range(1, lib.Tracks.Count + 1):
            try:
                t = lib.Tracks.Item(i)
                artist = t.Artist or "Unknown Artist"
                album = t.Album or "Unknown Album"
                track = {
                    "name": t.Name or "Untitled",
                    "trackId": t.TrackDatabaseID,
                    "trackNumber": t.TrackNumber or 0,
                    "year": t.Year or 0,
                    "duration": t.Duration,
                    "size": t.Size,
                }
                tree.setdefault(artist, {}).setdefault(album, []).append(track)
            except:
                continue

    # Convert to sorted array structure for frontend
    library = []
    for artist_name in sorted(tree.keys(), key=str.lower):
        albums = []
        for album_name in tree[artist_name]:
            tracks = sorted(tree[artist_name][album_name], key=lambda t: (t["trackNumber"], t["name"].lower()))
            # Use earliest non-zero year from tracks as album year
            album_year = min((t["year"] for t in tracks if t["year"]), default=0)
            albums.append({"name": album_name, "year": album_year, "tracks": tracks})
        # Sort albums by year (0 = unknown goes last), then alphabetically
        albums.sort(key=lambda a: (a["year"] == 0, a["year"], a["name"].lower()))
        library.append({"name": artist_name, "albums": albums})

    pythoncom.CoUninitialize()
    return jsonify({"connected": True, "library": library})

@app.route("/api/delete-tracks", methods=["POST"])
def delete_tracks():
    """Delete tracks from iPod by TrackDatabaseID."""
    global is_busy
    if is_busy:
        return jsonify({"success": False, "deleted": 0, "error": "Server is busy"})

    is_busy = True
    track_ids = set(request.json.get("trackIds", []))

    itunes, ipod = get_ipod()
    if not ipod:
        is_busy = False
        pythoncom.CoUninitialize()
        return jsonify({"success": False, "deleted": 0, "error": "iPod not found"})

    lib = next((pl for pl in ipod.Playlists if pl.Kind == 1), None)
    deleted = 0
    errors = []

    # Iterate in reverse to safely delete by index
    for i in range(lib.Tracks.Count, 0, -1):
        try:
            t = lib.Tracks.Item(i)
            if t.TrackDatabaseID in track_ids:
                t.Delete()
                deleted += 1
        except Exception as e:
            errors.append(str(e))

    is_busy = False
    pythoncom.CoUninitialize()
    return jsonify({"success": True, "deleted": deleted, "errors": errors})

@app.route("/api/check-ffmpeg", methods=["POST"])
def check_ffmpeg():
    """Check if FFmpeg is installed and reachable on PATH."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10
        )
        if result.returncode == 0:
            # First line of ffmpeg -version contains the version string
            version_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
            return jsonify({"installed": True, "version": version_line, "error": None})
        else:
            return jsonify({"installed": False, "version": None, "error": "ffmpeg returned a non-zero exit code"})
    except FileNotFoundError:
        return jsonify({"installed": False, "version": None, "error": "ffmpeg not found on PATH"})
    except subprocess.TimeoutExpired:
        return jsonify({"installed": False, "version": None, "error": "ffmpeg check timed out"})
    except Exception as e:
        return jsonify({"installed": False, "version": None, "error": str(e)})

@app.route("/api/list-mp3", methods=["POST"])
def list_mp3():
    folder = normalize_path(request.json.get("folder", ""))
    files = scan_audio_files(folder)
    return jsonify({"count": len(files)})

@app.route("/")
def index(): return open("index.html", encoding="utf-8").read()

if __name__ == "__main__":
    def launch():
        edge = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
        url = "http://localhost:5000"
        if os.path.exists(edge): subprocess.Popen([edge, url])
        else: webbrowser.open(url)
    threading.Timer(1.5, launch).start()
    app.run(port=5000)