"""Floating Ticker TW: a PyQt6 floating quote for Taiwan stocks."""
import json
import os
import re
import sys
import threading
import time
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

_QT_DLL_HANDLES = []


def _prepare_qt_dll_path():
    if sys.platform != "win32":
        return
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "PyQt6", "Qt6", "bin"))
    else:
        roots.append(os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages", "PyQt6", "Qt6", "bin"))
    for root in roots:
        if os.path.isdir(root):
            if hasattr(os, "add_dll_directory"):
                _QT_DLL_HANDLES.append(os.add_dll_directory(root))
            os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


_prepare_qt_dll_path()

from PyQt6.QtCore import Qt, QObject, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
    QVBoxLayout, QWidget,
)


THEMES = {
    "dark": {"bg": "#14171f", "border": "#262b36", "text_primary": "#ffffff", "text_secondary": "#a2a8b5", "text_muted": "#6c7584", "text_dim": "#5d6675", "up": "#0ecb81", "down": "#f6465d", "up_bg": "rgba(14, 203, 129, 0.12)", "down_bg": "rgba(246, 70, 93, 0.12)"},
    "light": {"bg": "#ffffff", "border": "#e5e7eb", "text_primary": "#111827", "text_secondary": "#4b5563", "text_muted": "#6b7280", "text_dim": "#9ca3af", "up": "#16a34a", "down": "#dc2626", "up_bg": "rgba(22, 163, 74, 0.10)", "down_bg": "rgba(220, 38, 38, 0.10)"},
}
DEFAULT_SYMBOLS = ["0050", "2330", "2317", "2454", "2303", "2881", "2882", "1301", "2412", "2603"]
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,8}$")
TWSE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={symbols}&json=1&delay=0"


def normalise_symbol(value):
    value = str(value or "").strip().upper().replace(".TW", "")
    return value if SYMBOL_RE.fullmatch(value) else ""


def _parse_proxy_url(url):
    try:
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.port:
            return None
        return {"http_proxy_host": parsed.hostname, "http_proxy_port": parsed.port, "proxy_type": "socks5" if parsed.scheme.lower().startswith("socks") else "http", "http_proxy_auth": (parsed.username, parsed.password) if parsed.username else None}
    except Exception:
        return None


