"""Flick: throw files from your phone to this laptop over Wi-Fi.

    python server.py               run in this window and open the laptop page
    python server.py --background  run hidden, with an icon in the system tray
    python server.py --install     start hidden every time you sign in to Windows
    python server.py --uninstall   stop starting at sign-in

Files land in the folder set in config.json (default: ~/Downloads/Flick).
"""

import argparse
import asyncio
import datetime
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import unquote

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CERT_DIR = ROOT / ".cert"
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "flick.log"
MAX_FILE_BYTES = 4 * 1024**3
STAGED_TTL = 3600  # seconds a picked-but-never-flicked file is kept
PHONE_TTL = 12  # seconds without a heartbeat before a phone counts as gone
LOOPBACK = ("127.0.0.1", "::1")


def load_config():
    config = {
        "folder": str(Path.home() / "Downloads" / "Flick"),
        "token": secrets.token_urlsafe(16),
        "phone_port": 7443,
        "desk_port": 7080,
        "landing_card": True,
        "copy_to_clipboard": True,
    }
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


CONFIG = load_config()
FOLDER = Path(CONFIG["folder"]).expanduser().resolve()
STAGING = FOLDER / ".incoming"
SENDERS_PATH = STAGING / "senders.json"
DESK_URL = f"http://127.0.0.1:{CONFIG['desk_port']}/"

staged = {}  # id -> (path, final name, time staged)
desks = set()  # open laptop-page websockets
phones = {}  # device id -> {"name", "seen"}
senders = {}  # file name -> name of the phone that sent it
tray = None
card = None  # the on-screen landing card (overlay.py), Windows only


def log(message):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {message}", flush=True)


def lan_ip():
    # No packet is sent; this only asks the OS which interface it would use.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def ensure_cert(ip):
    """Self-signed cert for the LAN address. Phones need HTTPS for motion sensors."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_path, key_path = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips = [str(a) for a in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            ips = []
        if ip in ips and cert.not_valid_after_utc > now + datetime.timedelta(days=7):
            return cert_path, key_path

    CERT_DIR.mkdir(exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Flick")])
    alt_names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip != "127.0.0.1":
        alt_names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def clean_name(raw):
    name = re.split(r"[\\/]", raw)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "file"
    stem, ext = os.path.splitext(name)
    if stem.upper() in RESERVED:
        stem = "_" + stem
    return stem[:150] + ext[:20]


def unique_path(folder, name):
    path = folder / name
    stem, ext = os.path.splitext(name)
    n = 2
    while path.exists():
        path = folder / f"{stem} ({n}){ext}"
        n += 1
    return path


DOC_TYPES = {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"}


def kind_of(name):
    mime = mimetypes.guess_type(name)[0] or ""
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if os.path.splitext(name)[1].lower() in DOC_TYPES:
        return "doc"
    return "file"


def describe(path):
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "kind": kind_of(path.name),
        "time": stat.st_mtime,
        "from": senders.get(path.name, ""),
    }


def inbox_file(name):
    """Resolve a name to a file directly inside the inbox folder, or 404."""
    path = (FOLDER / name).resolve()
    if path.parent != FOLDER or not path.is_file():
        raise web.HTTPNotFound()
    return path


def save_senders():
    recent = dict(list(senders.items())[-500:])
    try:
        SENDERS_PATH.write_text(json.dumps(recent), encoding="utf-8")
    except OSError:
        pass


def require_token(request):
    given = request.headers.get("X-Token", "")
    if not secrets.compare_digest(given, CONFIG["token"]):
        raise web.HTTPUnauthorized()


def require_local(request):
    """Desk routes: only this machine, and only when addressed as this machine."""
    host = (request.host or "").rsplit(":", 1)[0]
    if request.remote not in LOOPBACK or host not in ("127.0.0.1", "localhost"):
        raise web.HTTPForbidden()


def device_of(request):
    """Note which phone is talking to us. Returns (id, name)."""
    dev_id = request.headers.get("X-Device-Id", "")[:40]
    name = unquote(request.headers.get("X-Device-Name", "")).strip()[:40] or "Phone"
    if not dev_id:
        return "", name
    known = phones.get(dev_id)
    changed = known is None or known["name"] != name
    phones[dev_id] = {"name": name, "seen": time.monotonic()}
    if changed:
        if known is None:
            log(f"phone connected: {name}")
        asyncio.ensure_future(broadcast(presence()))
    return dev_id, name


def presence():
    return {"type": "phones", "phones": sorted(p["name"] for p in phones.values())}


async def broadcast(message):
    for ws in list(desks):
        try:
            await ws.send_json(message)
        except Exception:
            desks.discard(ws)


def notify(title, message):
    if tray is not None:
        try:
            tray.notify(message, title)
        except Exception:
            pass


def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" or n >= 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def copy_to_clipboard(paths):
    """Make what landed pasteable with Ctrl+V. Returns True if the clipboard now holds it."""
    if sys.platform != "win32" or not CONFIG["copy_to_clipboard"]:
        return False
    try:
        import clipboard

        formats, sequence = clipboard.copy_files(paths)
        if sequence is None:
            return False
        if len(paths) == 1 and kind_of(paths[0].name) == "image":
            threading.Thread(target=clipboard.add_picture, args=(paths[0], formats, sequence), daemon=True).start()
        return True
    except Exception as error:
        log(f"could not copy to the clipboard: {error}")
        return False


def announce(paths, device):
    """Tell the user something landed without interrupting them: the floating card, else a toast."""
    total = human_size(sum(p.stat().st_size for p in paths))
    copied = "Copied · " if copy_to_clipboard(paths) else ""
    if len(paths) == 1:
        title, click = paths[0].name, lambda: open_path(paths[0])
    else:
        title, click = f"{len(paths)} files", lambda: open_path(FOLDER)
    if card is not None:
        preview = next((p for p in paths if kind_of(p.name) == "image"), None)
        badge = paths[0].suffix.lstrip(".") or "file"
        kinds = {kind_of(p.name) for p in paths}
        kind = kinds.pop() if len(kinds) == 1 else "file"  # a mixed batch gets the neutral colour
        card.show(title, f"{copied}From {device} · {total}", preview=preview, badge=badge, on_click=click, kind=kind)
    elif not desks:
        notify(f"From {device}", f"{title} landed in {FOLDER.name}")


def open_path(path, reveal=False):
    if sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(path)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path.parent if reveal else path)])


# ---- phone routes -----------------------------------------------------------


async def index(request):
    # http://127.0.0.1:<desk_port>/?phone previews the phone page on the laptop.
    is_desk = request.remote in LOOPBACK and not request.secure and "phone" not in request.query
    return web.FileResponse(
        STATIC / ("desk.html" if is_desk else "phone.html"),
        headers={"Cache-Control": "no-store"},
    )


async def ping(request):
    """The phone's heartbeat: confirms pairing and keeps it listed as connected."""
    require_token(request)
    device_of(request)
    return web.json_response({"name": socket.gethostname()})


