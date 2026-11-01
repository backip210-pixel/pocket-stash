package app.pocketstash.companion;

/** Keeps the local setup page in step with the Activity's saved appearance. */
final class AppearanceSync {
    private AppearanceSync() { }

    static String setupUrl(boolean dark) {
        return "http://127.0.0.1:8765/?appearance=" + (dark ? "dark" : "light");
    }

    static String script(boolean dark) {
        String chosen = dark ? "dark" : "light";
        return "(function(){var t='" + chosen + "';"
                + "if(window.pocketApplyAppearance){window.pocketApplyAppearance(t);}"
                + "else{document.documentElement.dataset.theme=t;try{localStorage.setItem('pocket-theme',t);}catch(e){}}})();";
    }
}
