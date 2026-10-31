package app.pocketstash.companion;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Base64;

/** Creates a bounded Termux stdin script from APK-owned, not web-provided, data. */
final class BootstrapPayload {
    static final int MAX_SCRIPT_CHARS = 280_000; // comfortably below Termux's Binder limit
    private static final String DATA_MARKER = "__POCKET_B64__";
    private static final String HASH_MARKER = "__POCKET_SHA256__";

    private BootstrapPayload() { }

    static String build(byte[] archive, String template) {
        if (archive == null || template == null || !template.contains(DATA_MARKER)
                || !template.contains(HASH_MARKER)) {
            throw new IllegalArgumentException("Missing embedded companion files.");
        }
        // Check before encoding, so a too-large build cannot allocate an unbounded intent.
        if (archive.length > 190_000) {
            throw new IllegalArgumentException("Embedded companion is too large for Termux. Use manual mode.");
        }
        String script = template.replace(DATA_MARKER, Base64.getEncoder().encodeToString(archive))
                .replace(HASH_MARKER, sha256(archive));
        if (script.length() > MAX_SCRIPT_CHARS) {
            throw new IllegalArgumentException("Embedded companion is too large for Termux. Use manual mode.");
        }
        return script;
    }

    private static String sha256(byte[] bytes) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
            StringBuilder output = new StringBuilder(digest.length * 2);
            for (byte value : digest) output.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
            return output.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is unavailable", e);
        }
    }
}
