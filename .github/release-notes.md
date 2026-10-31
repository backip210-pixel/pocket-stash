## What's new in v0.3.2

- Dark mode is the default for the Android shell and setup page; use the appearance button to switch to light mode.
- Scans can generate video perceptual hashes by default. Use **Generate missing hashes** for videos scanned earlier; this does not overwrite existing hashes.
- Release signing and version values are checked before GitHub publishes the APK.

## Install

Download the `Pocket-Stash-v*.apk` file below and open it on your Android phone. GitHub builds and signs this APK; `SHA256SUMS.txt` can be used to check the download. Termux must also be installed and initialized—Pocket Stash is a companion, not a bundled Stash port. Follow the [setup guide](../../#first-time-android-setup).

The app and its Termux/Photos helpers have not yet been verified on the target phone. Start with a small test video and keep a separate backup of anything important.

Google Photos backup verification, performer-album creation and device-only deletion require your review and manual action. Pocket Stash does not verify cloud backup or automatically delete videos.

The first signed release uses a different key from earlier debug APKs. If Android refuses to update an earlier debug build, uninstall that build before installing this one. Termux and its Stash data are separate. Future release APKs signed with the same release key can update this one in place.
