import asyncio
import glob
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.parse import urlparse, parse_qs
from urllib.error import URLError
try:
    import winreg
except ImportError:
    winreg = None
CLIENT_ID = "1469972396336353451"
LARGE_IMAGE_KEY = "roblox_logo"
POLL_INTERVAL_SEC = 2
MAX_LOG_LINES = 300
PORT = 47882
SCRIPT_PATH = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APP_NAME = "AzqTracker"
SETTINGS_FILE = os.path.join(os.path.dirname(SCRIPT_PATH), "azq_tracker_settings.json")
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "logs")
JOIN_PATTERN = re.compile(r"! Joining game '([0-9a-fA-F-]{36})' place (\d+) at")
LEAVE_PATTERN = re.compile(
    r"(! Leaving|leaveUIGame|Disconnecting|connectionLost|Client:Disconnect|GameEnded)",
    re.IGNORECASE
)
def is_roblox_running():
    try:
        output = subprocess.check_output(
            'tasklist /FI "IMAGENAME eq RobloxPlayerBeta.exe"',
            shell=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        return "RobloxPlayerBeta.exe" in output
    except Exception:
        return True
DEFAULT_SETTINGS = {"lang": "ja", "allow_join": False}
def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("lang") in ("ja", "en"):
                settings["lang"] = data["lang"]
            if isinstance(data.get("allow_join"), bool):
                settings["allow_join"] = data["allow_join"]
        except Exception:
            pass
    return settings
def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        return True
    except Exception:
        return False
def load_lang():
    return load_settings()["lang"]
def save_lang(lang):
    settings = load_settings()
    settings["lang"] = lang
    return save_settings(settings)
def is_join_enabled():
    return load_settings()["allow_join"]
def set_join_enabled(enable):
    settings = load_settings()
    settings["allow_join"] = bool(enable)
    return save_settings(settings)
def http_get_json(url):
    req = urlrequest.Request(url, headers={"User-Agent": "AzqTracker/1.0"})
    with urlrequest.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))
def get_startup_command():
    if getattr(sys, "frozen", False):
        return f'"{SCRIPT_PATH}"'
    exe = sys.executable
    pythonw = exe
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            pythonw = candidate
    return f'"{pythonw}" "{SCRIPT_PATH}"'
def is_startup_enabled():
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, STARTUP_APP_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False
def set_startup(enable):
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, STARTUP_APP_NAME, 0, winreg.REG_SZ, get_startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False
def find_latest_log():
    files = glob.glob(os.path.join(LOG_DIR, "*.log"))
    files = [f for f in files if "studio" not in os.path.basename(f).lower()]
    if not files:
        return None
    return max(files, key=os.path.getmtime)
def get_game_info(place_id):
    try:
        data = http_get_json(
            f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
        )
        universe_id = data.get("universeId")
        if not universe_id:
            return f"Place {place_id}", None, "Roblox"
        data2 = http_get_json(
            f"https://games.roblox.com/v1/games?universeIds={universe_id}"
        )
        games = data2.get("data", [])
        if games:
            name = games[0].get("name", f"Place {place_id}")
            creator = games[0].get("creator", {})
            creator_name = creator.get("name", "Roblox")
        else:
            name = f"Place {place_id}"
            creator_name = "Roblox"
        return name, universe_id, creator_name
    except Exception:
        return f"Place {place_id}", None, "Roblox"
def get_game_icon_url(universe_id):
    if not universe_id:
        return None
    try:
        url = (
            "https://thumbnails.roblox.com/v1/games/icons"
            f"?universeIds={universe_id}&size=512x512&format=Png&isCircular=false"
        )
        data = http_get_json(url)
        items = data.get("data", [])
        if items:
            return items[0].get("imageUrl")
    except Exception:
        pass
    return None
class DiscordIPCError(Exception):
    pass
