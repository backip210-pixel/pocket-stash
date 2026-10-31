package app.pocketstash.companion;

import java.net.URI;
import java.net.URISyntaxException;

/** Strictly validates the tiny, non-destructive native link interface. */
final class PhotosLink {
    static final class Command {
        final String action;
        final String itemId;
        Command(String action, String itemId) { this.action = action; this.itemId = itemId; }
    }

    static boolean isCompanionPage(String address) {
        try {
            URI uri = new URI(address);
            String path = uri.getRawPath();
            return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
                    && uri.getPort() == 8765 && ("/".equals(path) || "".equals(path))
                    && uri.getRawUserInfo() == null;
        } catch (URISyntaxException | NullPointerException e) {
            return false;
        }
    }

    static Command parse(String address) {
        try {
            URI uri = new URI(address);
            if (!"pocketstash".equals(uri.getScheme()) || !"photos".equals(uri.getHost())
                    || uri.getPort() != -1 || uri.getRawUserInfo() != null || uri.getRawFragment() != null) return null;
            String path = uri.getRawPath();
            String query = uri.getRawQuery();
            if ("/app".equals(path) && query == null) return new Command("app", null);
            if ("/scan".equals(path) && query == null) return new Command("scan", null);
            if ("/open".equals(path) && query != null && query.matches("id=[a-f0-9]{24}")) {
                return new Command("open", query.substring(3));
            }
        } catch (URISyntaxException | NullPointerException e) {
            return null;
        }
        return null;
    }

    private PhotosLink() { }
}
