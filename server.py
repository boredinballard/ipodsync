import re, sys, os, random, string, subprocess, webbrowser, threading, time, queue, tempfile, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from flask import Flask, request, jsonify, Response, stream_with_context

try:
    import win32com.client
    import pythoncom
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, APIC, ID3NoHeaderError
    from mutagen.flac import FLAC
    from PIL import Image
    import io
except ImportError:
    print("❌ Missing dependencies: pip install flask pywin32 mutagen Pillow")
    sys.exit(1)

SUPPORTED_EXTENSIONS = {'.mp3', '.flac'}
CONVERSION_WORKERS = min(os.cpu_count() or 4, 4)  # Max parallel FFmpeg processes
ALBUM_ART_SIZE = (500, 500)  # Target album art dimensions in px (optimized for iPod)

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

def extract_album_art(p: Path) -> bytes | None:
    """Extract embedded album art from a FLAC or MP3 file."""
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

def clean_tags(p: Path, title: str, artist: str, album: str, track_number: str = None, artwork_data: bytes = None):
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
        tags.add(TDRC(encoding=3, text="2000"))
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

def convert_flac_to_mp3(src: Path, dest_dir: Path, bitrate: int = 320) -> Path:
    """Convert a FLAC file to MP3 using FFmpeg at the specified bitrate. Returns the output path."""
    dest = dest_dir / f"{src.stem}.mp3"
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', str(src), '-codec:a', 'libmp3lame', '-b:a', f'{bitrate}k',
         '-write_id3v2', '1', '-id3v2_version', '3', str(dest)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "FFmpeg error")
    return dest