async def stage(request):
    """Receive one file into the staging area. It only lands when thrown."""
    require_token(request)
    device_of(request)
    name = clean_name(unquote(request.headers.get("X-Name", "file")))
    sid = secrets.token_urlsafe(9)
    path = STAGING / f"{sid}.part"
    size = 0
    try:
        with open(path, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_FILE_BYTES, actual_size=size)
                f.write(chunk)
        expected = request.headers.get("X-Size")
        if expected and expected.isdigit() and int(expected) != size:
            raise web.HTTPBadRequest(text="incomplete upload")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    staged[sid] = (path, name, time.monotonic())
    return web.json_response({"id": sid})


async def throw(request):
    require_token(request)
    _, device = device_of(request)
    data = await request.json()
    landed, paths = [], []
    for sid in data.get("ids", []):
        item = staged.pop(sid, None)
        if not item:
            continue
        path, name, _ = item
        dest = unique_path(FOLDER, name)
        os.replace(path, dest)
        senders[dest.name] = device
        landed.append(describe(dest))
        paths.append(dest)
        log(f"landed from {device}: {dest.name}")
    if landed:
        save_senders()
        try:
            power = min(1.0, max(0.0, float(data.get("power", 0.5))))
        except (TypeError, ValueError):
            power = 0.5
        await broadcast({"type": "drop", "files": landed, "power": power, "from": device})
        announce(paths, device)
    return web.json_response({"landed": [f["name"] for f in landed], "desk": socket.gethostname()})


async def discard(request):
    require_token(request)
    data = await request.json()
    for sid in data.get("ids", []):
        item = staged.pop(sid, None)
        if item:
            item[0].unlink(missing_ok=True)
    return web.json_response({"ok": True})


# ---- desk routes (this laptop only) -----------------------------------------


async def info(request):
    require_local(request)
    return web.json_response(
        {
            "url": request.app["phone_url"],
            "folder": str(FOLDER),
            "name": socket.gethostname(),
            "phones": presence()["phones"],
        }
    )


