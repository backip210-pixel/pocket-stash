package app.pocketstash.companion;

import org.junit.Test;
import static org.junit.Assert.*;

public class PhotosLinkTest {
    @Test public void onlyActualCompanionHomeCanInitiateHelpers() {
        assertTrue(PhotosLink.isCompanionPage("http://127.0.0.1:8765/"));
        assertTrue(PhotosLink.isCompanionPage("http://127.0.0.1:8765/#performers"));
        assertFalse(PhotosLink.isCompanionPage("http://127.0.0.1:9999/"));
        assertFalse(PhotosLink.isCompanionPage("http://localhost:8765/"));
        assertFalse(PhotosLink.isCompanionPage("http://127.0.0.1:8765/other"));
        assertFalse(PhotosLink.isCompanionPage("https://127.0.0.1:8765/"));
        assertFalse(PhotosLink.isCompanionPage("http://user@127.0.0.1:8765/"));
        assertFalse(PhotosLink.isCompanionPage(null));
    }

    @Test public void onlyOpaqueJournalIdsAndTwoSimpleActionsAreAllowed() {
        assertEquals("app", PhotosLink.parse("pocketstash://photos/app").action);
        assertEquals("scan", PhotosLink.parse("pocketstash://photos/scan").action);
        PhotosLink.Command open = PhotosLink.parse("pocketstash://photos/open?id=0123456789abcdef01234567");
        assertNotNull(open);
        assertEquals("open", open.action);
        assertEquals("0123456789abcdef01234567", open.itemId);
        assertNull(PhotosLink.parse("pocketstash://photos/delete?id=0123456789abcdef01234567"));
        assertNull(PhotosLink.parse("pocketstash://photos/open?path=/storage/private.mp4"));
        assertNull(PhotosLink.parse("pocketstash://photos/open?id=0123456789abcdef01234567&id=1"));
        assertNull(PhotosLink.parse("pocketstash://photos/open?id=0123456789ABCDEF01234567"));
        assertNull(PhotosLink.parse("pocketstash://photos/scan?x=1"));
        assertNull(PhotosLink.parse("pocketstash://photos/app#fragment"));
        assertNull(PhotosLink.parse("pocketstash://user@photos/app"));
        assertNull(PhotosLink.parse("pocketstash://photos:123/app"));
        assertNull(PhotosLink.parse("https://photos/app"));
        assertNull(PhotosLink.parse("pocketstash://photos/%61pp"));
        assertNull(PhotosLink.parse(null));
    }
}