def prepare_file(audio: Path, temp_dir: Path, folder: Path, cancel_event: threading.Event, ffmpeg_ok: threading.Event, bitrate: int = 320):
    """Worker function: prepare a single audio file for iPod transfer.
    Converts FLAC→MP3 or copies MP3 to temp dir, then applies ID3 tags.
    Runs in a thread pool — must NOT touch COM objects.
    Returns a result dict with file info or error details.
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

    # Read the track number from source before any conversion
    track_number = get_source_track_number(audio)

    # Extract and resize album art from source before conversion
    raw_art = extract_album_art(audio)
    artwork_data = resize_album_art(raw_art) if raw_art else None

    result = {
        "audio": audio,
        "artist": artist_name,
        "album": album_name,
        "new_stem": new_stem,
        "final_path": None,
        "error": None,
        "ffmpeg_missing": False,
    }

    # Bail early if cancelled
    if cancel_event.is_set():
        result["error"] = "cancelled"
        return result

    temp_subdir = temp_dir / slugify(artist_name) / slugify(album_name)
    temp_subdir.mkdir(parents=True, exist_ok=True)

    if ext == '.flac':
        if not ffmpeg_ok.is_set():
            result["error"] = "ffmpeg_missing"
            result["ffmpeg_missing"] = True
            return result
        try:
            mp3_path = convert_flac_to_mp3(audio, temp_subdir, bitrate)
            final_path = temp_subdir / f"{new_stem}.mp3"
            if mp3_path != final_path:
                mp3_path.rename(final_path)
        except FileNotFoundError:
            ffmpeg_ok.clear()  # Signal all other workers to skip FLACs
            result["error"] = "ffmpeg_not_found"
            result["ffmpeg_missing"] = True
            return result
        except (RuntimeError, Exception) as e:
            result["error"] = str(e)
            return result
    else:
        # MP3 — copy to temp dir (never modify source folder)
        final_path = temp_subdir / f"{new_stem}.mp3"
        shutil.copy2(str(audio), str(final_path))

    # Tag the file (mutagen, no COM) — preserve original track number and album art
    clean_tags(final_path, new_stem, artist_name, album_name, track_number, artwork_data)
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

@app.route("/api/sync", methods=["POST"])
def sync():
    global is_busy
    is_busy = True
    cancel_event.clear()
    data = request.json
    folder = Path(data.get("folder", ""))
    bitrate = data.get("bitrate", 320)
    
    def generate():
        global is_busy
        log = lambda m: f"data: {m}\n\n"
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
            existing_in_lib = set()
            for t in lib.Tracks:
                try:
                    existing_in_lib.add(t.Name.lower().strip())
                except:
                    continue
            yield log(f"📋 iPod has {len(existing_in_lib)} existing tracks")

            # --- Scan source folder for audio files ---
            audio_files = sorted([f for f in folder.rglob('*') if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS])
            if not audio_files:
                yield log("❌ No supported audio files found (.mp3, .flac)")
                return

            total = len(audio_files)
            yield log(f"📂 Found {total} audio files")

            temp_dir = Path(tempfile.mkdtemp(prefix="ipodsync_"))
            transfers = 0
            skipped = 0
            errors = 0

            # --- Pre-sync duplicate check (before conversion) ---
            files_to_process = []  # (original_idx, audio) tuples for non-duplicate files
            for idx, audio in enumerate(audio_files, 1):
                new_stem = slugify(audio.stem)
                tag = f"[{idx}/{total}]"
                keys_to_check = {new_stem.lower().strip()}
                source_title = get_source_title(audio)
                if source_title:
                    keys_to_check.add(source_title.lower().strip())
                if keys_to_check & existing_in_lib:
                    yield log(f"  {tag} 🔗 Already on iPod: {audio.name}")
                    skipped += 1
                else:
                    files_to_process.append((idx, audio))

            if not files_to_process:
                yield log("✅ All files already on iPod, nothing to sync.")

            # --- Multi-threaded conversion + serial transfer pipeline ---
            if files_to_process:
                ffmpeg_ok = threading.Event()
                ffmpeg_ok.set()  # Assume FFmpeg is available until proven otherwise
                yield log(f"⚡ Starting conversion pipeline ({CONVERSION_WORKERS} workers, {len(files_to_process)} files, {bitrate}kbps)...")

                with ThreadPoolExecutor(max_workers=CONVERSION_WORKERS) as executor:
                    # Submit all files for parallel preparation
                    future_list = []  # [(future, idx, audio), ...] — maintains submission order
                    for idx, audio in files_to_process:
                        future = executor.submit(prepare_file, audio, temp_dir, folder, cancel_event, ffmpeg_ok, bitrate)
                        future_list.append((future, idx, audio))

                    # Consume results in order — blocks on each future until ready
                    ffmpeg_error_logged = False
                    for future, idx, audio in future_list:
                        if cancel_event.is_set():
                            cancelled = True
                            yield log(f"⏹ Sync cancelled by user at file {idx}/{total}.")
                            # Cancel remaining pending futures
                            for f, _, _ in future_list:
                                f.cancel()
                            break

                        tag = f"[{idx}/{total}]"
                        try:
                            result = future.result()  # Blocks until this file's prep is done
                        except Exception as e:
                            yield log(f"  {tag} ❌ Unexpected error preparing: {audio.name} — {e}")
                            errors += 1
                            continue

                        # Handle worker errors
                        if result["error"]:
                            if result["error"] == "cancelled":
                                cancelled = True
                                break
                            elif result["ffmpeg_missing"] and not ffmpeg_error_logged:
                                yield log(f"  ❌ FFmpeg not found! Install FFmpeg and add it to PATH to convert FLAC files.")
                                yield log(f"  ⏭️ Skipping all FLAC files.")
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

                        yield log(f"  {tag} ✅ Transfer: {final_path.name}  ← {artist_name} / {album_name}")
                        lib.AddFile(str(final_path.resolve()))
                        time.sleep(0.3)
                        transfers += 1

            # --- Finalization ---
            if transfers > 0 and not cancelled:
                yield log("💾 Finalizing writes to iPod...")
                consecutive_errors = 0
                while True:
                    try:
                        if itunes.LibraryUpdateStatus == 0: break
                    except:
                        consecutive_errors += 1
                        if consecutive_errors > 10: break
                    time.sleep(5)
                    yield log("⏳ Syncing in progress...")

                yield log("⏳ Final stabilization (5s)...")
                time.sleep(5)
                yield log("🔌 Operation finished.")
            elif transfers > 0 and cancelled:
                yield log("💾 Stabilizing iPod after partial sync...")
                time.sleep(5)

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
        for album_name in sorted(tree[artist_name].keys(), key=str.lower):
            tracks = sorted(tree[artist_name][album_name], key=lambda t: (t["trackNumber"], t["name"].lower()))
            albums.append({"name": album_name, "tracks": tracks})
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
            capture_output=True, text=True, timeout=10
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
    folder = Path(request.json.get("folder", ""))
    files = [f for f in folder.rglob('*') if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
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