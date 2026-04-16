# ipodsync
✨ Sync your music library the lazy way. For people who just want their music on their iPod, now.
✨ Read this readme first please

# 🎧 iPod LazySync OS

**"Sync your music library with proper Artist/Album structure — no iTunes library management required."**

### 🚀 The "Lazy" Philosophy
This app is designed for users who hate the rigid structure of the iTunes Library. If you are used to the **"Drag & Drop"** life and you don't want to spend hours managing your iTunes library before syncing to your device, this is for you.

**The goal:** You have a music folder on your PC organized as `Artist/Album/songs`. You point the app at it. You click one button. Your iPod gets properly tagged music with correct Artist and Album structure. Done.

---

## ✨ Key Features

### Syncing
* **Lazy Syncing:** No need to manually import songs into iTunes first. The app bridges your local folders directly to the device.
* **Artist/Album Structure:** Your folder hierarchy (`Artist/Album/song.mp3`) is preserved — songs are tagged with the correct Artist and Album metadata so they display properly on your iPod.
* **Metadata Track Names:** The original track title from your files' metadata is preserved. Only Artist and Album tags are overwritten based on folder structure.
* **FLAC → MP3 Conversion:** FLAC files are automatically converted to **MP3 320kbps** using FFmpeg before syncing. Your original FLAC files are never modified.
* **Automatic Sanitization (Slugify):** Automatically renames files into a clean format (e.g., `my_song_track.mp3`) to avoid file system errors.
* **Automatic Year Stripping:** Year patterns are automatically removed from folder names before tagging (e.g., `2021 - Album Name`, `Album Name (2021)`, `Album Name [2021]` all become `Album Name`).
* **Smart Duplicate Detection:** If a song already exists in the iPod's library, it is skipped to save time and storage.

### Library Management
* **Library Explorer:** Browse all music currently on your iPod in a collapsible **Artist → Album → Track** tree view.
* **Batch Deletion:** Select entire artists, albums, or individual tracks using tri-state checkboxes and delete them from the iPod in one click.
* **Track Count Badges:** See at a glance how many tracks each artist and album contains.
* **Confirmation Safety:** A confirmation dialog prevents accidental deletions, showing exactly how many tracks will be removed.

### Interface
* **Tabbed Layout:** Switch between a real-time **Terminal** log (for sync progress) and the **Library** tree explorer.
* **Device Detection:** Automatic iPod detection with live status indicator.
* **FFmpeg Check:** Automatic verification that FFmpeg is installed and available for FLAC conversion.
* **Dark Cyberpunk UI:** A stylish dark interface built with custom CSS — no frameworks required.

---

## ⚙️ How it works (The Technical Flow)

Ever wondered how the app bypasses the manual iTunes struggle? Here is the step-by-step process executed every time you click **Launch Sync**:

1.  **Folder Analysis:** The script recursively scans your selected music folder for `.mp3` and `.flac` files.
2.  **Structure Detection:** For each file, the app derives the **Artist** and **Album** from the folder hierarchy:
    * `Music/Artist/Album/song.mp3` → Artist = `Artist`, Album = `Album`
    * `Music/SubFolder/song.mp3` → Artist = `SubFolder`, Album = `SubFolder`
    * `Music/song.mp3` → Artist = `Music`, Album = `Music`
3.  **FLAC Conversion:** Any `.flac` files are converted to **MP3 320kbps** using FFmpeg into a temporary directory. The original FLAC files remain untouched.
4.  **File Sanitization (Slugify):** To prevent database corruption or sync errors, filenames are stripped of special characters and converted to a clean format (e.g., `01 - My Song! @2024.mp3` becomes `01_my_song_2024.mp3`).
5.  **Year Stripping:** Year patterns are stripped from folder/artist/album names to produce clean labels (e.g., `2021 - Album Name` → `Album Name`).
6.  **Smart Tagging:** The script reads the **existing track title** from the file's metadata and preserves it. The **Artist** and **Album** fields are set based on folder structure. If no existing title is found, the filename is used as fallback.
7.  **iTunes COM Bridge:** Using the `win32com` library, the app opens a secure communication bridge with the iTunes background process.
8.  **Smart Upload:** It checks if a song already exists in the iPod's library. New tracks are physically transferred; existing tracks are skipped.
9.  **Database Stabilization:** The app monitors `LibraryUpdateStatus` in real-time. It waits for iTunes to finish writing the raw data to the iPod's physical disk before releasing the lock, preventing the "Syncing..." loop or database corruption.

---

## 🗑️ How Library Management Works

The Library Manager lets you see and manage what's already on your iPod:

1.  **Scan:** Click **REFRESH LIBRARY** to read all tracks from the iPod. The app queries every track via the iTunes COM interface and builds a tree grouped by Artist and Album.
2.  **Browse:** Expand/collapse artists and albums in the tree. Track counts are shown as badges.
3.  **Select:** Use checkboxes at any level — checking an artist selects all their albums and tracks. Partial selections show an indeterminate state.
4.  **Delete:** Click **DELETE SELECTED** to remove chosen tracks. A confirmation dialog shows exactly what will be removed. Tracks are deleted via reverse-index iteration to prevent database corruption.
5.  **Refresh:** The library automatically refreshes after deletion to reflect the updated state.

---

## 🛠 Prerequisites & Setup

### 1. iTunes Configuration (CRITICAL)
For this tool to work, your iPod **MUST** be configured for manual management:
1.  Connect your iPod to your PC and open iTunes.
2.  Go to the **Summary** tab of your device.
3.  Check the box: **"Manually manage music and videos"** (or *"Gérer manuellement la musique et les vidéos"*).
4.  Disable any automatic synchronization.

### 2. Folder Structure
Organize your music folder like this:
```
D:\Music\
├── Artist A\
│   ├── Album 1\
│   │   ├── 01 track.mp3
│   │   └── 02 track.mp3
│   └── Album 2\
│       └── 01 track.mp3
└── Artist B\
    └── Album 1\
        └── 01 track.flac
```

### 3. Environment
* **OS:** Windows (Requires iTunes for Windows installed).
* **Software:** iTunes must be running in the background.
* **FFmpeg:** Required for FLAC → MP3 conversion. Download from [ffmpeg.org](https://ffmpeg.org/download.html) and ensure `ffmpeg` is available in your system PATH.
* **Python:** Version 3.10+ recommended.
* **Install dependencies first:** `pip install flask pywin32 mutagen`

### 4. Running the App
```bash
python server.py
```
The app will automatically open in your default browser at `http://localhost:5000`.

---

## 📁 Project Structure

```
ipodsync/
├── server.py      # Flask backend — API routes, iTunes COM bridge, sync logic
├── index.html     # Frontend — UI, tree explorer, terminal, styling
├── README.md      # This file
└── TODO.md        # Future feature roadmap
```

---

## 🛣️ Roadmap

See [TODO.md](TODO.md) for planned features including a metadata editor, search/filter, selective sync, additional format support, and more.