def resolve_proxy(config):
    if config and config.get("enabled") and config.get("host"):
        try:
            port = int(config.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port > 0:
            return {"http_proxy_host": config["host"], "http_proxy_port": port, "proxy_type": config.get("type", "http"), "http_proxy_auth": (config.get("username"), config.get("password")) if config.get("username") else None}
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        value = os.environ.get(name)
        if value:
            proxy = _parse_proxy_url(value)
            if proxy:
                return proxy
    return None


class ProxyDialog(QDialog):
    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle("代理設定")
        self.setMinimumWidth(380)
        layout = QFormLayout(self)
        self.enabled_chk = QCheckBox("啟用代理")
        self.enabled_chk.setChecked(bool(current.get("enabled")))
        layout.addRow(self.enabled_chk)
        self.type_combo = QComboBox()
        self.type_combo.addItems(["HTTP"])
        self.type_combo.setCurrentText(current.get("type", "http").upper())
        layout.addRow("代理類型:", self.type_combo)
        self.host_edit = QLineEdit(current.get("host", "127.0.0.1"))
        layout.addRow("主機:", self.host_edit)
        self.port_edit = QLineEdit(str(current.get("port", "")))
        layout.addRow("埠號:", self.port_edit)
        self.user_edit = QLineEdit(current.get("username", ""))
        layout.addRow("使用者名稱:", self.user_edit)
        self.pass_edit = QLineEdit(current.get("password", ""))
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("密碼:", self.pass_edit)
        hint = QLabel("也支援環境變數 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY。")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_config(self):
        try:
            port = int(self.port_edit.text())
        except ValueError:
            port = 0
        return {"enabled": self.enabled_chk.isChecked(), "type": self.type_combo.currentText().lower(), "host": self.host_edit.text().strip(), "port": port, "username": self.user_edit.text().strip(), "password": self.pass_edit.text()}


class SymbolsDialog(QDialog):
    def __init__(self, symbols, parent=None):
        super().__init__(parent)
        self.setWindowTitle("股票代號設定")
        self.setMinimumWidth(320)
        self._inputs = []
        layout = QFormLayout(self)
        hint = QLabel("輸入 2–8 位上市或上櫃股票代號，最多儲存 10 個。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addRow(hint)
        for index in range(10):
            edit = QLineEdit(symbols[index] if index < len(symbols) else "")
            edit.setPlaceholderText("例如 0050")
            edit.setMaxLength(8)
            edit.setClearButtonEnabled(True)
            self._inputs.append(edit)
            layout.addRow(f"代號 {index + 1}:", edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_symbols(self):
        result = []
        for edit in self._inputs:
            symbol = normalise_symbol(edit.text())
            if symbol and symbol not in result:
                result.append(symbol)
        return result[:10]


class StockWorker(QObject):
    ticker = pyqtSignal(dict)
    status = pyqtSignal(bool, str)

    def __init__(self, symbol, proxy_config):
        super().__init__()
        self.symbol = symbol
        self.proxy_config = proxy_config or {}
        self._stop = False
        self._thread = None

    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def _run(self):
        while not self._stop:
            try:
                data = self._fetch()
                if data:
                    self.ticker.emit(data)
                    self.status.emit(True, "已連線")
                else:
                    self.status.emit(False, "查無此代號")
            except Exception as exc:
                self.status.emit(False, "連線失敗: " + str(exc))
            for _ in range(50):
                if self._stop:
                    return
                time.sleep(0.1)

    def _fetch(self):
        symbols = quote(f"tse_{self.symbol}.tw|otc_{self.symbol}.tw", safe="_|.")
        request = Request(TWSE_URL.format(symbols=symbols), headers={"User-Agent": "FloatingTickerTW/1.0"})
        proxy = resolve_proxy(self.proxy_config)
        handlers = []
        if proxy and proxy.get("proxy_type") == "http":
            prefix = "http://"
            auth = proxy.get("http_proxy_auth")
            if auth:
                prefix += quote(str(auth[0])) + ":" + quote(str(auth[1])) + "@"
            proxy_url = f"{prefix}{proxy['http_proxy_host']}:{proxy['http_proxy_port']}"
            handlers.append(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        opener = build_opener(*handlers)
        with opener.open(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
        item = next((entry for entry in payload.get("msgArray", []) if entry.get("c") == self.symbol and entry.get("z") not in (None, "-", "")), None)
        if not item:
            return None
        price = float(item.get("z") or item.get("pz"))
        previous = float(item.get("y") or 0)
        return {"price": price, "change": ((price - previous) / previous * 100) if previous else 0.0, "high": float(item.get("h") or price), "low": float(item.get("l") or price), "name": item.get("n") or self.symbol}


class TickerWindow(QWidget):
    def __init__(self):
        super().__init__()
        # 獨立命名空間，絕不讀取或覆蓋原版 FloatingTicker/Ticker 設定。
        self._settings = QSettings("FloatingTickerTW", "Ticker")
        self._saved_symbols = self._load_symbols()
        self._current_symbol = normalise_symbol(self._settings.value("symbol", self._saved_symbols[0])) or self._saved_symbols[0]
        self._current_name = str(self._settings.value("name", "台灣股票"))
        self._persist_symbols()
        self._theme_mode = self._settings.value("theme_mode", "auto")
        if self._theme_mode not in ("auto", "dark", "light"):
            self._theme_mode = "auto"
        self._system_theme = self._detect_system_theme()
        try:
            self._proxy_config = json.loads(self._settings.value("proxy", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            self._proxy_config = {}
        if not isinstance(self._proxy_config, dict):
            self._proxy_config = {}
        self._connected = False
        self._price_flash = None
        self._change_trend = None
        self._drag_pos = None
        self._last_price = None
        self._always_on_top = True
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(280, 120)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._build_ui()
        self._apply_styles()
        try:
            from PyQt6.QtGui import QGuiApplication
            QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
        except Exception:
            pass
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(400)
        self._flash_timer.timeout.connect(self._clear_flash)
        self._worker = None
        self._start_worker()

    def _load_symbols(self):
        raw = self._settings.value("symbols", "")
        try:
            values = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            values = []
        result = []
        if isinstance(values, list):
            for value in values:
                symbol = normalise_symbol(value)
                if symbol and symbol not in result:
                    result.append(symbol)
        for symbol in DEFAULT_SYMBOLS:
            if symbol not in result:
                result.append(symbol)
        return result[:10]

    def _persist_symbols(self):
        self._settings.setValue("symbols", json.dumps(self._saved_symbols))

    def _detect_system_theme(self):
        try:
            from PyQt6.QtGui import QGuiApplication
            return "light" if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"
        except Exception:
            return "dark"

    def _current_theme_dict(self):
        return THEMES[self._system_theme if self._theme_mode == "auto" else self._theme_mode]

    def _on_system_scheme_changed(self):
        self._system_theme = self._detect_system_theme()
        if self._theme_mode == "auto":
            self._apply_theme()

    def _set_theme_mode(self, mode):
        if mode in ("auto", "dark", "light") and mode != self._theme_mode:
            self._theme_mode = mode
            self._settings.setValue("theme_mode", mode)
            self._apply_theme()

    def _apply_theme(self):
        self._apply_styles()
        self._set_status_color(self._connected)
        self._set_change_color(self._change_trend)
        self._set_price_color(self._price_flash)

    def _build_ui(self):
        self.card = QWidget(self)
        self.card.setObjectName("card")
        self.card.setFixedSize(280, 120)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.card)
        inner = QVBoxLayout(self.card)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(7)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.pair_base = QLabel(self._current_symbol)
        self.pair_base.setObjectName("pair-base")
        self.pair_quote = QLabel("  " + self._current_name)
        self.pair_quote.setObjectName("pair-quote")
        self.pair_quote.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(6, 6)
        header.addWidget(self.pair_base, 0, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.pair_quote, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch()
        header.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        inner.addLayout(header)
        price_row = QHBoxLayout()
        price_row.setContentsMargins(0, 0, 0, 0)
        price_row.setSpacing(6)
        self.price_label = QLabel("--")
        self.price_label.setObjectName("price")
        self.price_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.change_label = QLabel("--")
        self.change_label.setObjectName("change")
        self.change_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price_row.addWidget(self.price_label)
        price_row.addStretch()
        price_row.addWidget(self.change_label)
        inner.addLayout(price_row)
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 0, 0, 0)
        stats_row.setSpacing(8)

        def stat_block(title):
            box = QHBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(4)
            title_label = QLabel(title)
            title_label.setObjectName("stat-label")
            value_label = QLabel("--")
            value_label.setObjectName("stat-val")
            box.addWidget(title_label)
            box.addWidget(value_label)
            widget = QWidget()
            widget.setLayout(box)
            return widget, value_label

        high_w, self.high_val = stat_block("今高")
        low_w, self.low_val = stat_block("今低")
        stats_row.addWidget(high_w)
        stats_row.addStretch()
        stats_row.addWidget(low_w)
        inner.addLayout(stats_row)
        mono = self._pick_mono_font()
        price_font = QFont(mono)
        price_font.setPixelSize(22)
        price_font.setWeight(QFont.Weight.Bold)
        self.price_label.setFont(price_font)
        self.price_label.setFixedWidth(150)
        change_font = QFont(mono)
        change_font.setPixelSize(11)
        change_font.setWeight(QFont.Weight.Bold)
        self.change_label.setFont(change_font)
        self.change_label.setFixedWidth(78)
        stat_font = QFont(mono)
        stat_font.setPixelSize(11)
        self.high_val.setFont(stat_font)
        self.low_val.setFont(stat_font)
        self.high_val.setFixedWidth(72)
        self.low_val.setFixedWidth(72)
        pair_font = self.pair_base.font()
        pair_font.setPixelSize(16)
        pair_font.setWeight(QFont.Weight.DemiBold)
        self.pair_base.setFont(pair_font)

    def _pick_mono_font(self):
        families = set(QFontDatabase.families())
        for family in ("Consolas", "Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Menlo", "Monaco", "Courier New", "Courier"):
            if family in families:
                return family
        return QFont().defaultFamily()

    def _apply_styles(self):
        theme = self._current_theme_dict()
        self.setStyleSheet(f'''QWidget {{ font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif; }} QWidget#card {{ background: {theme["bg"]}; border: 1px solid {theme["border"]}; border-radius: 8px; }} QLabel#pair-base {{ color: {theme["text_primary"]}; }} QLabel#pair-quote {{ color: {theme["text_muted"]}; font-size: 11px; }} QLabel#stat-label {{ color: {theme["text_dim"]}; font-size: 11px; }} QLabel#stat-val {{ color: {theme["text_secondary"]}; font-size: 11px; }} QLabel#price {{ color: {theme["text_primary"]}; }} QLabel#change {{ color: {theme["text_muted"]}; padding: 2px 6px; border-radius: 4px; background: transparent; }}''')

    def _set_price_color(self, color_key):
        self._price_flash = color_key
        self.price_label.setStyleSheet("" if color_key is None else "color: " + self._current_theme_dict()[color_key] + ";")

    def _set_change_color(self, trend):
        self._change_trend = trend
        theme = self._current_theme_dict()
        if trend == "up":
            self.change_label.setStyleSheet(f"color: {theme['up']}; background: {theme['up_bg']};")
        elif trend == "down":
            self.change_label.setStyleSheet(f"color: {theme['down']}; background: {theme['down_bg']};")
        else:
            self.change_label.setStyleSheet("")

    def _set_status_color(self, connected):
        self._connected = connected
        color = self._current_theme_dict()["up" if connected else "down"]
        self.status_dot.setStyleSheet(f"background: {color}; border-radius: 3px; min-width: 6px; max-width: 6px; min-height: 6px; max-height: 6px;")

    def _start_worker(self):
        self._stop_worker()
        self._worker = StockWorker(self._current_symbol, self._proxy_config)
        self._worker.ticker.connect(self._on_ticker)
        self._worker.status.connect(self._on_status)
        self._worker.start()

    def _stop_worker(self):
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def _on_status(self, ok, message):
        self._set_status_color(ok)
        if not ok and message:
            self.pair_quote.setText("  " + message[:12])

    def _on_ticker(self, data):
        try:
            price, change = float(data["price"]), float(data["change"])
            high, low = float(data["high"]), float(data["low"])
        except (KeyError, TypeError, ValueError):
            return
        if self._last_price is not None and price != self._last_price:
            self._flash_timer.stop()
            self._set_price_color("up" if price > self._last_price else "down")
            self._flash_timer.start()
        self._last_price = price
        self.price_label.setText(f"{price:,.2f}")
        trend = "up" if change >= 0 else "down"
        self.change_label.setText(f"{'+' if change >= 0 else ''}{change:.2f}%")
        self._set_change_color(trend)
        self.high_val.setText(f"{high:,.2f}")
        self.low_val.setText(f"{low:,.2f}")
        self._current_name = str(data.get("name") or self._current_name)
        self.pair_quote.setText("  " + self._current_name)
        self._settings.setValue("name", self._current_name)

    def _clear_flash(self):
        self._set_price_color(None)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        stock_menu = menu.addMenu("切換股票")
        for symbol in self._saved_symbols:
            action = QAction(symbol, self, checkable=True)
            action.setChecked(symbol == self._current_symbol)
            action.triggered.connect(lambda _checked=False, s=symbol: self._switch_pair(s))
            stock_menu.addAction(action)
        menu.addAction("輸入股票代號...", self._custom_pair)
        menu.addAction("股票代號設定（10個）...", self._open_symbols_dialog)
        menu.addSeparator()
        theme_menu = menu.addMenu("外觀")
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        for label, mode in (("跟隨系統", "auto"), ("深色", "dark"), ("淺色", "light")):
            action = QAction(label, self, checkable=True)
            action.setChecked(self._theme_mode == mode)
            action.triggered.connect(lambda _checked=False, m=mode: self._set_theme_mode(m))
            self._theme_group.addAction(action)
            theme_menu.addAction(action)
        menu.addSeparator()
        menu.addAction("代理設定...", self._open_proxy_dialog)
        menu.addSeparator()
        top_action = QAction("視窗置頂", self, checkable=True)
        top_action.setChecked(self._always_on_top)
        top_action.toggled.connect(self._toggle_top)
        menu.addAction(top_action)
        menu.addSeparator()
        menu.addAction("離開", self.close)
        menu.exec(event.globalPos())

    def _toggle_top(self, checked):
        self._always_on_top = checked
        flags = self.windowFlags()
        flags = flags | Qt.WindowType.WindowStaysOnTopHint if checked else flags & ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _open_proxy_dialog(self):
        dialog = ProxyDialog(self._proxy_config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._proxy_config = dialog.get_config()
            self._settings.setValue("proxy", json.dumps(self._proxy_config))
            self._start_worker()

    def _open_symbols_dialog(self):
        dialog = SymbolsDialog(self._saved_symbols, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        symbols = dialog.get_symbols()
        if not symbols:
            return
        self._saved_symbols = symbols
        self._persist_symbols()
        if self._current_symbol not in symbols:
            self._switch_pair(symbols[0], add_to_saved=False)

    def _switch_pair(self, symbol, add_to_saved=True):
        symbol = normalise_symbol(symbol)
        if not symbol:
            return
        if add_to_saved:
            self._saved_symbols = [symbol] + [s for s in self._saved_symbols if s != symbol]
            self._saved_symbols = self._saved_symbols[:10]
            self._persist_symbols()
        if symbol == self._current_symbol:
            return
        self._current_symbol = symbol
        self._current_name = "台灣股票"
        self._settings.setValue("symbol", symbol)
        self._settings.setValue("name", self._current_name)
        self.pair_base.setText(symbol)
        self.pair_quote.setText("  " + self._current_name)
        self._reset_state()
        self._start_worker()

    def _custom_pair(self):
        text, ok = QInputDialog.getText(self, "輸入股票代號", "輸入上市或上櫃股票代號（例如 0050、2330）:", text=self._current_symbol)
        if ok:
            symbol = normalise_symbol(text)
            if symbol:
                self._switch_pair(symbol)

    def _reset_state(self):
        self._last_price = None
        self.price_label.setText("--")
        self._set_price_color(None)
        self.change_label.setText("--")
        self._set_change_color(None)
        self.high_val.setText("--")
        self.low_val.setText("--")

    def closeEvent(self, event):
        self._settings.setValue("pos", self.pos())
        self._stop_worker()
        super().closeEvent(event)
        QTimer.singleShot(0, QApplication.instance().quit)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Floating Ticker TW")
    window = TickerWindow()
    pos = window._settings.value("pos")
    if pos is not None:
        window.move(pos)
    else:
        screen = app.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            window.move(geometry.right() - window.width() - 20, geometry.top() + 60)
        else:
            window.move(200, 200)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
