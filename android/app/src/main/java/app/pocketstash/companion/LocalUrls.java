package app.pocketstash.companion;

import java.net.URI;
import java.net.URISyntaxException;

/** Strictly allow only the two HTTP services on this device. */
final class LocalUrls {
    private LocalUrls() { }

    static boolean isAllowed(String raw) {
        if (raw == null) return false;
        try {
            URI uri = new URI(raw);
            String host = uri.getHost();
            return "http".equalsIgnoreCase(uri.getScheme())
                    && uri.getUserInfo() == null
                    && ("127.0.0.1".equals(host) || "localhost".equalsIgnoreCase(host))
                    && (uri.getPort() == 8765 || uri.getPort() == 9999);
        } catch (URISyntaxException | IllegalArgumentException e) {
            return false;
        }
    }

    static boolean isExternalHttps(String raw) {
        if (raw == null) return false;
        try {
            URI uri = new URI(raw);
            return "https".equalsIgnoreCase(uri.getScheme()) && uri.getHost() != null
                    && uri.getUserInfo() == null;
        } catch (URISyntaxException | IllegalArgumentException e) {
            return false;
        }
    }
}
