package app.cryptoanalyst;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.TextUtils;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Toast;

import android.app.Activity;

public class SetupActivity extends Activity {
    static final String PREFS = "crypto_analyst";
    static final String KEY_URL = "server_url";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        boolean forceSetup = getIntent().getBooleanExtra("force_setup", false);
        String saved = prefs.getString(KEY_URL, "");
        if (!forceSetup && !TextUtils.isEmpty(saved)) {
            openMain(saved);
            return;
        }
        setContentView(R.layout.activity_setup);
        EditText urlBox = findViewById(R.id.serverUrl);
        Button openBtn = findViewById(R.id.openBtn);
        if (!TextUtils.isEmpty(saved)) {
            urlBox.setText(saved);
        }
        openBtn.setOnClickListener(v -> {
            String url = normalize(urlBox.getText().toString());
            if (url == null) {
                Toast.makeText(this, "请填写地址，或点「本机 Termux」", Toast.LENGTH_SHORT).show();
                return;
            }
            prefs.edit().putString(KEY_URL, url).apply();
            openMain(url);
        });
        findViewById(R.id.localBtn).setOnClickListener(v -> {
            String url = "http://127.0.0.1:8000";
            prefs.edit().putString(KEY_URL, url).apply();
            openMain(url);
        });
    }

    private void openMain(String url) {
        Intent intent = new Intent(this, MainActivity.class);
        intent.putExtra(KEY_URL, url);
        startActivity(intent);
        finish();
    }

    static String normalize(String raw) {
        if (raw == null) return null;
        String url = raw.trim();
        if (url.isEmpty()) return null;
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }
}