class DiscordIPC:
    def __init__(self, client_id, loop):
        self.client_id = client_id
        self.loop = loop
        self.reader = None
        self.writer = None
    @staticmethod
    def _find_pipe():
        base = r"\\?\pipe"
        try:
            for entry in os.scandir(base):
                if entry.name.startswith("discord-ipc-"):
                    return entry.path
        except FileNotFoundError:
            pass
        return None
    async def connect(self):
        path = self._find_pipe()
        if not path:
            raise DiscordIPCError("Discordが見つかりません(起動していますか?)")
        self.reader = asyncio.StreamReader(loop=self.loop)
        protocol = asyncio.StreamReaderProtocol(self.reader, loop=self.loop)
        transport, _ = await asyncio.wait_for(
            self.loop.create_pipe_connection(lambda: protocol, path), timeout=10
        )
        self.writer = asyncio.StreamWriter(transport, protocol, self.reader, self.loop)
        await self._send(0, {"v": 1, "client_id": self.client_id})
        resp = await self._read()
        if resp.get("evt") == "ERROR":
            raise DiscordIPCError(resp.get("data", {}).get("message", "handshake failed"))
    async def _send(self, op, payload):
        data = json.dumps(payload).encode("utf-8")
        self.writer.write(struct.pack("<II", op, len(data)) + data)
        await self.writer.drain()
    async def _read(self):
        header = await asyncio.wait_for(self.reader.readexactly(8), timeout=10)
        op, length = struct.unpack("<II", header)
        data = await asyncio.wait_for(self.reader.readexactly(length), timeout=10)
        return json.loads(data.decode("utf-8"))
    async def set_activity(self, activity):
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(time.time()),
        }
        await self._send(1, payload)
        return await self._read()
    async def clear_activity(self):
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": None},
            "nonce": str(time.time()),
        }
        await self._send(1, payload)
        return await self._read()
    async def close(self):
        try:
            self.writer.close()
        except Exception:
            pass
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.game_name = None
        self.log_lines = []
    def add_log(self, message):
        with self.lock:
            timestamp = time.strftime("%H:%M:%S")
            self.log_lines.append(f"[{timestamp}] {message}")
            if len(self.log_lines) > MAX_LOG_LINES:
                self.log_lines = self.log_lines[-MAX_LOG_LINES:]
    def set_status(self, connected, game_name):
        with self.lock:
            self.connected = connected
            self.game_name = game_name
    def snapshot(self):
        with self.lock:
            return {
                "connected": self.connected,
                "game": self.game_name,
                "logs": list(self.log_lines),
            }
RPC_BUTTON_TEXT = {
    "ja": {"view": "ゲームを見る", "join": "ゲームに参加", "dl": "DL AzqTracker"},
    "en": {"view": "View Game", "join": "Join Game", "dl": "Download AzqTracker"},
}
class AzqTracker:
    def __init__(self, state: SharedState):
        self.state = state
        self.playing = None
        self.current_log = None
        self.log_pos = 0
        self.stop_event = threading.Event()
        self.loop = None
        self.ipc = None
    def log(self, message):
        self.state.add_log(message)
    def set_status(self, connected, game_name):
        self.state.set_status(connected, game_name)
    def stop(self):
        self.stop_event.set()
    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        finally:
            self.loop.close()
    async def _sleep(self, seconds):
        step = 0.5
        elapsed = 0
        while elapsed < seconds and not self.stop_event.is_set():
            await asyncio.sleep(min(step, seconds - elapsed))
            elapsed += step
    async def _connect_discord(self):
        while not self.stop_event.is_set():
            try:
                self.ipc = DiscordIPC(CLIENT_ID, self.loop)
                await self.ipc.connect()
                self.log("Discordに接続しました")
                self.set_status(True, self.playing)
                return True
            except Exception as e:
                self.log(f"Discordに接続できません。再試行します... ({e})")
                self.set_status(False, None)
                await self._sleep(5)
        return False
    async def _update_presence(self, game_name, universe_id, place_id, job_id, creator_name):
        icon_url = await self.loop.run_in_executor(None, get_game_icon_url, universe_id)
        large_image = icon_url if icon_url else LARGE_IMAGE_KEY
        lang = await self.loop.run_in_executor(None, load_lang)
        texts = RPC_BUTTON_TEXT.get(lang, RPC_BUTTON_TEXT["ja"])
        allow_join = await self.loop.run_in_executor(None, is_join_enabled)
        if allow_join:
            game_button = {
                "label": texts["join"],
                "url": f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}",
            }
        else:
            game_button = {
                "label": texts["view"],
                "url": f"https://www.roblox.com/games/{place_id}",
            }
        activity = {
            "details": game_name,
            "state": creator_name,
            "timestamps": {"start": int(time.time())},
            "assets": {
                "large_image": large_image,
                "large_text": game_name,
                "small_image": LARGE_IMAGE_KEY,
                "small_text": "AzqTracker",
            },
            "buttons": [
                game_button,
                {"label": texts["dl"], "url": "https://azqtracker.f5.si/"},
            ],
        }
        try:
            await self.ipc.set_activity(activity)
            self.log(f"ステータス更新: {game_name}")
            self.set_status(True, game_name)
        except Exception:
            self.log("Discordとの接続が切れました。再接続します...")
            await self._connect_discord()
    async def _clear_presence(self):
        try:
            await self.ipc.clear_activity()
            self.log("ステータスをクリアしました")
            self.set_status(True, None)
        except Exception:
            pass
    def _read_new_lines(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.log_pos)
                lines = f.readlines()
                pos = f.tell()
            return lines, pos
        except FileNotFoundError:
            return [], self.log_pos
    async def _watch_logs(self):
        while not self.stop_event.is_set():
            try:
                if self.playing and not await self.loop.run_in_executor(None, is_roblox_running):
                    self.playing = None
                    await self._clear_presence()
                latest = await self.loop.run_in_executor(None, find_latest_log)
                if latest != self.current_log:
                    self.current_log = latest
                    if latest and os.path.exists(latest):
                        self.log_pos = os.path.getsize(latest)
                    else:
                        self.log_pos = 0
                if latest:
                    new_lines, new_pos = await self.loop.run_in_executor(
                        None, self._read_new_lines, latest
                    )
                    self.log_pos = new_pos
                    for line in new_lines:
                        m = JOIN_PATTERN.search(line)
                        if m:
                            job_id = m.group(1)
                            place_id = m.group(2)
                            game_name, universe_id, creator_name = await self.loop.run_in_executor(
                                None, get_game_info, place_id
                            )
                            if game_name != self.playing:
                                self.playing = game_name
                                await self._update_presence(game_name, universe_id, place_id, job_id, creator_name)
                        elif LEAVE_PATTERN.search(line) and self.playing:
                            self.playing = None
                            await self._clear_presence()
            except Exception as e:
                self.log(f"エラーが発生しましたが継続します: {e}")
            await self._sleep(POLL_INTERVAL_SEC)
    async def _main(self):
        if not os.path.isdir(LOG_DIR):
            self.log(f"Robloxのログフォルダが見つかりません: {LOG_DIR}")
            self.log("Windows以外の環境では動作しません。フォルダが見つかるまで待機します...")
            while not os.path.isdir(LOG_DIR) and not self.stop_event.is_set():
                await self._sleep(10)
            if self.stop_event.is_set():
                return
        while not self.stop_event.is_set():
            try:
                if not await self._connect_discord():
                    break
                self.log("監視を開始しました。Robloxでゲームに参加してみてください。")
                await self._watch_logs()
            except Exception as e:
                self.log(f"予期しないエラーが発生しました。5秒後に再開します: {e}")
                await self._sleep(5)
        if self.ipc:
            try:
                await self.ipc.clear_activity()
                await self.ipc.close()
            except Exception:
                pass
        self.set_status(False, None)
        self.log("停止しました")
