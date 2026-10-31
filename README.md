# Pocket Stash

An unofficial Android companion and local setup page for [Stash](https://github.com/stashapp/stash) on [Termux](https://github.com/termux/termux-app). Set up Stash, choose scan folders, and use a review-first workflow to stage identified videos in performer folders for Google Photos. Photos backup, album membership and device-only cleanup remain **manual checks**; see below.

**The APK is a real Android app, but it is not a standalone Stash port.** The Android app opens the local setup page and can ask Termux to run the companion; **Stash, Python and the server still run inside Termux**. The APK does not bundle Stash or your media. No root is required.

## Build the APK on GitHub

The project includes a Gradle wrapper and `.github/workflows/android.yml`. On every push or pull request, and when started manually, GitHub Actions runs the Python and Android unit tests and Android lint, builds a **debug-signed APK**, and uploads it as `Pocket-Stash-debug-APK` (kept for 14 days).

1. Extract the source ZIP **into an empty folder**. The extracted folder should contain this `README.md`, `server.py`, `android/`, and the hidden `.github/` folder. These files must be at your repository's root—not inside another project folder.
2. Add all files, **including `.github/` and `android/gradle/wrapper/gradle-wrapper.jar`**, to your GitHub repository and push. For a new, empty repository, for example:

   ```sh
   cd /path/to/the/extracted/folder
   git init
   git add .
   git commit -m "Add Pocket Stash Android companion"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
   git push -u origin main
   ```

   For an existing checkout, copy the project files into it instead and use your usual `git add`, `git commit`, and `git push`. Authenticate through your own GitHub CLI, Git credential manager, or SSH; **do not paste a token into a chat or commit one to the repository**. If you use GitHub's web upload, ensure the hidden `.github/workflows/android.yml` is actually uploaded.
3. On GitHub, open **Actions → Build Android APK** and choose the latest successful run. If needed, use **Run workflow**. Download the `Pocket-Stash-debug-APK` artifact, unzip it, and transfer `app-debug.apk` to your phone. You may need to sign in to download artifacts and allow your browser/file manager to install unknown apps.

The artifact is **for testing**, not a production-signed release. Each clean GitHub runner normally creates a different debug signing key. Android will reject an update signed by a different key; uninstall the old APK before installing the new one. This does **not** remove Termux or its Stash data. For stable upgrades/distribution, create and safeguard your own release signing key and configure signing separately; this repository does not contain a release key or a signed production build.

### Build locally

Install Java 17 and the Android SDK platform 35 and build tools 35.0.0, and set `ANDROID_HOME` to the SDK directory. The wrapper downloads the checksum-pinned Gradle 8.9 distribution; no separate Gradle installation is needed.

```sh
python3 -m unittest discover -s tests -v
cd android
./gradlew testDebugUnitTest assembleDebug lintDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

Windows: use `gradlew.bat`. The Gradle build creates the companion ZIP from this repository's `server.py`, `performer_workflow.py` and `web/` files and embeds it in the APK; there is no separately maintained prebuilt payload. `assembleRelease` does not supply a release signing key.

## What you need on your phone

- Android **8.0 / API 26 or newer**, an up-to-date Android System WebView, and the APK built above.
- An **APT-based Termux**, version 0.109 or newer; use the [F-Droid build](https://f-droid.org/packages/com.termux/) or an official GitHub **APT** APK. Open Termux once and wait for its initial setup. The experimental pacman build is not supported by the installer.
- Internet access for the initial Termux packages, and enough storage for Stash, FFmpeg, the database and generated media.
- Permission for Termux to read the folders you choose. Android can restrict app-private directories, some SD cards and Storage Access Framework-only locations.

The setup page installs Stash from the community-maintained [Termux User Repository (TUR)](https://github.com/termux-user-repository/tur), plus Termux's `ffmpeg` package. TUR is third-party software, not an official Stash or Termux package; its available version and supported CPU architectures can change. On an unsupported architecture, installation may fail rather than falling back to an incompatible Linux binary.

## First-time Android setup

1. Install and open Termux first, then install and open the Pocket Stash APK.
2. In the app, tap **Copy Termux setup command**, open Termux, paste and run it, and approve the storage permission prompt. This enables Termux's `allow-external-apps` setting and runs `termux-setup-storage`. You can inspect the exact command below.
3. Back in Pocket Stash, tap **Allow Termux control** and grant the permission. On some devices it is in Android Settings → Apps → Pocket Stash → Permissions → **Additional permissions** → **Run commands in Termux environment**.
4. Tap **Start Pocket Stash**. The app sends its embedded companion to Termux using Termux's documented `RUN_COMMAND` interface. It installs Python from Termux if necessary and starts the local setup server. Initial startup may take several minutes and needs internet; check Termux notifications if it fails.
5. When the setup page opens, tap **Install**. This enables TUR and installs Stash and FFmpeg. Then choose one or more readable folders, tap **Save & scan**, and choose **Open Stash**. The native toolbar also switches between **Setup** and **Stash**.

Next time, open Pocket Stash: it connects if the setup server is already running, or tap **Start Pocket Stash**. The companion starts an installed Stash if port 9999 is free. Termux must remain running; Android may stop background processes. It is not a guaranteed always-on service or boot launcher.

The copied preparation command is:

```sh
mkdir -p "$HOME/.termux" && touch "$HOME/.termux/termux.properties" && sed -i '/^[[:space:]]*allow-external-apps[[:space:]]*=/d' "$HOME/.termux/termux.properties" && printf '\nallow-external-apps=true\n' >> "$HOME/.termux/termux.properties" && termux-reload-settings && termux-setup-storage
```

**Security:** granting Termux control permits this app to run commands with Termux's access, including Termux files and any shared storage Termux can read. Enabling `allow-external-apps` also permits other apps that you separately grant the Termux command permission. Only grant that permission to software you trust. To revoke it, remove Pocket Stash's Termux command permission in Android settings. To disable external Termux commands for **all** apps, set `allow-external-apps=false` in `~/.termux/termux.properties` and run `termux-reload-settings`. You can use the manual option below without enabling Termux control at all.

### Start and stop

In the setup page, open **Help → Stop setup server** to stop the companion and the Stash process it started; other Termux sessions and an independently started Stash are not stopped. Active scans will stop. Wait for package installation to finish first. In manual mode, `Ctrl+C` in the terminal running the companion also stops it and its managed Stash.

The APK keeps its embedded companion files under `~/.pocket-stash-android/versions/` in Termux; it does not overwrite `~/pocket-stash` or Stash's database. If a previous companion is already running, the APK connects to it rather than replacing it. Stop the old setup server and start again to run the version embedded in a newer APK. Stash's configuration and media are not deleted.

## Manual Termux mode (no external-app permission)

You can use the Android app as a viewer without giving it Termux command permission, or use a normal browser instead. Extract the source ZIP into a new folder in Termux and run `install.sh`. For example, if the **GitHub-ready ZIP from this project** is in Downloads and is named `pocket-stash-performer-github-ready.zip`:

```sh
termux-setup-storage
pkg install -y unzip
mkdir -p "$HOME/pocket-stash-source"
unzip -o "$HOME/storage/downloads/pocket-stash-performer-github-ready.zip" -d "$HOME/pocket-stash-source"
bash "$HOME/pocket-stash-source/install.sh"
```

GitHub's own **Download ZIP** adds a repository-and-branch directory inside the archive: adjust the last path to point to that extracted directory's `install.sh`. The installer installs Python if needed, creates a `pocket-stash` launcher and starts the server. If the Android app is installed, tap **Already running? Check connection**. Otherwise open **http://127.0.0.1:8765/** in a browser on the phone. Next time, run `pocket-stash` in Termux. An earlier Termux-only `pocket-stash.zip` has its own original extraction path and instructions.

## Performer → Google Photos workflow

**Read before using:** the workflow moves your original files. Make an independent backup of any media you cannot replace, and start with a small, non-sensitive test video. The automatic Stash database backup below is **not** a backup of your videos, and does not include all Stash blobs/generated assets. The app never automatically deletes videos, uploads through the Google Photos API, or claims that a Google Photos item is backed up or belongs to an album.

1. **Prepare Stash.** In the setup page add your source folder and the intended staging destination (or a parent of both) as Stash **video** libraries. Stash only moves files into configured libraries. A convenient arrangement is to add `Movies` as one library, put incoming videos in `Movies/Incoming`, and create `Movies/Pocket Stash Photos` as the destination. The destination picker can create that empty folder inside an already configured library. Choose a folder on the phone's shared storage, not Termux's private home. Run **Scan now**, open Stash, and identify/edit each scene's title and performer(s). Wait for scans and other Stash jobs to finish. Nothing moves while you are preparing metadata.
2. **Preview and select.** In **Performers → Photos**, choose the source and destination and tap **Preview changes**. The dry run reads Stash and the filesystem but does not rename or move files. Review every `From` and `To`; uncheck videos you want excluded. Choose **one** performer for a scene with several performers. Scenes without performers, ambiguous shared file IDs, inaccessible files, existing destinations, conflicting performer names, nonempty performer folders not tracked by this workflow, unsupported video types, videos larger than 10,000,000,000 bytes and cross-volume moves are skipped. A `title [file ID].ext` name goes under the sanitized performer folder; no duplicate video is created for a second performer. A missing title falls back to the original filename. A `.nomedia` in the destination path is rejected. Subtitle files and other sidecars stay at their original locations and must be handled separately; Stash may stop linking them after a later scan.
3. **Enable and approve.** Check **Enable input-file renaming** and the acknowledgement after reviewing the plan, then confirm the move. This is Pocket Stash's opt-in for this batch; no separate Stash renaming plugin is required. The app checks Stash jobs, asks Stash to make a database-only backup, rechecks metadata, paths, size and file timestamps, records each attempt in a private journal, and calls Stash's `moveFiles` one file at a time. On failure or an ambiguous response it stops rather than silently retrying. Up to 500 files may be previewed in a batch, 3,000 matching scenes may be queried, and the journal holds at most 3,000 file entries or 8 MB. Separate source folders into smaller batches if necessary.
4. **Back up through Google Photos.** Open Google Photos, select your intended account and backup quality, and turn on backup for **each new performer subfolder** (or explicitly choose backup for all device folders). Pocket Stash's optional Android **Help Photos discover videos** button requests video-read permission and asks Android's media scanner to index up to the 500 most recent locally present workflow videos; it does not initiate or verify an upload. The individual **Open video** button attempts to open that file in Google Photos. On Android 14+, selected-only access can limit the helper to videos you selected; change the app's video permission in Android Settings if needed. Both Android helpers are best-effort and **not yet tested on the target phone**; if they fail, find the folders/videos manually in Photos. Allow time and network for backup.
5. **Manually verify every video and album.** In Photos, open each video and check Details/Info says **Backed up** in the correct account. Check it plays correctly, including duration and any captions important to you. Create a Photos album named for that video's performer and add the video to it; device folders do not automatically become cloud albums. Check the video in the album, then use the two checkboxes in Pocket Stash to record **your own** checks. They are not independent verification by the app. Photos' account, backup settings and any device-specific unlimited-storage entitlement are controlled by Google and your ROM; test a small video first and verify the actual account/quality/storage behavior. Storage Saver may reduce quality or lose captions.
6. **Remove only a reviewed local copy.** After both checks, open that exact video in Google Photos and choose the current **Delete from device** / **Remove from device** action, if available. Do this for each workflow video you intend to remove. Do not use regular Photos Trash, which can remove the cloud copy, or the broad **Free up space** feature, which can affect unrelated media. If the device-only option is unavailable or the backup status is unclear, **do not delete**. Recheck the cloud copy and album after cleanup. No deletion is performed by Pocket Stash.

**After local removal:** Stash's database still points at local paths, so the videos will not play in Stash until downloaded again. Do not run Stash's **Clean** operation blindly: cleaning missing entries can remove metadata. Preserve/export Stash's database and associated assets if you need them later. The journal and your review checkboxes remain in `~/.pocket-stash/performer-workflow.json`; keep a private copy and don't treat it as a media backup.

**Failures and concurrency:** keep other Stash clients, scans, plugins and file writers idle during the move. Stash checks whether a destination exists before moving, but its check and rename are not atomic; another writer can race it. Even on a same-volume move, Stash may fall back to copying then removing the source, and an unsuccessful copy can leave a partial destination. The app checks paths, timestamps and size, not file hashes. If a journal entry is **uncertain**, inspect both paths and the Stash file record before taking action; the UI's resolution buttons only update the journal after checking that Stash and the filesystem match your inspected choice. If both copies or a partial destination exist, resolve that situation manually without deleting the only good copy, then recheck. New batches stay blocked while an uncertain entry remains. If the journal is unreadable, back it up before repairing anything.

**Why not automatic albums/deletion?** Google Photos' public Library API cannot manage items backed up separately by the Photos app. A separate API upload could consume storage or make duplicates and would not necessarily preserve a device-specific entitlement. Photos' UI and selectors vary by app version; this project deliberately does not use unverified accessibility taps, broad root access or automated deletion. The Android helper launches Photos and requests media indexing only.

## Folders and existing libraries

- The picker is a **file-path browser**, not Android's native folder picker. It shows readable shared-storage and removable-storage roots and Termux's private home. It does not scan media until you explicitly start a scan.
- The managed Stash uses `~/.stash/config.yml` and its existing database. Back up that directory before changing an important library. Stash's own startup may update its configuration as usual.
- Saving replaces Stash's configured folder list, preserving the video/image choices shown. Removing a folder from the list does not delete its files. A stale-list check warns if the list changed in Stash while you were editing.
- If another Stash is already running on port `9999`, the companion can connect to it but will not stop it. Saving folders still changes that instance after you explicitly press Save. Its network exposure and config location are governed by that instance.
- If an existing instance requires authentication, add its API key under **Connection settings**. The key is saved in `~/.pocket-stash/settings.json` with owner-only permissions. Leave the field blank to remove it.
- If Stash requests a database migration, open Stash and follow its migration flow; the companion will not migrate automatically.

## Privacy and networking

The companion listens on `127.0.0.1:8765`. A Stash process it starts is forced to listen on `127.0.0.1:9999`, regardless of the host setting in its config. Both are HTTP services intended for **the same phone only**; other apps on that phone may still reach local services. The companion uses a request token and host/origin checks, but is not a multi-user authentication system. It does not open a LAN or internet-facing port.

The Android WebView allows the two local origins, blocks direct remote subresource requests and pop-up windows, and can open user-initiated ordinary HTTPS navigation in the external browser. This is defense in depth, **not a network firewall**: WebView redirect handling may bypass request interception, and Stash itself can still access the internet. Some Stash features or plugins that need external assets, pop-ups, special file access, or fullscreen handling may not work in the embedded view. If needed, open **http://127.0.0.1:9999/** in your phone's full browser while Termux is running.

Installing packages requires network access. Stash itself may use the internet for its own update checks, scrapers, plugins, or other optional features. The companion does not upload your media. Its settings and size-limited server log are in `~/.pocket-stash/`; Stash's own data remains in `~/.stash/` by default.

## Troubleshooting

- **The app waits for Termux:** open Termux once to finish its own initialization. Check that `allow-external-apps=true` is set, that Pocket Stash has the Termux command permission, and that Termux notifications are enabled. Check the error shown in the app or Termux notifications. If necessary use manual mode.
- **Storage unavailable:** run `termux-setup-storage` in Termux, approve the prompt, and check Android Settings → Apps → Termux → Permissions. On some versions, enable “All files access” as well. The picker only shows paths Termux can read.
- **Installation failed:** check **View activity & logs**, the internet connection, free space, and Termux mirrors (`termux-change-repo`). Retry. Package availability depends on your device architecture and TUR.
- **Stash won't start or a port is in use:** check the activity log. Another service might already own port `9999`; the companion won't kill a process it did not start. If `8765` is already occupied by another program, stop that program or use a different setup.
- **Can't read an existing library:** add the Stash API key under **Connection settings** if authentication is enabled. If migration is required, open Stash itself.
- **Termux or scans stop in the background:** Android battery and background-process policies vary. Keep Termux open and check its battery settings; disabling battery optimization may help but is not a guarantee.
- **Update won't install:** debug APKs from different GitHub runs may have different signing keys. Uninstall the prior APK, then install the new one. Termux and Stash data remain separate.
- **Start over without deleting media:** stop the setup server, uninstall the Android APK, and optionally remove the companion files in `~/.pocket-stash-android/versions/` and the manual launcher `"$PREFIX/bin/pocket-stash"` and extracted source folder. `~/.pocket-stash/` contains companion settings/logs; `~/.stash/` contains Stash configuration and database. Do not remove these unless you intend to lose that data. Removing the companion does not uninstall Stash or FFmpeg.

## Development and verification

The companion uses Python's standard library; no Python packages or frontend build are needed. A safe interactive simulation is available:

```sh
python3 -m unittest discover -s tests -v
python3 server.py --demo --host 0.0.0.0 --port 8765
```

`--demo` uses sample folders and never installs packages, connects to a real Stash, or modifies real libraries. Only demo mode permits non-loopback binding. Setup, scanning, database backup and performer-file moves have been exercised with synthetic videos against upstream Stash **v0.31.1 on Linux**; automated tests also cover skipped files, consent, interrupted/uncertain moves and journal recovery. Actual Android/Termux package installation, permission prompts, the background bridge, MediaStore/Photos behavior and device-specific backup entitlement still require testing on a real phone.

## References and licences

- [Stash](https://github.com/stashapp/stash) and [Stash documentation](https://docs.stashapp.cc)
- [Termux](https://github.com/termux/termux-app) and [RUN_COMMAND documentation](https://github.com/termux/termux-app/wiki/RUN_COMMAND-Intent)
- [TUR](https://github.com/termux-user-repository/tur) and its [Stash package recipe](https://github.com/termux-user-repository/tur/blob/master/tur/stash/build.sh)
- Google Photos: [device-folder backup](https://support.google.com/photos/answer/6193313), [video backup limits](https://support.google.com/photos/answer/6174018), [backup quality](https://support.google.com/photos/answer/6220791), and [API album restrictions](https://developers.google.com/photos/library/reference/rest/v1/albums/batchAddMediaItems)

Pocket Stash is an independent project and is not affiliated with Stash, Termux or TUR. This companion is MIT-licensed; Stash is licensed separately under AGPL-3.0. The included official Gradle wrapper is provided by Gradle under the Apache License 2.0.
