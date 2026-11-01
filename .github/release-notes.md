## What's new in v0.3.3

- The Android setup screen reapplies its saved appearance after navigation and reload. The theme control now shows the current choice and lets you explicitly select Dark or Light.
- Pocket Stash prepares `Unsorted` and `Sorted` in every selected video library, including existing libraries when setup opens. Existing files and old library folders are left alone.
- After a review, scenes with a title and at least one performer are sorted into performer folders. The existing filename—including a rename made by Rename File on Update—is preserved. Multi-performer scenes get a separate byte-identical file for each performer. Additional copies are checked before the original moves and require extra local space.
- Scan-time pHashes and the separate missing-hash option remain available. Photos backup, album membership and device-only deletion remain manual; pHashes do not verify Google Photos backup or deduplication.

## Update and important checks

Install the signed `Pocket-Stash-v*.apk` over the previous **release** APK. After installation, finish any Stash tasks, then use **Help → Stop setup server** and **Start Pocket Stash** to replace an older Termux companion that is still running. This does not erase your Stash database or media.

Test first with a small, backed-up video. Android/Termux storage, theme and Photos helpers have not been verified on the target phone. On Android a newly copied filename may be visible while the copy is still in progress; wait for the batch to finish before backup or opening it. Keep other Stash jobs, edits, rename-plugin actions and file writers idle during sorting. An interrupted batch may need manual inspection; the app will not silently retry an uncertain operation. See the [workflow and recovery guide](../../#performer--google-photos-workflow).

In Google Photos, enable backup for the new performer folders and verify each intended item says **Backed up** before adding it to albums or using **Delete from device**. Exact copies may be grouped or deduplicated, but video deduplication is not guaranteed. Check any deletion prompt carefully; avoid regular Trash and broad Free up space.