async def qr_svg(request):
    require_local(request)
    import qrcode

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(request.app["phone_url"])
    qr.make()
    matrix = qr.get_matrix()
    n = len(matrix)
    cells = "".join(
        f"M{x},{y}h1v1h-1z" for y, row in enumerate(matrix) for x, on in enumerate(row) if on
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
        f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{cells}" fill="#000"/></svg>'
    )
    return web.Response(text=svg, content_type="image/svg+xml", headers={"Cache-Control": "no-store"})


async def recent(request):
    require_local(request)
    files = [p for p in FOLDER.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return web.json_response({"files": [describe(p) for p in files[:60]]})


async def inbox(request):
    """Image previews for the desk page. Never serves anything a browser could run."""
    require_local(request)
    path = inbox_file(request.match_info["name"])
    if kind_of(path.name) != "image":
        raise web.HTTPNotFound()
    return web.FileResponse(
        path,
        headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"},
    )


async def open_file(request):
    require_local(request)
    data = await request.json()
    if data.get("name"):
        open_path(inbox_file(data["name"]), reveal=bool(data.get("reveal")))
    else:
        open_path(FOLDER)
    return web.json_response({"ok": True})


async def desk_socket(request):
    require_local(request)
    origin = request.headers.get("Origin", "")
    port = CONFIG["desk_port"]
    if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    desks.add(ws)
    await ws.send_json(presence())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        desks.discard(ws)
    return ws


# ---- keeping things healthy -------------------------------------------------


async def housekeeping(app, tls):
    """Drop phones that went quiet, expire unsent files, follow Wi-Fi address changes."""
    ip = app["ip"]
    tick = 0
    while True:
        await asyncio.sleep(3)
        tick += 1
        now = time.monotonic()

        gone = [d for d, p in phones.items() if now - p["seen"] > PHONE_TTL]
        for dev_id in gone:
            log(f"phone left: {phones.pop(dev_id)['name']}")
        if gone:
            await broadcast(presence())

        for sid, (path, _, at) in list(staged.items()):
            if now - at > STAGED_TTL:
                staged.pop(sid, None)
                path.unlink(missing_ok=True)

        if tick % 5 == 0:
            current = lan_ip()
            if current != ip and current != "127.0.0.1":
                try:
                    tls.load_cert_chain(*ensure_cert(current))
                except Exception as error:
                    log(f"could not switch certificate to {current}: {error}")
                    continue
                ip = app["ip"] = current
                app["phone_url"] = phone_url(ip)
                log(f"network address changed to {ip}")
                await broadcast({"type": "address"})
                notify("Flick moved to a new address", "Scan the QR code again to reconnect your phone.")


def phone_url(ip):
    return f"https://{ip}:{CONFIG['phone_port']}/#{CONFIG['token']}"


def print_qr(url):
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        pass  # consoles that can't draw block characters; the desk page has the QR too


# ---- tray icon and start-at-sign-in ------------------------------------------


def startup_shortcut():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Flick.lnk"


def windowless_python():
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return quiet if quiet.exists() else exe


def install_startup():
    env = {
        **os.environ,
        "FLICK_LNK": str(startup_shortcut()),
        "FLICK_EXE": str(windowless_python()),
        "FLICK_ARGS": f'"{ROOT / "server.py"}" --background --quiet',
        "FLICK_DIR": str(ROOT),
    }
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:FLICK_LNK);"
        "$s.TargetPath = $env:FLICK_EXE; $s.Arguments = $env:FLICK_ARGS;"
        "$s.WorkingDirectory = $env:FLICK_DIR; $s.WindowStyle = 7;"
        "$s.Description = 'Flick: receive files from your phone'; $s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        env=env, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def uninstall_startup():
    startup_shortcut().unlink(missing_ok=True)