PAGE_HTML =
class AppServer:
    def __init__(self):
        self.state = SharedState()
        self.tracker = None
        self.tracker_thread = None
        self.httpd = None
    def start_tracker(self):
        if self.tracker is not None:
            return
        self.tracker = AzqTracker(self.state)
        self.tracker_thread = threading.Thread(target=self.tracker.run, daemon=True)
        self.tracker_thread.start()
    def stop_tracker(self):
        if self.tracker:
            self.state.add_log("停止しています...")
            self.tracker.stop()
            self.tracker = None
    def make_handler(self):
        server = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def _send(self, status, content_type, body: bytes):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                if self.path == "/" or self.path == "":
                    self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
                elif self.path.startswith("/api/ping"):
                    self._send(200, "application/json", b'{"ok": true}')
                elif self.path.startswith("/api/status"):
                    body = json.dumps(server.state.snapshot()).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/startup_status"):
                    body = json.dumps({"enabled": is_startup_enabled()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/startup_enable"):
                    ok = set_startup(True)
                    body = json.dumps({"ok": ok, "enabled": is_startup_enabled()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/startup_disable"):
                    ok = set_startup(False)
                    body = json.dumps({"ok": ok, "enabled": is_startup_enabled()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/lang_get"):
                    body = json.dumps({"lang": load_lang()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/lang_set"):
                    qs = parse_qs(urlparse(self.path).query)
                    lang = qs.get("lang", ["ja"])[0]
                    if lang not in ("ja", "en"):
                        lang = "ja"
                    ok = save_lang(lang)
                    body = json.dumps({"ok": ok, "lang": load_lang()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/join_status"):
                    body = json.dumps({"enabled": is_join_enabled()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/join_enable"):
                    ok = set_join_enabled(True)
                    body = json.dumps({"ok": ok, "enabled": is_join_enabled()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/join_disable"):
                    ok = set_join_enabled(False)
                    body = json.dumps({"ok": ok, "enabled": is_join_enabled()}).encode("utf-8")
                    self._send(200, "application/json", body)
                elif self.path.startswith("/api/start"):
                    server.start_tracker()
                    self._send(200, "application/json", b'{"ok": true}')
                elif self.path.startswith("/api/stop"):
                    server.stop_tracker()
                    self._send(200, "application/json", b'{"ok": true}')
                elif self.path.startswith("/api/exit"):
                    self._send(200, "application/json", b'{"ok": true}')
                    server.stop_tracker()
                    threading.Thread(target=server.httpd.shutdown, daemon=True).start()
                else:
                    self._send(404, "text/plain; charset=utf-8", b"Not Found")
        return Handler
    def already_running(self, url):
        try:
            req = urlrequest.Request(url + "api/ping")
            with urlrequest.urlopen(req, timeout=1) as resp:
                return resp.status == 200
        except Exception:
            return False
    def run(self):
        url = f"http://127.0.0.1:{PORT}/"
        if self.already_running(url):
            webbrowser.open(url)
            return
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), self.make_handler())
        except OSError:
            webbrowser.open(url)
            return
        self.start_tracker()
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            self.httpd.serve_forever()
        finally:
            self.stop_tracker()
if __name__ == "__main__":
    AppServer().run()
