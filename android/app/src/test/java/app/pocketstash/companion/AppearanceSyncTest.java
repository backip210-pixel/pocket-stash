package app.pocketstash.companion;

import org.junit.Test;
import static org.junit.Assert.*;

public class AppearanceSyncTest {
    @Test public void setupUrlCarriesTheSavedTheme() {
        assertEquals("http://127.0.0.1:8765/?appearance=dark", AppearanceSync.setupUrl(true));
        assertEquals("http://127.0.0.1:8765/?appearance=light", AppearanceSync.setupUrl(false));
    }

    @Test public void pageSyncUsesSavedThemeAndSupportsOldCompanion() {
        String dark = AppearanceSync.script(true);
        String light = AppearanceSync.script(false);
        assertTrue(dark.contains("var t='dark'"));
        assertTrue(light.contains("var t='light'"));
        for (String script : new String[]{dark, light}) {
            assertTrue(script.contains("window.pocketApplyAppearance(t)"));
            assertTrue(script.contains("document.documentElement.dataset.theme=t"));
            assertTrue(script.contains("localStorage.setItem('pocket-theme',t)"));
        }
    }
}
