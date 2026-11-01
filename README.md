# Pocket Stash

An unofficial Android companion and local setup page for [Stash](https://github.com/stashapp/stash) on [Termux](https://github.com/termux/termux-app). Set up Stash, choose scan folders, and use a review-first workflow to stage identified videos in performer folders for Google Photos. Photos backup, album membership and device-only cleanup remain **manual checks**; see below.

**The APK is a real Android app, but it is not a standalone Stash port.** The Android app opens the local setup page and can ask Termux to run the companion; **Stash, Python and the server still run inside Termux**. The APK does not bundle Stash or your media. No root is required.

## Download and build the APK on GitHub

Open this repository's **Releases** page and download the signed `Pocket-Stash-v*.apk` from the latest release. Android may ask you to allow installation from your browser or file manager. The companion still needs Termux and Stash; see the first-time setup below.

Every push to `main` starts `.github/workflows/release.yml`. GitHub runs the Python tests, Android unit tests and lint, builds and verifies a **release-signed APK**, creates its version tag, and publishes the APK and `SHA256SUMS.txt` as a normal GitHub release. A push containing multiple commits is released together. GitHub's generated source archives are attached automatically.

The release version is tracked in `android/version.properties`. Before each update, increase both `VERSION_NAME` and `VERSION_CODE`, and update the companion `APP_VERSION` in `server.py` to match. GitHub checks them against previous release tags, verifies the built APK values, and creates a matching Git tag. It refuses to replace a published release or publish a lower version. The version properties are part of the tagged source, so local builds report the same version as GitHub builds.

The repository owner must retain the **same private release signing key** for future versions. GitHub stores it in encrypted Actions secrets; it is not committed to this repository. The owner should also keep an offline private backup of the keystore and password: if the key is lost, future APKs cannot update the installed app in place. Anyone with the key can impersonate app updates, so never upload it to a public repository or share it. A release APK signed with this key can update a previous release signed with the same key and a lower version code. The first release uses a different key from earlier debug builds; uninstall an older debug build first if Android rejects the update. Termux and Stash data are separate.

Every push or pull request also starts `.github/workflows/android.yml` for debug tests and lint. Debug builds are **not distributed** because their ephemeral signing keys would conflict with release updates. Install from GitHub Releases instead.

### Set up release signing for your own repository

The original repository's owner has configured release signing. If you fork the repository with its full history, create and privately back up your own keystore, then configure repository Actions secrets before pushing to `main`:

1. With a JDK installed, create a PKCS12 key with alias `pocket-stash`, for example `keytool -genkeypair -keystore pocket-stash-release.p12 -storetype PKCS12 -alias pocket-stash -keyalg RSA -keysize 3072 -validity 18250 -dname "CN=Pocket Stash"`. Use a strong password for the keystore and key. Keep the keystore and password private and backed up; never commit them.
2. In your GitHub repository's **Settings → Secrets and variables → Actions**, set `POCKET_STASH_RELEASE_KEYSTORE` to the single-line base64 encoding of the keystore, and `POCKET_STASH_RELEASE_PASSWORD` to its password. For example, `base64 -w0 pocket-stash-release.p12` prints the value on Linux. Do this only on a trusted device and do not paste the value into an issue, pull request or public terminal log.
3. For your own signing key, set `android/release-certificate.sha256` to the lowercase SHA-256 fingerprint of its public certificate (remove colons from `keytool -list -v -keystore pocket-stash-release.p12 -alias pocket-stash`). This fingerprint is public; the private keystore and password must remain secret. Keep the same key for future updates.
4. Increase `VERSION_NAME` and `VERSION_CODE` in `android/version.properties`, and set the same `APP_VERSION` in `server.py`, before pushing each update to `main`. GitHub builds, checks the certificate and version, and creates the matching tag and release. Missing/invalid secrets, a mismatched certificate or a reused version cause the release job to fail rather than publishing an incompatible APK.

The release job publishes a normal release and requires `contents: write` for its GitHub-provided token. GitHub disables some workflows for forks by default; enable Actions in the fork if necessary. If a run is interrupted while its release is still a draft, retrying the run can complete it; a published release is not overwritten.

### Build locally

Install Java 17 and the Android SDK platform 35 and build tools 35.0.0, and set `ANDROID_HOME` to the SDK directory. The wrapper downloads checksum-pinned Gradle 8.9; no separate Gradle installation is needed.

```sh
python3 -m unittest discover -s tests -v
cd android
./gradlew testDebugUnitTest assembleDebug lintDebug
# Debug APK: app/build/outputs/apk/debug/app-debug.apk
```

The regular Python suite skips the optional real-Stash tests. To exercise hash generation, reviewed sorting, real copies and duplicate rescanning against a local Stash v0.31.1 binary with FFmpeg installed, set `POCKET_STASH_TEST_STASH_BINARY=/path/to/stash` and rerun the suite. The test uses synthetic videos and a temporary Stash home, not your library.

Windows: use `gradlew.bat`. A local **release** build additionally needs `POCKET_STASH_KEYSTORE` set to the private keystore file path and `POCKET_STASH_SIGNING_PASSWORD` set to its password; then run `./gradlew assembleRelease`. To produce an installable update, set a higher `VERSION_CODE` and the matching `VERSION_NAME` in the tracked `android/version.properties` file. Without signing credentials, the release build fails. Avoid exposing the password in shell history or logs. The Gradle build creates the companion ZIP from this repository's `server.py`, `performer_workflow.py` and `web/` files and embeds it in the APK.

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

Updating the APK does not automatically replace a setup companion that is already running in Termux. If Pocket Stash warns about an older companion, first finish and verify any Stash scan, hash generation, or move. Use **Stop setup server** in the running setup page's help dialog, then tap **Start Pocket Stash** to start the new bundled version. A normal update does not erase your Stash database or settings.

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

## Appearance and perceptual hashes

Pocket Stash opens in **dark mode** by default. The Android appearance control shows the current theme; tap it and choose **Dark** or **Light**; changing the Android appearance reloads the page, so save unfinished selections first. In a browser, use the sun/moon button at the top of the setup page. Stash itself has its own appearance setting; the companion does not rewrite Stash's pages.

**Generate perceptual hashes on scan** is enabled by default in Library folders, and can be switched off there. Stash generates video pHashes when scanning new or changed videos; this can make scans slower and consume CPU/battery. For videos that were scanned earlier without pHashes, use **Generate missing hashes** after the scan finishes. This queues a Stash job for the configured video folders, with existing hashes left unchanged; it does not guarantee that every video can be processed or matched. Check Stash's Tasks/logs if a file is unavailable or FFmpeg fails. This option generates video pHashes, not image pHashes, and does not automatically identify or upload media.

## Performer → Google Photos workflow

**Read before using:** sorting moves your original video. Make a separate backup of media you cannot replace and start with one small test video. Stash's database backup is **not** a video backup. Pocket Stash does not upload, check Photos backup, create cloud albums, or delete your original videos. It can remove an unfinished copy that it created itself.

1. **Choose libraries.** In Pocket Stash, select and save every Stash **video** library. Inside each one, Pocket Stash creates `Unsorted` and `Sorted`, including for existing installations when the setup page opens. Saving a new library prepares its pair too. Existing files and old library locations are left untouched. Inaccessible, read-only, conflicting or symlinked folder pairs are reported; fix them and tap **Retry folder setup**. Choose primary shared phone storage when possible: Google Photos and the native helper may not see Termux-private or removable-storage paths.
2. **Identify and rename in Unsorted.** Put videos into that library's `Unsorted`, scan, then add a **title and at least one performer** in Stash. The workflow doesn't require Stash's Organized flag. The optional [Rename File on Update plugin](https://discourse.stashapp.cc/t/rename-file-on-update/2563) can rename on metadata update: leave its **Default directory path format** blank to keep each file in its current directory, and adjust its own “Rename unorganized scenes” setting as desired. Wait for the plugin and Stash jobs to finish. Editing metadata after sorting can trigger the plugin to rename the copies too; finish your edits first, as later renames make Pocket Stash's recorded review paths stale.
3. **Preview every destination.** In **Performers → Photos**, choose the video library and tap **Preview changes**. The dry run shows every `From` and performer `To`, the number of resulting files and the additional space needed; uncheck videos to exclude them. The exact existing filename, including the plugin's rename, is preserved. For example:

   ```text
   Movies/Unsorted/A video.mp4
     → Movies/Sorted/Alex/A video.mp4
     → Movies/Sorted/Jamie/A video.mp4
   ```

   Here Alex gets the original and Jamie gets a separate, byte-identical physical copy. For N performers there are N files in total and N−1 additional copies. Scenes without a title or performer, ambiguous file IDs, inaccessible files, existing destination filenames, colliding performer folders or filenames, unsupported types, files over 10,000,000,000 bytes, `.nomedia` destinations and cross-volume moves are skipped. Existing folders may contain other files; Pocket Stash never claims those files as its own. Sidecars such as subtitles stay behind and can lose their association after a Stash scan.
4. **Review and sort.** Check both consent boxes and confirm. Keep other Stash clients, metadata edits, scans and file operations idle. The app checks free space and Stash's job queue, creates a database-only backup, makes and verifies the additional copies with SHA-256 while leaving the original in place, checks the source again, and then asks Stash to move the original to one performer folder. All copies are actual separate files, not hard links. The hashing is an integrity check, **not** a Google Photos upload or backup check. Sorting may use substantial space, CPU and time. Up to 500 sources can be previewed, 3,000 matching scenes queried, and the private journal holds at most 3,000 destination items or 8 MB.
5. **Back up in Google Photos.** Wait until the entire batch finishes before opening or backing up newly created files. On Android shared storage, an exclusive-created file can be visible while still being written. Enable backup for each new performer subfolder (or choose all device folders) in Photos. The optional **Help Photos discover videos** button requests video-read permission and asks Android to index up to 500 recent local workflow videos; it does not initiate or verify backup. Both native helper buttons are best-effort and untested on the target phone. On Android 14+, selected-only permission may limit access. Find the files manually if needed.
6. **Verify and organise manually.** For every intended video, confirm Details/Info says **Backed up** in the correct Photos account and that it plays correctly. Create the matching performer albums and add each video to the appropriate albums; device folders do not create cloud albums. The journal's checkboxes record only **your** checks. Google Photos may group or deduplicate exact copies, but this is not guaranteed for these videos; Stash's pHashes do not prove Photos backup or deduplication. Device-specific unlimited-storage entitlement and quality are controlled by Google and the ROM; test them yourself. Storage Saver may reduce quality or lose captions.
7. **Remove only reviewed device copies.** After verifying the backup and album, use the exact video's **Delete from device** / **Remove from device** action in Photos if available. Check the prompt: Photos may group duplicates or offer to remove more than one local copy. Don't use ordinary Trash or broad **Free up space**, which can affect cloud copies or unrelated media. If status or the scope of deletion is unclear, do not delete. Check the cloud copy and albums again afterwards.

**After device-only removal:** Stash's database points at local paths, so absent videos will not play until downloaded again. Do not run Stash's **Clean** task blindly; preserve/export the Stash database and assets. The private journal is `~/.pocket-stash/performer-workflow.json`; keep a backup, but it is not a media backup. In a synthetic test with Stash 0.31.1, its rescan associated three identical physical files with one scene; other versions may behave differently.

**Failures and concurrency:** Stash's original-file move checks the destination but is not atomic against another writer; it can also fall back to copy/delete and leave a partial target. Never run another writer or rename plugin while sorting. For additional copies, Pocket Stash uses exclusive creation on Termux's Android storage (or atomic no-replace publication where supported), verifies the bytes and leaves the original in `Unsorted` until all copies are complete. If interrupted, a final-named copy or hidden temporary may be partial. Stop the companion and inspect the journal paths and Stash before taking action; never delete the only good file. The resolution buttons check the file states and, for a completed copy, compare hashes, but don't change or delete media. New batches are blocked while an entry is uncertain. A batch can stop after some verified copies while the original remains; inspect and reconcile those existing destination files manually before previewing again. If the journal cannot be read, back it up before repairing anything.

**Why not automatic albums/deletion?** Google Photos' public Library API cannot manage items backed up separately by the Photos app. A separate API upload could consume storage or create duplicates and would not necessarily preserve a device-specific entitlement. Photos' UI varies by app version; this project deliberately does not use unverified accessibility taps, broad root access or automated device cleanup.

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
- **Update won't install:** release APKs must have the same signing key as the installed app and a higher version code. Debug APKs, and the first release after a debug APK, may use different keys; in that case uninstall the prior APK before installing the new one. Termux and Stash data remain separate.
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
