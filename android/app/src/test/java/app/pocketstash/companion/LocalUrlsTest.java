package app.pocketstash.companion;

import org.junit.Test;
import static org.junit.Assert.*;

public class LocalUrlsTest {
    @Test public void allowsOnlyTwoLoopbackOrigins() {
        assertTrue(LocalUrls.isAllowed("http://127.0.0.1:8765/"));
        assertTrue(LocalUrls.isAllowed("http://localhost:9999/scenes/123?x=y"));
        assertFalse(LocalUrls.isAllowed("http://127.0.0.1:80/"));
        assertFalse(LocalUrls.isAllowed("https://127.0.0.1:8765/"));
        assertFalse(LocalUrls.isAllowed("http://127.0.0.1.evil.example:8765/"));
        assertFalse(LocalUrls.isAllowed("http://evil.example@127.0.0.1:8765/"));
        assertFalse(LocalUrls.isAllowed("http://127.0.0.1@evil.example:8765/"));
        assertFalse(LocalUrls.isAllowed("http://192.168.1.1:9999/"));
        assertFalse(LocalUrls.isAllowed("file:///etc/passwd"));
        assertFalse(LocalUrls.isAllowed("intent://127.0.0.1:8765/"));
        assertFalse(LocalUrls.isAllowed("javascript:alert(1)"));
        assertFalse(LocalUrls.isAllowed(null));
    }
    @Test public void externalBrowserOnlyAcceptsHttps() {
        assertTrue(LocalUrls.isExternalHttps("https://github.com/termux/termux-app"));
        assertFalse(LocalUrls.isExternalHttps("https://user@github.com/"));
        assertFalse(LocalUrls.isExternalHttps("http://example.com/"));
        assertFalse(LocalUrls.isExternalHttps("intent://example.com/"));
    }
}
