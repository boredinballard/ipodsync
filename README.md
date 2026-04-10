# ipodsync
Sync your playlists the lazy way. For people who don't care about arranging tracks and just want their music on their iPod, now.

# 🎧 iPod LazySync OS

**"Sync your playlist in very lazy way (for lazy people who doesn't care about playlist arranging)."**

### 🚀 The "Lazy" Philosophy
This app is designed for users who hate the rigid structure of the iTunes Library. If you are used to the **"Drag & Drop"** life and you don't want to spend hours pre-arranging playlists in your library before syncing them to your device, this is for you.

**The goal:** You have a folder on your PC. You want it as a playlist on your iPod. You click one button. Done.

---

## ✨ Key Features

* **Lazy Syncing:** No need to manually import songs into iTunes first. The app bridge your local folders directly to the device.
* **Automatic Sanitization (Slugify):** Automatically renames files into a clean format (e.g., `my_song_track.mp3`) to avoid file system errors.
* **Automatic Tagging:** The app overwrites ID3 tags (Title, Artist, Album) to match the folder name. This ensures a perfectly uniform display on your iPod and simplifies your playlist view.
* **Smart Duplicate Linker:** If a song already exists in the iPod's library, the app creates a link instead of re-uploading, saving time and storage.
* **Deep Physical Purge:** A dedicated maintenance tool to completely wipe a playlist and its associated files from the iPod's physical memory.

---

## ⚙️ How it works (The Technical Flow)

Ever wondered how the app bypasses the manual iTunes struggle? Here is the step-by-step process executed every time you click **Launch Sync**:

1.  **Folder Analysis:** The script scans your selected local folder for `.mp3` files.
2.  **File Sanitization (Slugify):** To prevent database corruption or sync errors, filenames are stripped of special characters and converted to a clean format (e.g., `01 - My Song! @2024.mp3` becomes `01_my_song_2024.mp3`).
3.  **Lazy Tagging:** The script injects the **Folder Name** into the *Album* and *Artist* metadata fields of every song. This forces the iPod to group them together perfectly, regardless of their original tags.
4.  **iTunes COM Hijacking:** Using the `win32com` library, the app opens a secure communication bridge with the iTunes background process.
5.  **Smart Upload:** * It checks if a song's "Slug" is already in the iPod's library.
    * If **New**: It physically transfers the file.
    * If **Existing**: It simply creates a shortcut (link) to the existing file to save space.
6.  **Database Stabilization:** The app monitors `LibraryUpdateStatus` in real-time. It waits for iTunes to finish writing the raw data to the iPod's physical disk before releasing the lock, preventing the "Syncing..." loop or database corruption.

---

## 🛠 Prerequisites & Setup

### 1. iTunes Configuration (CRITICAL)
For this tool to work, your iPod **MUST** be configured for manual management:
1.  Connect your iPod to your PC and open iTunes.
2.  Go to the **Summary** tab of your device.
3.  Check the box: **"Manually manage music and videos"** (or *"Gérer manuellement la musique et les vidéos"*).
4.  Disable any automatic synchronization.

### 2. Environment
* **OS:** Windows (Requires iTunes for Windows installed).
* **Software:** iTunes must be running in the background.
* **Python:** Version 3.10+ recommended.

### 3. Installation
```bash
# Clone the repository
git clone [https://github.com/your-username/iPod-LazySync.git](https://github.com/your-username/iPod-LazySync.git)

# Install dependencies
pip install flask pywin32 mutagen
