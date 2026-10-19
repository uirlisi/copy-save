# copy-save

**Rolling backups for games that only keep one autosave.**

Some games (DOOM 2016, DOOM Eternal, Vampyr, …) only have checkpoint autosaves and only let you load the latest one. Make a bad call right before a checkpoint, and there's no going back.

This tool watches your games: while a game is running, it zips the game's save folder every time it changes, giving you a timeline of checkpoints. You can restore any of them in two clicks.

## Features

- **Automatic snapshots** while a game is running, at a cadence you choose (default: twice a minute). Unchanged saves are skipped, so there are no duplicates.
- **Two-click restore** of any snapshot. Your current save is backed up first, so a restore can always be undone.
- **Multiple games**, each with its own save folder, executable, and cadence. The list shows each game's own icon (fetched from the exe file).
- **Safe by design:** a snapshot is discarded if the game writes to the save mid-zip, and a restore swaps the folder in only once it's fully unpacked. If anything fails, your current save is left untouched.
- **Windows 11 look** with light and dark mode. It follows the Windows setting until you flip the **Dark mode** switch, after which your choice is remembered.
- **Plain zip files** stored next to your saves, so you can open, copy or restore them yourself without the app.

## Install

1. Download `copy-save-<version>-setup.exe` from the [Releases](../../releases) page.
2. Run it. Admin rights aren't needed: it installs for your user account by default.

> **"Windows protected your PC"?** The installer isn't code-signed (and likely won't ever be), so Windows SmartScreen warns about it. Click **More info > Run anyway**. 

The only requirement is 64-bit Windows 10 or 11. Python is bundled, so there's nothing else to install.

## Usage

1. Click **Add** and fill in:
   - **Save folder**: the folder holding the game's saves (see [Finding a game's save folder](#finding-a-games-save-folder)).
   - **Game executable**: the game's `.exe`. Snapshots (zips) are only taken while it's running.
   - **Saves per minute**: how often to check for a new save.
2. Click **Start monitoring** and play. The Activity log shows each snapshot as it's made.
3. To go back, **exit the game**, select it in the list, click **Restore…**, and double-click the snapshot you want. Then start the game and load the latest checkpoint.

The **Back up this game** switch pauses snapshots for one game without deleting its settings.

## How backups work

- Snapshots go into a `copy-save-zips` folder **next to the save folder**. For example, saves in `…\id Software\DOOM` are backed up to `…\id Software\copy-save-zips\`.
- Each zip is named after the save's last-modified time, e.g. `2026-10-09_18-26-24.zip`. A new zip is only made when that time changes.
- Several games can share one `copy-save-zips` folder (when their save folders have the same parent). Each zip records which save folder it holds, so every game's Restore window only lists its own snapshots. If two games' saves have the same timestamp, the second zip gets the save folder's name added, e.g., `2026-10-09_18-26-24_DOOMEternal.zip`.
- Each zip contains the save folder itself. To restore by hand, exit the game, rename the current save folder, and extract the zip into the parent folder.
- Old snapshots aren't deleted automatically. Use **Delete** in the Restore window, or remove zips from the folder yourself.

## Tips

**Finding a game's save folder.** Common places:
- `%USERPROFILE%\Saved Games\`
- `%USERPROFILE%\Documents\My Games\`
- `%LOCALAPPDATA%\` and `%APPDATA%\` (in a folder named after the game or publisher)
- `<Steam folder>\userdata\<your id>\<game id>\`

[PCGamingWiki](https://www.pcgamingwiki.com) lists the exact save location for most games under *Game data > Save game data location*.

**Steam Cloud / GOG Galaxy cloud saves.** After a restore, the launcher may report a sync conflict next time you start the game. Choose the **local** files to keep the restored save.

**Restore with the game closed.** Games keep progress in memory and may overwrite a restored save, or lock its files. This tool warns you if the game is still running.

## Where settings are stored

Settings live in `%APPDATA%\copy-save\config.json`. Uninstalling copy-save leaves both the settings and your backup zips in place.

## Running from source

```
pip install -r requirements.txt
pythonw copy_save.pyw
```

The app also runs with only Python installed: without `sv-ttk`/`darkdetect` it falls back to the classic look, and without `psutil` it uses `tasklist` to detect games.

The first line of `copy_save.pyw` names the Python to use (`#!C:\Python313\pythonw.exe`). Adjust it to your Python install; it keeps double-clicking the script from starting the Microsoft Store Python, which would show Python's icon in the taskbar.

## Building the installer

Requirements: Python 3.13 and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```
pip install -r requirements.txt pyinstaller
python build.py
```

This produces `dist\copy-save\copy-save.exe` and the installer `dist\copy-save-<version>-setup.exe`. Use `python build.py --no-installer` to skip the installer step.

**Releasing:** bump `__version__` in `copy_save.pyw`, commit, then tag and push:

```
git tag v1.0.0
git push origin v1.0.0
```

The GitHub Actions workflow builds the installer and publishes a release with it attached. The tag must match `__version__`, or the build stops.

`make_icon.py` regenerates `copy_save.ico` if you want to change the icon.
