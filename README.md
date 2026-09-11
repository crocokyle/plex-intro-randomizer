# Plex Intro Randomizer & Google Photos Sync

A robust, self-contained Python automation tool for **Linux Mint** (and other Linux distributions) that:
1. **One-way syncs (downloads)** short video clips daily from a public Google Photos shared album.
2. **Automatically transcodes** non-MP4 formats (such as `.MOV` and `.wmv`) to standard H.264/AAC `.mp4` using `ffmpeg` so Plex can Direct Play them without issues.
3. **Randomizes Plex pre-roll intros every hour** by conflict-free renaming: restores the current `intro.mp4` safely to its original name and promotes a new random video to `intro.mp4`.
4. **Supports both scheduling options**: run as a self-contained **systemd background service** or execute individual tasks via **cron**.

---

## How It Works with Plex

```
Google Photos Shared Album (https://photos.app.goo.gl/YOUR_ALBUM_KEY)
                         │
                         ▼ (Daily One-Way Sync via '=dv' download)
┌────────────────────────────────────────────────────────────────────────┐
│  Target Video Directory (e.g. /home/croc/videos/plex_intros)           │
│                                                                        │
│  - clip_1.mp4                                                          │
│  - clip_2.mp4                                                          │
│  - clip_3.mp4                                                          │
│  - intro.mp4  <────── (Hourly rotation: swaps a random clip to intro) │
│  - .intro_state.json (Tracks original filenames & avoids duplicates)   │
│  - .download_manifest.json (Tracks synced Google Photos media)         │
└────────────────────────────────────────────────────────────────────────┘
                         │
                         ▼ (Plex Server reads /path/to/intros/intro.mp4)
                   Plex Clients
```

### Why Renaming Works
Plex Media Server does **not** copy or cache pre-roll videos internally. When a movie starts, Plex opens and streams the file located at the configured file path (`/path/to/intros/intro.mp4`) directly off the disk.

By normalizing all clips to standard H.264/AAC `.mp4` on download, every clip Direct-Plays seamlessly across all Plex clients without needing server transcoding.

---

## Prerequisites (Linux Mint)

Python 3 and `ffmpeg` are required. On Linux Mint, install them via apt if they are not already installed:

```bash
sudo apt update
sudo apt install -y python3 ffmpeg
```

The script uses standard library modules (no third-party pip packages required).

---

## Installation & Quickstart

### 1. Clone or Download the Repository
```bash
git clone https://github.com/your-username/plex-intro-randomizer.git /home/croc/code/plex-intro-randomizer
cd /home/croc/code/plex-intro-randomizer
```

### 2. Configure Settings
Copy `config.example.json` to `config.json`:

```bash
cp config.example.json config.json
```

Edit `config.json` with your desired directories and intervals:

```json
{
  "video_directory": "/home/croc/videos/plex_intros",
  "album_url": "https://photos.app.goo.gl/YOUR_ALBUM_KEY",
  "target_intro_name": "intro.mp4",
  "sync_interval_hours": 24,
  "rotate_interval_minutes": 60,
  "transcode_to_mp4": true
}
```

* `video_directory`: The directory where intro videos are downloaded, converted, and stored.
* `album_url`: The Google Photos shared album link.
* `target_intro_name`: The active intro filename that Plex points to (`intro.mp4`).
* `sync_interval_hours`: How often to check for newly added videos in Google Photos (default: `24`).
* `rotate_interval_minutes`: How often to rotate `intro.mp4` (default: `60`).
* `transcode_to_mp4`: Automatically convert incoming `.MOV`, `.wmv`, and other non-mp4 videos to `.mp4` via `ffmpeg` (default: `true`).

### 3. Configure Plex Media Server
1. Open **Plex Web App**.
2. Go to **Settings** (wrench icon top right) → **Server** → **Extras**.
3. Click **Show Advanced** (top right of settings pane).
4. Under **Movie pre-roll video**, enter the absolute path to your `intro.mp4`:
   ```text
   /home/croc/videos/plex_intros/intro.mp4
   ```
5. Click **Save Changes**.
6. *(Optional)* Make sure your movie library has Cinema Trailers enabled: **Settings** → **Manage** → **Libraries** → Edit Movie Library → **Advanced** → check **Enable Cinema Trailers**.

