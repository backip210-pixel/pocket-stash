package app.pocketstash.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;

/** Receives only the result of the explicitly issued, one-shot Termux command. */
public final class TermuxResultReceiver extends BroadcastReceiver {
    static final String PREFS = "pocket_termux";
    static final String ID = "run_id";

    @Override public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String id = intent.getStringExtra(ID);
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (id == null || !id.equals(prefs.getString(ID, ""))) return;

        Bundle result = intent.getBundleExtra("result");
        String message;
        if (result == null) {
            message = "Termux did not provide a command result. Check its notifications and permissions.";
        } else {
            int error = result.getInt("err", -1);
            int exit = result.getInt("exitCode", 0);
            String detail = result.getString("errmsg", "");
            String stderr = result.getString("stderr", "");
            String stdout = result.getString("stdout", "");
            if (error == -1 && exit == 0) {
                message = "Termux stopped the setup server. You can start it again.";
            } else {
                message = "Termux could not start the setup server (exit " + exit + ").";
                String extra = !detail.isEmpty() ? detail : !stderr.isEmpty() ? stderr : stdout;
                if (!extra.isEmpty()) {
                    // Keep the UI and preferences bounded; don't include terminal control codes.
                    extra = extra.replaceAll("\\u001B\\[[0-?]*[ -/]*[@-~]", "").trim();
                    if (extra.length() > 1800) extra = extra.substring(extra.length() - 1800);
                    message += "\n\n" + extra;
                }
            }
        }
        prefs.edit().putBoolean("finished", true).putString("result", message).apply();
    }
}
