package com.und.rcs.tabletsimple

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.JavascriptInterface
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

/**
 * 단순 이동(serving / simple_move) 로봇용 태블릿.
 * 서버의 /api/tasks/tablet-simple/{robotId} 페이지를 WebView 로 표시.
 *
 * 화면 구성(서버 템플릿 기준):
 *  - 좌상단: 배터리
 *  - 상단 중앙: 시스템 이름 (RCS 설정 동기화)
 *  - 우상단: 로봇 이름
 *  - 화면 중앙(큰 글씨): 현재 상태
 *  - 그 아래: 목적지 이름
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 화면 항상 켜두기
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        webView = WebView(this)

        // WebView 위에 네이티브 설정 버튼을 오버레이 — FrameLayout 컨테이너 사용
        val container = FrameLayout(this)
        container.addView(
            webView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )

        // 좌상단 설정 버튼 (반투명 원형 톱니바퀴)
        val dp = resources.displayMetrics.density
        val btnSize = (48 * dp).toInt()
        val btnMargin = (12 * dp).toInt()

        val btnBg = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(Color.argb(48, 255, 255, 255))
            setStroke((1.5f * dp).toInt(), Color.argb(80, 255, 255, 255))
        }
        val settingsBtn = Button(this).apply {
            text = "⚙"
            setTextColor(Color.argb(200, 255, 255, 255))
            textSize = 22f
            background = btnBg
            setPadding(0, 0, 0, 0)
            setOnClickListener {
                val prefs = getSharedPreferences("config", Context.MODE_PRIVATE)
                showConfigDialog(prefs) { url, id -> loadTablet(url, id) }
            }
        }
        val btnParams = FrameLayout.LayoutParams(btnSize, btnSize).apply {
            gravity = Gravity.TOP or Gravity.START
            setMargins(btnMargin, btnMargin, 0, 0)
        }
        container.addView(settingsBtn, btnParams)

        setContentView(container)

        hideSystemUI()

        webView.addJavascriptInterface(AndroidBridge(), "Android")
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_NO_CACHE
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        }

        val prefs = getSharedPreferences("config", Context.MODE_PRIVATE)
        val serverUrl = prefs.getString("server_url", "") ?: ""
        val robotId = prefs.getString("robot_id", "") ?: ""

        if (serverUrl.isEmpty() || robotId.isEmpty()) {
            showConfigDialog(prefs) { url, id -> loadTablet(url, id) }
        } else {
            loadTablet(serverUrl, robotId)
        }
    }

    private fun loadTablet(serverUrl: String, robotId: String) {
        webView.webViewClient = WebViewClient()
        webView.loadUrl("${serverUrl.trimEnd('/')}/api/tasks/tablet-simple/$robotId")
    }

    private fun showConfigDialog(
        prefs: android.content.SharedPreferences,
        onConfirm: (String, String) -> Unit
    ) {
        val dp = resources.displayMetrics.density
        val pad = (24 * dp).toInt()

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
        }

        fun label(text: String) = TextView(this).apply {
            this.text = text
            textSize = 16f
            setPadding(0, (8 * dp).toInt(), 0, (4 * dp).toInt())
        }

        val etUrl = EditText(this).apply {
            setText(prefs.getString("server_url", "http://192.168.0.21:8002"))
            hint = "http://서버IP:8002"
            textSize = 18f
        }
        val etId = EditText(this).apply {
            setText(prefs.getString("robot_id", ""))
            hint = "예: 16"
            textSize = 22f
            inputType = android.text.InputType.TYPE_CLASS_NUMBER
        }

        layout.addView(label("서버 주소"))
        layout.addView(etUrl)
        layout.addView(label("로봇 ID"))
        layout.addView(etId)

        AlertDialog.Builder(this)
            .setTitle("태블릿(단순) 설정")
            .setView(layout)
            .setCancelable(false)
            .setPositiveButton("시작") { _, _ ->
                val url = etUrl.text.toString().trim().trimEnd('/')
                val id = etId.text.toString().trim()
                if (url.isNotEmpty() && id.isNotEmpty()) {
                    prefs.edit()
                        .putString("server_url", url)
                        .putString("robot_id", id)
                        .apply()
                    onConfirm(url, id)
                } else {
                    showConfigDialog(prefs, onConfirm)
                }
            }
            .setNeutralButton("설정 초기화") { _, _ ->
                prefs.edit().clear().apply()
                showConfigDialog(prefs, onConfirm)
            }
            .show()
    }

    private fun hideSystemUI() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_FULLSCREEN or
            View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        )
    }

    inner class AndroidBridge {
        @JavascriptInterface
        fun openSettings() {
            runOnUiThread {
                val prefs = getSharedPreferences("config", Context.MODE_PRIVATE)
                showConfigDialog(prefs) { url, id -> loadTablet(url, id) }
            }
        }

        @JavascriptInterface
        fun goHome() {
            runOnUiThread {
                val intent = Intent(Intent.ACTION_MAIN).apply {
                    addCategory(Intent.CATEGORY_HOME)
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                }
                startActivity(intent)
            }
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUI()
    }
}
