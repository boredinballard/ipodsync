# 📋 iPod LazySync OS — TODO / Future Features

## 🔧 Library Management Enhancements

- [ ] **Metadata Editor** — Click a track in the library tree to view/edit its metadata (title, artist, album, genre, year, track number). Write changes back to the iPod via iTunes COM.
- [ ] **Search & Filter** — Add a search box to the library tree to quickly find artists, albums, or tracks by name.
- [ ] **Storage Info** — Show total/used/free space on the iPod in the device card. Display per-artist or per-album storage usage.
- [ ] **Sort Options** — Allow sorting library tree by name, size, date added, or track count.
- [ ] **Select All / Deselect All** — Quick buttons to toggle all checkboxes in the library tree.
- [ ] **Album Art Viewer** — Display embedded album art for selected tracks or albums.

---

## 🎵 Sync Improvements (Priority)

- [x] **Cancel / Stop Sync** — Add a cancel button that gracefully aborts a sync in progress. Should stop processing new files immediately, wait for any active transfer to finish, then stabilize the iPod database so no corruption occurs. Must also clean up the temporary conversion directory (`ipodsync_*` in system temp) so orphaned MP3s don't accumulate on disk. The UI should re-enable after cancellation.
- [x] **Pre-Sync Duplicate Check** — Before converting or transferring anything, compare the source folder against the iPod's existing library. Skip files that already exist on the device *before* running FFmpeg conversion, avoiding wasted time re-converting FLACs that are already synced. Currently, duplicates are only detected after conversion.
- [x] **Streaming Convert + Transfer Pipeline** — Instead of converting all FLACs first and then transferring all MP3s, process each file end-to-end (convert → tag → transfer) before moving on to the next. This overlaps conversion and transfer work and means the iPod starts receiving music immediately rather than waiting for the entire batch to finish converting.
- [x] **Multi-Threaded FLAC Conversion** — Convert up to 4 FLAC files simultaneously using a thread pool (`ThreadPoolExecutor`) while transferring completed files to the iPod on the main thread. Conversion and transfer are fully overlapped — the next batch of files is already converting while the current file transfers.

### Backlog
- [ ] **Selective Sync** — After browsing a folder, show a preview tree of what will be synced and let the user check/uncheck specific artists, albums, or tracks before syncing.
- [ ] **Sync History / Log** — Persist a log of past sync operations (date, folder, tracks added, errors) for reference.
- [ ] **Dry Run Mode** — Preview what a sync would do without actually transferring files — useful for verifying folder structure and tags.
- [ ] **Progress Bar** — Replace or supplement the terminal log with a visual progress bar showing percentage complete during sync.
- [ ] **Bidirectional Sync** — Option to export tracks from iPod back to a local folder (backup).

---

## 🔊 Audio & Format Support

- [ ] **AAC/M4A Support** — Extend supported formats beyond MP3 and FLAC.
- [ ] **Configurable Bitrate** — Let the user choose MP3 bitrate for FLAC conversion (128, 192, 256, 320 kbps) instead of hardcoded 320.
- [ ] **Preserve Album Art During Conversion** — Ensure FFmpeg carries over embedded cover art from FLAC to MP3.

---

## 🖥️ UI / UX Polish

- [ ] **Dark/Light Theme Toggle** — Add a theme switcher (current dark theme is default).
- [ ] **Responsive Layout** — Make the UI usable on smaller screens / tablet browsers.
- [ ] **Keyboard Shortcuts** — `Ctrl+R` to refresh library, `Delete` to remove selected, `Tab` to switch panels, etc.
- [ ] **Notification Toasts** — Replace or supplement terminal messages with brief toast popups for key events (sync complete, error, deletion done).
- [ ] **Drag & Drop Folder** — Allow dragging a folder onto the app window instead of using the file picker.

---

## ⚙️ Backend / Architecture

- [ ] **Configuration File** — A `config.json` or `.env` for settings like default music folder, preferred bitrate, port number, etc.
- [ ] **REST API Cleanup** — Standardize all endpoints to proper REST conventions (GET for reads, DELETE for deletions).
- [ ] **Error Handling Hardening** — Better COM error recovery, retry logic for flaky iTunes COM connections.
- [ ] **Packaging / Installer** — Bundle into a standalone `.exe` with PyInstaller so users don't need Python installed.
- [ ] **macOS Support** — Investigate AppleScript or Music.app integration for macOS users (currently Windows-only via iTunes COM).
