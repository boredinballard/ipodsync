import re, sys, os, random, string, subprocess, webbrowser, threading, time, queue
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from flask import Flask, request, jsonify, Response, stream_with_context

try:
    import win32com.client
    import pythoncom
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, ID3NoHeaderError
except ImportError:
    print("❌ Missing dependencies: pip install flask pywin32 mutagen")
    sys.exit(1)

app = Flask(__name__)
is_busy = False # Server global lock

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

def clean_tags(p: Path, t: str, f: str):
    try:
        try: tags = ID3(p)
        except ID3NoHeaderError:
            audio = MP3(p); audio.add_tags(); tags = audio.tags
        tags.delete(p, delete_v1=True, delete_v2=True)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=t)); tags.add(TPE1(encoding=3, text=f))
        tags.add(TALB(encoding=3, text=f)); tags.add(TRCK(encoding=3, text="1"))
        tags.add(TDRC(encoding=3, text="2000")); tags.save(p, v2_version=3)
    except: pass

# --- ROUTES ---

@app.route("/api/ipod-status", methods=["POST"])
def ipod_status():
    tk_wake()
    itunes, ipod = get_ipod()
    res = jsonify({"connected": ipod is not None, "name": ipod.Name if ipod else None, "busy": is_busy})
    pythoncom.CoUninitialize() 
    return res

@app.route("/api/list-playlists", methods=["POST"])
def list_playlists():
    itunes, ipod = get_ipod()
    if not ipod: 
        pythoncom.CoUninitialize()
        return jsonify({"playlists": []})
    
    names = []
    try:
        for pl in ipod.Playlists:
            # Kind == 2 signifie que c'est une playlist normale (pas la musique entière)
            # .SpecialKind == 0 signifie que ce n'est PAS un dossier système (Films, Podcasts, etc.)
            # Cela fonctionne quelle que soit la langue d'iTunes !
            if pl.Kind == 2 and pl.SpecialKind == 0:
                names.append(pl.Name)
    except:
        pass

    res = jsonify({"playlists": sorted(list(set(names)))})
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

@app.route("/api/delete-playlist", methods=["POST"])
def delete_playlist():
    global is_busy
    is_busy = True
    pl_name = request.json.get("playlist")
    def generate():
        global is_busy
        log = lambda m: f"data: {m}\n\n"
        yield log(f"🗑️ Deep physical purge: {pl_name}")
        try:
            itunes, ipod = get_ipod()
            playlist = next((p for p in ipod.Playlists if p.Name == pl_name), None)
            if playlist:
                to_delete = [(t.Name.lower(), t.Artist.lower()) for t in playlist.Tracks]
                yield log(f"🔥 Deleting {len(to_delete)} files...")
                lib = next(p for p in ipod.Playlists if p.Kind == 1)
                deleted = 0
                for i in range(lib.Tracks.Count, 0, -1):
                    t = lib.Tracks.Item(i)
                    if (t.Name.lower(), t.Artist.lower()) in to_delete:
                        t.Delete(); deleted += 1
                time.sleep(0.5)
                playlist.Delete()
                yield log(f"✅ Completed: {deleted} files removed.")
            yield log("☑️ DONE ☑️ Playlist deleted 🎵")
        except Exception as e: yield log(f"❌ Error: {str(e)}")
        finally: 
            is_busy = False
            pythoncom.CoUninitialize()
    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/api/sync", methods=["POST"])
def sync():
    global is_busy
    is_busy = True
    data = request.json
    folder = Path(data.get("folder", ""))
    
    def generate():
        global is_busy
        log = lambda m: f"data: {m}\n\n"
        yield log(f"🚀 Preparing: {folder.name}")
        try:
            itunes, ipod = get_ipod()
            if not ipod:
                yield log("❌ iPod not found.")
                return

            mp3_files = sorted(folder.glob("*.mp3"))
            renamed_paths = []
            
            for mp3 in mp3_files:
                new_stem = slugify(mp3.stem)
                new_path = mp3.parent / f"{new_stem}.mp3"
                if mp3.name != new_path.name:
                    mp3.rename(new_path)
                    yield log(f"  ✏️ {mp3.name} → {new_path.name}")
                clean_tags(new_path, new_stem, folder.name)
                renamed_paths.append(new_path)
            
            lib = next(pl for pl in ipod.Playlists if pl.Kind == 1)
            playlist = next((p for p in ipod.Playlists if p.Name == folder.name), None)
            if not playlist: 
                playlist = itunes.CreatePlaylistInSource(folder.name, ipod)
            
            existing_in_lib = {t.Name.lower().strip(): t for t in lib.Tracks}
            transfers = 0
            
            for path in renamed_paths:
                key = path.stem.lower().strip()
                if key not in existing_in_lib:
                    yield log(f"  ✅ Transfer: {path.name}")
                    lib.AddFile(str(path.resolve()))
                    time.sleep(0.3)
                    new_t = next((t for t in lib.Tracks if t.Name.lower().strip() == key), None)
                    if new_t: 
                        playlist.AddTrack(new_t)
                        transfers += 1
                else:
                    yield log(f"  🔗 Link created: {path.name}")
                    playlist.AddTrack(existing_in_lib[key])

            if transfers > 0:
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
            
            yield log("☑️ DONE ☑️ Enjoy your playlist 🎵")
        except Exception as e: yield log(f"❌ Error: {str(e)}")
        finally:
            is_busy = False
            pythoncom.CoUninitialize()
    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/api/list-mp3", methods=["POST"])
def list_mp3():
    folder = Path(request.json.get("folder", ""))
    files = list(folder.glob("*.mp3"))
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