def start_tray(loop, stop):
    """System-tray icon: proof Flick is running, and the way to open or quit it."""
    global tray
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        log("pystray/Pillow not installed: running without a tray icon")
        return

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(11, 13, 16, 255))
    draw.polygon([(32, 12), (50, 32), (38, 32), (38, 52), (26, 52), (26, 32), (14, 32)], fill=(124, 245, 196, 255))

    def toggle_startup(icon, item):
        uninstall_startup() if startup_shortcut().exists() else install_startup()

    def quit_flick(icon, item):
        icon.stop()
        loop.call_soon_threadsafe(stop.set)

    menu = pystray.Menu(
        pystray.MenuItem("Open Flick", lambda: webbrowser.open(DESK_URL), default=True),
        pystray.MenuItem("Open received files", lambda: open_path(FOLDER)),
        pystray.MenuItem("Pair a phone", lambda: webbrowser.open(DESK_URL + "?pair")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start when I sign in", toggle_startup, checked=lambda item: startup_shortcut().exists()),
        pystray.MenuItem("Quit Flick", quit_flick),
    )
    tray = pystray.Icon("Flick", image, "Flick: ready to receive", menu)
    threading.Thread(target=tray.run, daemon=True).start()


def spawn_background(open_page):
    """Relaunch hidden. python.exe is used (not pythonw) so the firewall rule you already allowed applies."""
    python = Path(sys.executable).with_name("python.exe")
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
        LOG_PATH.unlink()
    with open(LOG_PATH, "a", encoding="utf-8") as out:
        subprocess.Popen(
            [str(python if python.exists() else sys.executable), "-u", str(ROOT / "server.py"), "--hidden"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    if open_page:
        time.sleep(1.5)
        webbrowser.open(DESK_URL)


# ---- main -------------------------------------------------------------------


async def main(hidden, open_browser):
    FOLDER.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(exist_ok=True)
    for leftover in STAGING.glob("*.part"):
        leftover.unlink(missing_ok=True)
    if sys.platform == "win32":
        subprocess.run(["attrib", "+h", str(STAGING)], check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    if SENDERS_PATH.exists():
        try:
            senders.update(json.loads(SENDERS_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    ip = lan_ip()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(*ensure_cert(ip))

    app = web.Application()
    app["ip"] = ip
    app["phone_url"] = phone_url(ip)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/ping", ping),
            web.put("/api/stage", stage),
            web.post("/api/throw", throw),
            web.post("/api/discard", discard),
            web.get("/api/info", info),
            web.get("/api/qr.svg", qr_svg),
            web.get("/api/recent", recent),
            web.post("/api/open", open_file),
            web.get("/inbox/{name}", inbox),
            web.get("/ws", desk_socket),
        ]
    )

    # Phones abort the TLS handshake until the certificate warning is accepted; that is not an error.
    loop = asyncio.get_running_loop()

    def quiet_tls(loop, context):
        if not isinstance(context.get("exception"), (ssl.SSLError, ConnectionError)):
            loop.default_exception_handler(context)

    loop.set_exception_handler(quiet_tls)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", CONFIG["phone_port"], ssl_context=tls).start()
        await web.TCPSite(runner, "127.0.0.1", CONFIG["desk_port"]).start()
    except OSError:
        # The ports are taken, which almost always means Flick is already running.
        await runner.cleanup()
        log("Flick is already running.")
        if open_browser:
            webbrowser.open(DESK_URL)
        return

    if sys.platform == "win32" and CONFIG["landing_card"]:
        global card
        try:
            from overlay import Overlay

            card = Overlay()
        except Exception as error:
            log(f"landing card unavailable, falling back to notifications: {error}")

    stop = asyncio.Event()
    keeper = asyncio.create_task(housekeeping(app, tls))
    if hidden:
        start_tray(loop, stop)
        log(f"Flick started in the background. Files land in {FOLDER}")
    else:
        print("\nFlick is running.\n")
        print(f"  Files land in : {FOLDER}")
        print(f"  Laptop view   : {DESK_URL}")
        print(f"  Phone         : {app['phone_url']}\n")
        print("Scan this with your phone camera (same Wi-Fi as this laptop):\n")
        print_qr(app["phone_url"])
        print("\nPress Ctrl+C to stop.\n")
    if open_browser:
        webbrowser.open(DESK_URL)
    try:
        await stop.wait()
    finally:
        keeper.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flick files from your phone to this laptop.")
    parser.add_argument("--background", action="store_true", help="run hidden with a tray icon")
    parser.add_argument("--quiet", action="store_true", help="with --background: don't open the laptop page")
    parser.add_argument("--install", action="store_true", help="start hidden whenever you sign in")
    parser.add_argument("--uninstall", action="store_true", help="stop starting at sign-in")
    parser.add_argument("--no-browser", action="store_true", help="don't open the laptop page")
    parser.add_argument("--hidden", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.install:
        install_startup()
        print(f"Flick will start when you sign in.\n  {startup_shortcut()}")
    elif args.uninstall:
        uninstall_startup()
        print("Flick will no longer start when you sign in.")
    elif args.background:
        spawn_background(open_page=not args.quiet)
    else:
        try:
            asyncio.run(main(args.hidden, open_browser=not (args.hidden or args.no_browser)))
        except KeyboardInterrupt:
            print("\nStopped.")
