package app.pocketstash.companion;

import org.junit.Test;
import java.nio.charset.StandardCharsets;
import static org.junit.Assert.*;

public class BootstrapPayloadTest {
    @Test public void encodesAndHashesOnlyApkOwnedBytes() {
        String script = BootstrapPayload.build("abc".getBytes(StandardCharsets.UTF_8),
                "data=__POCKET_B64__; sha=__POCKET_SHA256__; again=__POCKET_SHA256__");
        assertTrue(script.contains("data=YWJj"));
        assertTrue(script.contains("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
        assertFalse(script.contains("__POCKET_"));
    }
    @Test(expected = IllegalArgumentException.class)
    public void rejectsTooLargePayloads() {
        BootstrapPayload.build(new byte[190_001], "__POCKET_B64__ __POCKET_SHA256__");
    }
    @Test(expected = IllegalArgumentException.class)
    public void rejectsMissingMarkers() {
        BootstrapPayload.build(new byte[0], "__POCKET_SHA256__");
    }
}
