package app.pocketstash.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

public final class MainActivity extends Activity {
    private static final String TERMUX = "com.termux";
    private static final String TERMUX_PERMISSION = "com.termux.permission.RUN_COMMAND";
    private static final String HOME_URL = "http://127.0.0.1:8765/";
    private static final String STASH_URL = "http://127.0.0.1:9999/";
    private static final String PHOTOS = "com.google.android.apps.photos";
    private static final int PERMISSION_REQUEST = 120;
    private static final int FILE_REQUEST = 121;
    private static final int VIDEO_PERMISSION_REQUEST = 122;
    private static final long START_TIMEOUT_MS = 5 * 60_000L;

    // Narrowly updates one Termux property while preserving other preferences.
    private static final String PREPARE_COMMAND =
            "mkdir -p \"$HOME/.termux\" && "
            + "touch \"$HOME/.termux/termux.properties\" && "
            + "sed -i '/^[[:space:]]*allow-external-apps[[:space:]]*=/d' \"$HOME/.termux/termux.properties\" && "
            + "printf '\\nallow-external-apps=true\\n' >> \"$HOME/.termux/termux.properties\" && "
            + "termux-reload-settings && termux-setup-storage";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private View onboarding;
    private View webContainer;
    private TextView status;
    private Button startButton;
    private WebView web;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> uploadCallback;
    private String pendingPhotosAction;
    private String pendingPhotosId;
    private boolean probing;
    private boolean pendingStart;
    private boolean starting;
    private long startedAt;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_main);
        onboarding = findViewById(R.id.onboarding);
        webContainer = findViewById(R.id.web_container);
        status = findViewById(R.id.status);
        startButton = findViewById(R.id.start);
        web = findViewById(R.id.webview);
        prefs = getSharedPreferences(TermuxResultReceiver.PREFS, MODE_PRIVATE);

        configureWebView();
        findViewById(R.id.open_termux).setOnClickListener(v -> openTermux());
        findViewById(R.id.copy_command).setOnClickListener(v -> copySetupCommand());
        findViewById(R.id.grant_permission).setOnClickListener(v -> explainAndRequestPermission());
        startButton.setOnClickListener(v -> probe(true));
        findViewById(R.id.check).setOnClickListener(v -> probe(false));
        findViewById(R.id.nav_setup).setOnClickListener(v -> web.loadUrl(HOME_URL));
        findViewById(R.id.nav_stash).setOnClickListener(v -> web.loadUrl(STASH_URL));
        findViewById(R.id.nav_help).setOnClickListener(v -> {
            webContainer.setVisibility(View.GONE);
            onboarding.setVisibility(View.VISIBLE);
            status.setText("The local setup was connected. Use Check connection to return to it.");
        });

        long prior = prefs.getLong("started_at", 0L);
        if (prefs.getBoolean("starting", false) && !prefs.getBoolean("finished", false)
                && prior > 0 && System.currentTimeMillis() - prior < START_TIMEOUT_MS) {
            startedAt = prior;
            setStarting(true);
        }
    }

    @Override protected void onResume() {
        super.onResume();
        probe(false);
    }

    private void configureWebView() {
        WebView.setWebContentsDebuggingEnabled(false);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        // Required only for content URIs that the user selects in Android's picker.
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        // Block popups/new windows. The setup page navigates the same WebView on Android.
        settings.setSupportMultipleWindows(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setUserAgentString(settings.getUserAgentString() + " PocketStashAndroid/0.2");

        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                                       FileChooserParams params) {
                if (!LocalUrls.isAllowed(view.getUrl())) return false;
                if (uploadCallback != null) uploadCallback.onReceiveValue(null);
                uploadCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_REQUEST);
                    return true;
                } catch (RuntimeException e) {
                    uploadCallback = null;
                    callback.onReceiveValue(null);
                    toast("No compatible file picker is available.");
                    return false;
                }
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String address = request.getUrl().toString();
                if (LocalUrls.isAllowed(address)) return false;
                if (request.isForMainFrame() && request.hasGesture() && PhotosLink.isCompanionPage(view.getUrl())
                        && handlePhotosLink(request.getUrl())) return true;
                if (request.isForMainFrame() && request.hasGesture() && LocalUrls.isExternalHttps(address)) {
                    openExternal(address);
                }
                return true;
            }

            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                String address = request.getUrl().toString();
                if (LocalUrls.isAllowed(address)) return null;
                // Permit embedded image/font data but not arbitrary data: documents or scripts.
                if (!request.isForMainFrame() && (address.startsWith("data:image/")
                        || address.startsWith("data:font/"))) return null;
                // Block direct remote requests and arbitrary local URIs. WebView may
                // follow redirects without invoking this callback, so this is not a firewall.
                return new WebResourceResponse("text/plain", "utf-8", 403, "Blocked",
                        Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
            }

            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame() && request.getUrl().toString().startsWith(HOME_URL)) {
                    webContainer.setVisibility(View.GONE);
                    onboarding.setVisibility(View.VISIBLE);
                    status.setText("The local setup server is not responding. Start it in Termux or tap Start below.");
                }
            }
        });
    }

    // Non-destructive helper links are honoured only from a user gesture on the
    // Pocket Stash home page. The server resolves an opaque journal ID to a path;
    // a web page cannot pass an arbitrary path or invoke deletion or root.
    private boolean handlePhotosLink(Uri uri) {
        PhotosLink.Command command = PhotosLink.parse(uri.toString());
        if (command == null) return false;
        if ("app".equals(command.action)) launchPhotosApp();
        else requestPhotosAccess(command.action, command.itemId);
        return true;
    }

    private void launchPhotosApp() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(PHOTOS);
        if (launch == null) {
            toast("Google Photos was not found. Open it manually to review your videos.");
            return;
        }
        try { startActivity(launch); }
        catch (RuntimeException e) { toast("Open Google Photos manually to review your videos."); }
    }

    private boolean hasFullVideoAccess() {
        String permission = Build.VERSION.SDK_INT >= 33 ? android.Manifest.permission.READ_MEDIA_VIDEO
                : android.Manifest.permission.READ_EXTERNAL_STORAGE;
        return checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasSelectedVideoAccess() {
        return Build.VERSION.SDK_INT >= 34 && checkSelfPermission(android.Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED)
                == PackageManager.PERMISSION_GRANTED;
    }

    private void requestPhotosAccess(String action, String id) {
        if (hasFullVideoAccess() || hasSelectedVideoAccess()) {
            if (!hasFullVideoAccess()) toast("Only selected videos are available. Grant broader video access in Settings if needed.");
            runPhotosAction(action, id);
            return;
        }
        String[] permissions = Build.VERSION.SDK_INT >= 34
                ? new String[]{android.Manifest.permission.READ_MEDIA_VIDEO, android.Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED}
                : new String[]{Build.VERSION.SDK_INT >= 33 ? android.Manifest.permission.READ_MEDIA_VIDEO
                : android.Manifest.permission.READ_EXTERNAL_STORAGE};
        pendingPhotosAction = action;
        pendingPhotosId = id;
        new AlertDialog.Builder(this).setTitle("Help Google Photos find a video?")
                .setMessage("Pocket Stash asks Android for video access only to request media indexing and open the videos in Google Photos. It does not upload, verify backup, create albums, delete files or use root. You can skip this and do those steps in Photos manually. If you grant selected-only access, only the videos you select can be opened.")
                .setPositiveButton("Continue", (d, which) -> requestPermissions(permissions, VIDEO_PERMISSION_REQUEST))
                .setNegativeButton("Not now", (d, which) -> { pendingPhotosAction = null; pendingPhotosId = null; })
                .show();
    }

    @SuppressWarnings("deprecation")
    private String checkedSharedVideo(String path) {
        try {
            if (path == null || path.length() > 4096) return null;
            File root = android.os.Environment.getExternalStorageDirectory().getCanonicalFile();
            File video = new File(path).getCanonicalFile();
            if (!video.getPath().startsWith(root.getPath() + File.separator)
                    || !video.isFile() || !video.canRead()) return null;
            return video.getPath();
        } catch (IOException | SecurityException e) {
            return null;
        }
    }

    private void runPhotosAction(String action, String id) {
        network.execute(() -> {
            if ("open".equals(action) && id != null && id.matches("[a-f0-9]{24}")) {
                JSONObject result = getJson(HOME_URL + "api/workflow/item?id=" + id, 16_384);
                String path = result == null || result.optBoolean("demo", true) ? null
                        : checkedSharedVideo(result.optString("path", null));
                runOnUiThread(() -> {
                    if (path == null) { toast("Video unavailable. Open Google Photos and locate it manually."); return; }
                    scanAndOpen(path);
                });
            } else if ("scan".equals(action)) {
                JSONObject result = getJson(HOME_URL + "api/workflow/scan-list", 1_000_000);
                JSONArray list = result == null || result.optBoolean("demo", true) ? null : result.optJSONArray("paths");
                java.util.ArrayList<String> paths = new java.util.ArrayList<>();
                if (list != null) {
                    for (int i = 0; i < list.length() && i < 500; i++) {
                        String path = checkedSharedVideo(list.optString(i, null));
                        if (path != null) paths.add(path);
                    }
                }
                runOnUiThread(() -> {
                    if (paths.isEmpty()) { toast("No accessible workflow videos to index. Review the folders in Photos manually."); return; }
                    indexVideos(paths.toArray(new String[0]));
                });
            }
        });
    }

    private void scanAndOpen(String path) {
        toast("Asking Android to find the video. Backup is not verified.");
        try {
            MediaScannerConnection.scanFile(this, new String[]{path}, null, (scanned, contentUri) ->
                    runOnUiThread(() -> {
                        if (contentUri == null) {
                            toast("Android could not index this video. Open Google Photos and find it manually.");
                            return;
                        }
                        Intent view = new Intent(Intent.ACTION_VIEW).setDataAndType(contentUri, "video/*")
                                .setPackage(PHOTOS).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                        try { startActivity(view); }
                        catch (RuntimeException e) { toast("Google Photos could not open this video. Find it manually in Photos."); }
                    }));
        } catch (RuntimeException e) { toast("Media indexing failed. Find the video manually in Google Photos."); }
    }

    private void indexVideos(String[] paths) {
        toast("Asking Android to index " + paths.length + " workflow videos…");
        AtomicInteger remaining = new AtomicInteger(paths.length);
        AtomicInteger failed = new AtomicInteger();
        try {
            MediaScannerConnection.scanFile(this, paths, null, (scanned, uri) -> {
                if (uri == null) failed.incrementAndGet();
                if (remaining.decrementAndGet() == 0) runOnUiThread(() ->
                        toast("Android indexing finished (" + failed.get() + " unavailable). Check Photos backup for each folder and item."));
            });
        } catch (RuntimeException e) { toast("Media indexing failed. Enable folder backup in Photos manually."); }
    }

    private void openTermux() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(TERMUX);
        if (launch != null) {
            startActivity(launch);
        } else {
            new AlertDialog.Builder(this).setTitle("Install Termux first")
                    .setMessage("Get the APT build from F-Droid or Termux’s official GitHub releases. Open it once and wait for its initial installation before returning here.")
                    .setPositiveButton("Open F-Droid", (d, which) -> openExternal("https://f-droid.org/packages/com.termux/"))
                    .setNegativeButton("Not now", null).show();
        }
    }

    private void copySetupCommand() {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        clipboard.setPrimaryClip(ClipData.newPlainText("Pocket Stash Termux setup", PREPARE_COMMAND));
        new AlertDialog.Builder(this).setTitle("Command copied")
                .setMessage("Open Termux, paste and run the command, then approve storage access. Return here when it completes.\n\nThe command enables Termux’s allow-external-apps setting; only apps you separately authorise can then send Termux commands.")
                .setPositiveButton("Open Termux", (d, which) -> openTermux())
                .setNegativeButton("Later", null).show();
    }

    private void explainAndRequestPermission() {
        if (!termuxInstalled()) {
            openTermux();
            return;
        }
        if (checkSelfPermission(TERMUX_PERMISSION) == PackageManager.PERMISSION_GRANTED) {
            toast("Termux control is already allowed.");
            return;
        }
        new AlertDialog.Builder(this).setTitle("Allow control of Termux?")
                .setMessage("Pocket Stash can run commands with all of Termux’s access, including its files and any shared storage permission. The external-app setting also permits other apps that you separately authorise. Only grant this to apps you trust.\n\nManual mode does not need this permission.")
                .setPositiveButton("Continue", (d, which) -> requestPermissions(new String[]{TERMUX_PERMISSION}, PERMISSION_REQUEST))
                .setNegativeButton("Cancel", null).show();
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == VIDEO_PERMISSION_REQUEST) {
            if ((hasFullVideoAccess() || hasSelectedVideoAccess()) && pendingPhotosAction != null) {
                if (!hasFullVideoAccess()) toast("Only the videos you selected are accessible.");
                runPhotosAction(pendingPhotosAction, pendingPhotosId);
            } else {
                toast("Video access was not granted. Open Google Photos and review the folders manually.");
            }
            pendingPhotosAction = null;
            pendingPhotosId = null;
            return;
        }
        if (requestCode != PERMISSION_REQUEST) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            status.setText("Termux control is allowed. If you ran the setup command, tap Start Pocket Stash.");
        } else {
            new AlertDialog.Builder(this).setTitle("Permission not granted")
                    .setMessage("Android may require enabling this manually: Apps → Pocket Stash → Permissions → Additional permissions → Run commands in Termux environment. Names vary by device. You can also use manual mode instead.")
                    .setPositiveButton("Open app settings", (d, which) -> openAppSettings())
                    .setNegativeButton("Not now", null).show();
        }
    }

    private void openAppSettings() {
        Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()));
        try { startActivity(i); } catch (RuntimeException e) { toast("Open this app's permissions in Android Settings."); }
    }

    private boolean termuxInstalled() {
        try {
            getPackageManager().getPackageInfo(TERMUX, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    private void probe(boolean requestStartIfMissing) {
        if (requestStartIfMissing) pendingStart = true;
        if (probing || isFinishing() || isDestroyed()) return;
        probing = true;
        network.execute(() -> {
            boolean connected = isLocalCompanionRunning();
            runOnUiThread(() -> {
                if (isFinishing() || isDestroyed()) return;
                probing = false;
                boolean startIfMissing = pendingStart;
                pendingStart = false;
                if (connected) {
                    handler.removeCallbacksAndMessages(null);
                    setStarting(false);
                    prefs.edit().putBoolean("starting", false).apply();
                    showWeb();
                } else if (startIfMissing) {
                    launchTermuxCompanion();
                } else if (starting) {
                    showStartingStateOrError();
                } else {
                    String result = prefs.getBoolean("finished", false) ? prefs.getString("result", "") : "";
                    status.setText(result.isEmpty()
                            ? "Setup is not running yet. Follow the steps below, or start it manually in Termux."
                            : result);
                }
            });
        });
    }

    private boolean isLocalCompanionRunning() {
        JSONObject health = getJson(HOME_URL + "api/health", 8192);
        if (health != null && "Pocket Stash".equals(health.optString("name")) && !health.optBoolean("demo", true)) {
            return true;
        }
        // Also recognise the previously delivered Termux-only version (before /api/health existed).
        JSONObject old = getJson(HOME_URL + "api/status", 65536);
        return old != null && old.has("version") && old.optJSONObject("tools") != null
                && old.optJSONObject("server") != null && !old.optBoolean("demo", true);
    }

    private JSONObject getJson(String address, int maxBytes) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(address).openConnection();
            connection.setConnectTimeout(1200);
            connection.setReadTimeout(3000);
            connection.setInstanceFollowRedirects(false);
            if (connection.getResponseCode() != 200) return null;
            try (InputStream input = connection.getInputStream()) {
                return new JSONObject(new String(readBounded(input, maxBytes), StandardCharsets.UTF_8));
            }
        } catch (Exception e) {
            return null;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private void launchTermuxCompanion() {
        if (starting) {
            showStartingStateOrError();
            return;
        }
        if (!termuxInstalled()) {
            status.setText("Install Termux and open it once before starting Pocket Stash.");
            return;
        }
        if (checkSelfPermission(TERMUX_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            status.setText("Termux control is not yet allowed. Use step 3, or start the companion manually.");
            return;
        }
        try {
            byte[] archive;
            byte[] template;
            try (InputStream in = getAssets().open("companion.zip")) { archive = readBounded(in, 190_000); }
            try (InputStream in = getAssets().open("bootstrap.sh")) { template = readBounded(in, 30_000); }
            String script = BootstrapPayload.build(archive, new String(template, StandardCharsets.UTF_8));
            String id = UUID.randomUUID().toString();
            long now = System.currentTimeMillis();
            prefs.edit().putString(TermuxResultReceiver.ID, id).putLong("started_at", now)
                    .putBoolean("starting", true).putBoolean("finished", false).remove("result").apply();

            Intent callback = new Intent(this, TermuxResultReceiver.class).putExtra(TermuxResultReceiver.ID, id);
            int flags = PendingIntent.FLAG_ONE_SHOT;
            if (Build.VERSION.SDK_INT >= 31) flags |= PendingIntent.FLAG_MUTABLE;
            PendingIntent pending = PendingIntent.getBroadcast(this, id.hashCode(), callback, flags);

            Intent command = new Intent("com.termux.RUN_COMMAND");
            command.setClassName(TERMUX, "com.termux.app.RunCommandService");
            command.putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash");
            command.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{"-s"});
            command.putExtra("com.termux.RUN_COMMAND_STDIN", script);
            command.putExtra("com.termux.RUN_COMMAND_WORKDIR", "/data/data/com.termux/files/home");
            command.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
            command.putExtra("com.termux.RUN_COMMAND_COMMAND_LABEL", "Start Pocket Stash");
            command.putExtra("com.termux.RUN_COMMAND_COMMAND_DESCRIPTION", "Install Python if needed and run the local setup companion.");
            command.putExtra("com.termux.RUN_COMMAND_PENDING_INTENT", pending);
            // Must be called from this foreground Activity; Termux hosts the actual job.
            startForegroundService(command);
            startedAt = now;
            setStarting(true);
            showStartingStateOrError();
        } catch (Exception e) {
            prefs.edit().putBoolean("starting", false).apply();
            setStarting(false);
            status.setText("Couldn't request Termux startup: " + safeMessage(e)
                    + "\n\nCheck Termux and Pocket Stash permissions, or start manually.");
        }
    }

    private void showStartingStateOrError() {
        if (!starting) return;
        if (prefs.getBoolean("finished", false)) {
            setStarting(false);
            prefs.edit().putBoolean("starting", false).apply();
            status.setText(prefs.getString("result", "Termux stopped before the server started."));
            return;
        }
        long elapsed = Math.max(0, System.currentTimeMillis() - startedAt);
        if (elapsed >= START_TIMEOUT_MS) {
            setStarting(false);
            prefs.edit().putBoolean("starting", false).apply();
            status.setText("Still waiting after five minutes. Check Termux notifications, internet access and its allow-external-apps setting, then use Check connection. You can also start manually.");
            return;
        }
        status.setText("Starting in Termux… " + (elapsed / 1000) + "s\nThe first start may install Python. Keep an internet connection and check Termux notifications if this takes a while.");
        handler.removeCallbacksAndMessages(null);
        handler.postDelayed(() -> probe(false), 2500);
    }

    private void setStarting(boolean value) {
        starting = value;
        startButton.setEnabled(!value);
        startButton.setAlpha(value ? 0.55f : 1f);
    }

    private void showWeb() {
        boolean wasVisible = webContainer.getVisibility() == View.VISIBLE;
        onboarding.setVisibility(View.GONE);
        webContainer.setVisibility(View.VISIBLE);
        if (!wasVisible) web.loadUrl(HOME_URL);
    }

    private static byte[] readBounded(InputStream in, int limit) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int count;
        while ((count = in.read(buffer)) != -1) {
            if (out.size() + count > limit) throw new IOException("Embedded data is too large.");
            out.write(buffer, 0, count);
        }
        return out.toByteArray();
    }

    private static String safeMessage(Exception e) {
        String s = e.getMessage();
        return s == null || s.isEmpty() ? e.getClass().getSimpleName() : s.substring(0, Math.min(300, s.length()));
    }

    private void openExternal(String address) {
        if (!LocalUrls.isExternalHttps(address)) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(address))); }
        catch (RuntimeException e) { toast("No browser is available to open this link."); }
    }

    private void toast(String text) { Toast.makeText(this, text, Toast.LENGTH_LONG).show(); }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_REQUEST && uploadCallback != null) {
            uploadCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            uploadCallback = null;
        }
    }

    @Override public void onBackPressed() {
        if (webContainer.getVisibility() == View.VISIBLE && web.canGoBack()) web.goBack();
        else if (webContainer.getVisibility() == View.VISIBLE) {
            webContainer.setVisibility(View.GONE);
            onboarding.setVisibility(View.VISIBLE);
            status.setText("You can return to setup with Check connection.");
        } else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        network.shutdownNow();
        if (uploadCallback != null) uploadCallback.onReceiveValue(null);
        if (web.getParent() instanceof ViewGroup) ((ViewGroup) web.getParent()).removeView(web);
        web.destroy();
        super.onDestroy();
    }
}