---

## Command Line Usage

You can test or run individual operations anytime using the CLI:

### Perform a Test Run (Dry Run)
Simulate synchronization and rotation without downloading files or changing file names:
```bash
python3 plex_intro_randomizer.py -c config.json --dry-run sync
python3 plex_intro_randomizer.py -c config.json --dry-run rotate
```

### One-Way Sync from Google Photos
Download all new videos from the album (transcoding any non-MP4s):
```bash
python3 plex_intro_randomizer.py -c config.json sync
```

### Rotate Intro Video Once
Safely restores the active `intro.mp4` to its original name and selects a new random video:
```bash
python3 plex_intro_randomizer.py -c config.json rotate
```

### Check Status
Display active intro, total candidate videos, and rotation history:
```bash
python3 plex_intro_randomizer.py -c config.json status
```

---

## Running as a Background Service (Option A - Recommended)

To run the script continuously as a systemd service on Linux Mint:

### 1. Copy the Systemd Unit File
You can install it as a **system service** or a **systemd user service**. Installing as a systemd user service runs without root privileges:

```bash
mkdir -p ~/.config/systemd/user
cp plex-intro-randomizer.service ~/.config/systemd/user/
```

### 2. Verify File Paths in the Service File
Open `~/.config/systemd/user/plex-intro-randomizer.service` and confirm the paths match your user and setup:
```ini
WorkingDirectory=/home/croc/code/plex-intro-randomizer
ExecStart=/usr/bin/python3 /home/croc/code/plex-intro-randomizer/plex_intro_randomizer.py -c /home/croc/code/plex-intro-randomizer/config.json daemon
```

### 3. Enable and Start the Service
```bash
systemctl --user daemon-reload
systemctl --user enable plex-intro-randomizer.service
systemctl --user start plex-intro-randomizer.service
```

### 4. Enable User Services to Run on Boot (Linger)
To keep the service running even when you are logged out:
```bash
loginctl enable-linger $USER
```

### 5. Check Service Status and Logs
```bash
# Check status
systemctl --user status plex-intro-randomizer.service

# Stream live logs
journalctl --user -u plex-intro-randomizer.service -f
```

---

## Running via Cron (Option B)

If you prefer using cron instead of a background daemon:

1. Open your user crontab:
   ```bash
   crontab -e
   ```

2. Add the following entries (daily sync at 3:00 AM, hourly rotation at the top of every hour):
   ```cron
   # Rotate Plex intro every hour
   0 * * * * /usr/bin/python3 /home/croc/code/plex-intro-randomizer/plex_intro_randomizer.py -c /home/croc/code/plex-intro-randomizer/config.json rotate >> /home/croc/code/plex-intro-randomizer/rotate.log 2>&1

   # One-way sync Google Photos album once a day at 3:00 AM
   0 3 * * * /usr/bin/python3 /home/croc/code/plex-intro-randomizer/plex_intro_randomizer.py -c /home/croc/code/plex-intro-randomizer/config.json sync >> /home/croc/code/plex-intro-randomizer/sync.log 2>&1
   ```

---

## Conflict-Free Safety Mechanics

The script is specifically designed to protect your media from being lost, overwritten, or locked:

1. **Original Name Memory**: When a clip (e.g. `1127781.mp4`) is picked to become `intro.mp4`, its original name is recorded in `.intro_state.json`.
2. **Safe Restoration**: On the next rotation, `intro.mp4` is renamed back to `1127781.mp4`.
3. **Collision Avoidance**: If another file named `1127781.mp4` already exists in the directory, the active `intro.mp4` is renamed to `1127781_backup_<timestamp>.mp4` so that **no files are ever overwritten**.
4. **Untracked File Protection**: If an `intro.mp4` existed before the script was ever run, it is automatically backed up as `intro_backup_<timestamp>.mp4`.
5. **Deduplication**: `.download_manifest.json` tracks unique Google Photos media IDs, preventing re-downloading videos that are already in your directory.
6. **Zero Playback Interruption**: Linux file semantics allow in-progress video streams to finish playing smoothly even while the file is being renamed for the next movie.

---

## Running Tests

To run the automated unit and integration tests:

```bash
python3 test_suite.py
```
