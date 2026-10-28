"""NexLauncher - launcher de Minecraft (vanilla) hecho desde cero.

Login con cuenta Microsoft (flujo device code) u offline, con sesión recordada.
Descarga versión, librerías, natives, assets y el Java oficial que pide cada versión.
Guarda el juego en %APPDATA%\\.minecraft (como el launcher oficial) y sus datos en %APPDATA%\\.nexlauncher.
Requiere Python 3.10+ y Pillow.
"""
import sys, os
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()
BASE_DIR = (Path(sys.argv[0]).resolve().parent if IS_FROZEN else Path(__file__).parent)
RES_DIR = (
    Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
    if IS_FROZEN else BASE_DIR
)


def ruta_recurso(nombre):
    """Recursos empaquetados con --add-data (logo, ícono): siempre por aquí, nunca BASE_DIR."""
    return RES_DIR / nombre


import subprocess
if not IS_FROZEN:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for _pkg, _mod in [("pillow==13.0.0", "PIL")]:
        try:
            __import__(_mod)
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", _pkg, "--quiet"],
                           check=True, creationflags=_NO_WINDOW)

import json, re, time, uuid, shutil, hashlib, zipfile, platform, threading, queue
import traceback, webbrowser, datetime, math, random, base64, io
import urllib.request, urllib.parse, urllib.error
import http.server, socketserver, socket, html
from concurrent.futures import ThreadPoolExecutor, as_completed

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("programacionbr.NexLauncher")
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont
from PIL import Image, ImageTk, ImageFilter, ImageDraw, ImageOps

APP_NAME = "NexLauncher"
APP_VERSION = "2.5"
FPS = 120  # cuadros por segundo de las animaciones
UA = f"NexLauncher/{APP_VERSION}"


# --- Carpetas: igual que el launcher oficial -------------------------------
def _carpeta_minecraft():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / ".minecraft"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "minecraft"
    return Path.home() / ".minecraft"


MC_DIR = _carpeta_minecraft()                 # %APPDATA%\.minecraft: versiones, librerías, saves, mods…
LAUNCHER_DIR = (Path(os.environ.get("APPDATA") or Path.home()) if os.name == "nt" else Path.home()) / ".nexlauncher"
DATA_DIR = MC_DIR
GAME_DIR = MC_DIR
VERSIONS_DIR = MC_DIR / "versions"
LIB_DIR = MC_DIR / "libraries"
ASSETS_DIR = MC_DIR / "assets"
RUNTIME_DIR = MC_DIR / "runtime"
ACCOUNTS_FILE = LAUNCHER_DIR / "accounts.json"  # lo propio del launcher va en %APPDATA%\.nexlauncher
CONFIG_FILE = LAUNCHER_DIR / "config.json"
LOG_FILE = LAUNCHER_DIR / "errores.log"


def log_error(texto):
    """Detalle técnico completo a %APPDATA%\\.nexlauncher\\errores.log."""
    linea = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {texto}\n"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linea)
    except OSError:
        sys.stderr.write(linea)


# --- URLs oficiales -------------------------------------------------------
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net"
JAVA_ALL_URL = ("https://piston-meta.mojang.com/v1/products/java-runtime/"
                "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
MS_DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
MS_SCOPE = "XboxLive.signin offline_access"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
AZURE_REVIEW_URL = "https://aka.ms/mce-reviewappid"

# --- Plataforma -----------------------------------------------------------
_IS64 = sys.maxsize > 2**32
_MACHINE = platform.machine().lower()
_ARM = _MACHINE in ("arm64", "aarch64")
OS_NAME = {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")
OS_ARCH = "x86" if not _IS64 else ("arm64" if _ARM else "x86_64")

if OS_NAME == "windows":
    JAVA_PLATFORM = "windows-arm64" if _ARM else ("windows-x64" if _IS64 else "windows-x86")
elif OS_NAME == "osx":
    JAVA_PLATFORM = "mac-os-arm64" if _ARM else "mac-os"
else:
    JAVA_PLATFORM = "linux-i386" if not _IS64 else "linux"

NOMBRE_OFFLINE_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


class LauncherError(Exception):
    """Error con mensaje ya pensado para mostrarse al usuario."""


class Cancelado(LauncherError):
    """El usuario canceló: no se muestra como error."""


class HttpError(Exception):
    def __init__(self, code, url, body):
        self.code, self.url, self.body = code, url, body
        try:
            self.json = json.loads(body) if body else {}
        except ValueError:
            self.json = {}
        super().__init__(f"HTTP {code} en {url}: {body[:300]}")


# --- HTTP -----------------------------------------------------------------
def http_json(url, data=None, headers=None, form=False, timeout=30):
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdr.update(headers)
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdr)
    host = urllib.parse.urlparse(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, url, e.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as e:
        raise LauncherError(f"Sin conexión con {host} ({e.reason}).") from e
    except (TimeoutError, OSError) as e:  # p. ej. el servidor dejó de responder a medio camino
        raise LauncherError(f"{host} tardó demasiado en responder ({e}).") from e
    try:
        return json.loads(raw) if raw else {}
    except ValueError as e:
        raise LauncherError(f"{host} respondió algo inesperado (¿red con portal de inicio de sesión?).") from e


def sha1_de(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def descargar(url, destino: Path, sha1=None, size=None, reintentos=5):
    """Descarga si falta o el tamaño no cuadra. Verifica SHA1 de lo recién bajado."""
    if destino.exists() and (size is None or destino.stat().st_size == size):
        return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".part")
    ultimo = None
    for intento in range(reintentos):
        if intento:
            time.sleep(min(2 ** intento, 15))  # espera creciente: 2, 4, 8, 15 s
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 16)
            if sha1 and sha1_de(tmp) != sha1:
                raise LauncherError(f"SHA1 incorrecto en {destino.name}")
            os.replace(tmp, destino)
            return True
        except (urllib.error.URLError, OSError, LauncherError) as e:
            ultimo = e
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    raise LauncherError(f"No se pudo descargar {destino.name}: {ultimo}")


def _pasada(lista, progreso, etiqueta, hilos):
    """Descarga en paralelo; devuelve [(trabajo, error)] de los que fallaron."""
    total, hechos, fallidos = len(lista), 0, []
    progreso(0, total, etiqueta)
    with ThreadPoolExecutor(max_workers=hilos) as pool:
        futuros = {pool.submit(descargar, *t): t for t in lista}
        for fut in as_completed(futuros):
            try:
                fut.result()
            except LauncherError as e:
                fallidos.append((futuros[fut], str(e)))
            hechos += 1
            if hechos % 20 == 0 or hechos == total:
                progreso(hechos, total, etiqueta)
    return fallidos


def descargar_lote(trabajos, progreso, etiqueta, hilos=8):
    """trabajos: lista de (url, destino, sha1, size). progreso(hechos, total, etiqueta).
    Los que fallen por red se reintentan en una segunda pasada con menos conexiones."""
    unicos = {}
    for t in trabajos:
        unicos.setdefault(str(t[1]), t)
    pendientes = []
    for t in unicos.values():
        try:
            listo = t[1].exists() and (t[3] is None or t[1].stat().st_size == t[3])
        except OSError:
            listo = False
        if not listo:
            pendientes.append(t)
    if not pendientes:
        progreso(len(unicos), len(unicos), etiqueta)
        return
    fallidos = _pasada(pendientes, progreso, etiqueta, hilos)
    if fallidos:
        time.sleep(3)
        fallidos = _pasada([t for t, _ in fallidos], progreso, f"{etiqueta} (reintento)", 2)
    if fallidos:
        for t, err in fallidos:
            log_error(f"Descarga fallida {t[0]}: {err}")
        raise LauncherError(
            f"No se pudieron descargar {len(fallidos)} archivos (el servidor no respondió).\n"
            "Revisa tu conexión y vuelve a darle a JUGAR: lo ya descargado no se repite.\n"
            f"Primer error: {fallidos[0][1]}")


# --- Cuentas --------------------------------------------------------------
def uuid_offline(nombre):
    """Mismo UUID que asigna un servidor con online-mode=false (UUID v3 de 'OfflinePlayer:<nombre>')."""
    h = bytearray(hashlib.md5(f"OfflinePlayer:{nombre}".encode("utf-8")).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(h)).hex


class MicrosoftAuth:
    def __init__(self, client_id):
        self.client_id = client_id

    def iniciar_device_code(self):
        return http_json(MS_DEVICE_URL, {"client_id": self.client_id, "scope": MS_SCOPE}, form=True)

    def esperar_token(self, device_code, intervalo, expira_en, cancelado: threading.Event):
        limite = time.time() + expira_en
        while time.time() < limite:
            if cancelado.wait(intervalo):
                raise Cancelado("Inicio de sesión cancelado.")
            try:
                return http_json(MS_TOKEN_URL, {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id, "device_code": device_code}, form=True)
            except HttpError as e:
                err = e.json.get("error")
                if err == "authorization_pending":
                    continue
                if err == "slow_down":
                    intervalo += 5
                    continue
                if err == "authorization_declined":
                    raise LauncherError("Rechazaste el inicio de sesión en la página de Microsoft.")
                if err == "expired_token":
                    break
                raise LauncherError(f"Microsoft rechazó el login: {e.json.get('error_description', err)}")
        raise LauncherError("El código expiró. Vuelve a intentarlo.")

    def refrescar(self, refresh_token):
        try:
            return http_json(MS_TOKEN_URL, {
                "grant_type": "refresh_token", "client_id": self.client_id,
                "refresh_token": refresh_token, "scope": MS_SCOPE}, form=True)
        except HttpError as e:
            raise LauncherError("La sesión de Microsoft expiró. Quita la cuenta y vuelve a iniciar sesión.") from e

    def login_minecraft(self, ms_access_token):
        xbl = http_json(XBL_URL, {
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                           "RpsTicket": f"d={ms_access_token}"},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
        try:
            xsts = http_json(XSTS_URL, {
                "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
                "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
        except HttpError as e:
            xerr = e.json.get("XErr")
            motivos = {
                2148916233: "Esta cuenta Microsoft no tiene perfil de Xbox. Entra una vez a xbox.com para crearlo.",
                2148916235: "Xbox Live no está disponible en el país de esta cuenta.",
                2148916236: "La cuenta requiere verificación de adulto (Corea).",
                2148916237: "La cuenta requiere verificación de adulto (Corea).",
                2148916238: "Cuenta de menor: un adulto debe agregarla a una familia de Microsoft.",
            }
            raise LauncherError(motivos.get(xerr, f"Xbox rechazó la cuenta (XErr {xerr}).")) from e
        uhs = xsts["DisplayClaims"]["xui"][0]["uhs"]
        try:
            mc = http_json(MC_LOGIN_URL, {"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
        except HttpError as e:
            if e.code == 403:
                raise LauncherError(
                    "Tu app de Azure aún no tiene permiso para la API de Minecraft.\n"
                    f"Solicítalo en {AZURE_REVIEW_URL} y espera la aprobación.") from e
            raise
        try:
            perfil = http_json(MC_PROFILE_URL, headers={"Authorization": f"Bearer {mc['access_token']}"})
        except HttpError as e:
            if e.code == 404:
                raise LauncherError("Esta cuenta Microsoft no tiene Minecraft: Java Edition.") from e
            raise
        return {"name": perfil["name"], "uuid": perfil["id"], "mc_token": mc["access_token"],
                "mc_expires": time.time() + int(mc.get("expires_in", 86400))}




# --- Login directo en el navegador (authorization code + PKCE) -------------------
MS_AUTH_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _logo_b64():
    try:
        img = Image.open(ruta_recurso("logo.png")).convert("RGBA").resize((144, 144), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except OSError as e:
        log_error(f"No se pudo cargar el logo para la página del navegador: {e!r}")
        return ""


def pagina_resultado(ok, titulo, texto, logo_b64=""):
    logo = f'<img class="l" src="data:image/png;base64,{logo_b64}" alt="">' if logo_b64 else ""
    icono = "✓" if ok else "!"
    fondo_i, txt_i = ("#d9d9de", "#0b0b0c") if ok else ("#35353b", "#f4f4f5")
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>NexLauncher</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{{color-scheme:dark}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0c;color:#f4f4f5;
font-family:"Segoe UI",system-ui,sans-serif;overflow:hidden}}
.c{{position:relative;text-align:center;padding:36px 48px 40px;background:#18181b;border:1px solid #2c2c31;
border-radius:22px;max-width:420px;animation:in .7s cubic-bezier(.2,.9,.3,1.15) both}}
.l{{width:112px;height:112px;display:block;margin:0 auto 6px;animation:flota 3.2s ease-in-out infinite;
filter:drop-shadow(0 0 22px rgba(255,255,255,.18))}}
.i{{width:46px;height:46px;margin:0 auto 14px;border-radius:50%;display:grid;place-items:center;font-size:22px;
font-weight:700;background:{fondo_i};color:{txt_i};animation:pop .55s .35s cubic-bezier(.3,1.7,.5,1) both}}
h1{{font-size:22px;margin:0 0 8px}}p{{margin:0;color:#8a8a93;font-size:14px;line-height:1.55}}
.p{{position:fixed;width:8px;height:8px;background:#2a2a2f;bottom:-12px;animation:sube linear infinite}}
@keyframes in{{from{{opacity:0;transform:translateY(26px) scale(.97)}}}}
@keyframes pop{{from{{transform:scale(0)}}}}
@keyframes flota{{50%{{transform:translateY(-8px)}}}}
@keyframes sube{{to{{transform:translateY(-110vh)}}}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}}}
</style></head><body>
<div class="p" style="left:12%;animation-duration:14s"></div><div class="p" style="left:28%;animation-duration:19s;animation-delay:-6s"></div>
<div class="p" style="left:47%;animation-duration:16s;animation-delay:-3s"></div><div class="p" style="left:66%;animation-duration:21s;animation-delay:-9s"></div>
<div class="p" style="left:84%;animation-duration:15s;animation-delay:-12s"></div>
<div class="c">{logo}<div class="i">{icono}</div><h1>{html.escape(titulo)}</h1><p>{html.escape(texto)}</p></div>
</body></html>"""


class _ServidorLocal(http.server.HTTPServer):
    allow_reuse_address = False  # que ningún otro proceso pueda compartir el puerto

    def server_bind(self):
        # Se salta getfqdn() de HTTPServer, que en Windows puede tardar varios segundos.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


class _ServidorLocal6(_ServidorLocal):
    address_family = socket.AF_INET6


class LoginNavegador:
    """Abre la página de Microsoft en el navegador y recibe el resultado en http://localhost.
    Cliente público con PKCE (sin secreto). En Azure: plataforma 'Aplicaciones móviles y de
    escritorio' con el URI de redirección http://localhost (cualquier puerto)."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.verifier = _b64url(os.urandom(48))
        reto = _b64url(hashlib.sha256(self.verifier.encode("ascii")).digest())
        self.state = _b64url(os.urandom(18))
        self.resultado, self.listo = {}, threading.Event()
        self.logo = _logo_b64()
        self.servidores = self._abrir_servidores()
        self.redirect = f"http://localhost:{self.puerto}"
        self.url = MS_AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": self.redirect,
            "response_mode": "query", "scope": MS_SCOPE, "state": self.state,
            "code_challenge": reto, "code_challenge_method": "S256", "prompt": "select_account"})

    def _abrir_servidores(self):
        login = self

        class Manejador(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" not in q and "error" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                if q.get("state", [""])[0] != login.state:
                    ok, titulo, texto = False, "Solicitud no válida", \
                        "Esta respuesta no corresponde al inicio de sesión de NexLauncher. Vuelve a intentarlo."
                elif "error" in q:
                    login.resultado = {"error": q["error"][0], "desc": q.get("error_description", [""])[0]}
                    ok, titulo, texto = False, "No se pudo iniciar sesión", "Vuelve a NexLauncher para ver el detalle."
                    login.listo.set()
                else:
                    login.resultado = {"code": q["code"][0]}
                    ok, titulo, texto = True, "¡Sesión iniciada!", "Ya puedes cerrar esta pestaña y volver a NexLauncher."
                    login.listo.set()
                cuerpo = pagina_resultado(ok, titulo, texto, login.logo).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(cuerpo)

        srv4 = _ServidorLocal(("127.0.0.1", 0), Manejador)
        self.puerto = srv4.server_address[1]
        servidores = [srv4]
        try:  # algunos navegadores resuelven "localhost" a ::1 primero
            servidores.append(_ServidorLocal6(("::1", self.puerto), Manejador))
        except OSError:
            pass
        for s in servidores:
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True).start()
        return servidores

    def cerrar(self):
        for s in self.servidores:
            try:
                s.shutdown()
                s.server_close()
            except OSError:
                pass

    def esperar(self, cancelado, limite=600):
        fin = time.time() + limite
        try:
            while not self.listo.wait(0.25):
                if cancelado.is_set():
                    raise Cancelado("Inicio de sesión cancelado.")
                if time.time() > fin:
                    raise LauncherError("Se agotó el tiempo para iniciar sesión en el navegador.")
        finally:
            self.cerrar()
        if "error" in self.resultado:
            err, desc = self.resultado["error"], self.resultado["desc"]
            if err == "access_denied":
                raise LauncherError("Cancelaste el inicio de sesión en la página de Microsoft.")
            raise LauncherError(f"Microsoft rechazó el inicio de sesión: {desc or err}")
        try:
            return http_json(MS_TOKEN_URL, {
                "client_id": self.client_id, "grant_type": "authorization_code",
                "code": self.resultado["code"], "redirect_uri": self.redirect,
                "code_verifier": self.verifier, "scope": MS_SCOPE}, form=True)
        except HttpError as e:
            raise LauncherError(f"No se pudo completar el inicio de sesión: "
                                f"{e.json.get('error_description', e)}") from e


# --- Cifrado de tokens guardados (DPAPI de Windows) -------------------------
# Los tokens quedan ligados a tu usuario de Windows: si copias la carpeta a otra
# PC, solo tendrás que volver a iniciar sesión con Microsoft.
if os.name == "nt":
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    for _fn in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
        _fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        _fn.restype = wintypes.BOOL

    def _dpapi(datos: bytes, cifrar_: bool) -> bytes:
        buf = ctypes.create_string_buffer(datos, len(datos))
        entrada = _Blob(len(datos), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        salida = _Blob()
        fn = _crypt32.CryptProtectData if cifrar_ else _crypt32.CryptUnprotectData
        if not fn(ctypes.byref(entrada), None, None, None, None, 0x01, ctypes.byref(salida)):
            raise OSError(ctypes.get_last_error(), "DPAPI falló")
        try:
            return ctypes.string_at(salida.pbData, salida.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(salida.pbData, ctypes.c_void_p))


def cifrar(texto):
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(texto.encode("utf-8"), True)).decode("ascii")
        except Exception as e:
            log_error(f"DPAPI no disponible, el token se guarda sin cifrar: {e!r}")
    return texto


def descifrar(texto):
    if not isinstance(texto, str):
        return ""
    if not texto.startswith("dpapi:"):
        return texto
    if os.name != "nt":
        return ""
    try:
        return _dpapi(base64.b64decode(texto[6:]), False).decode("utf-8")
    except Exception as e:
        log_error(f"No se pudo descifrar un token guardado (¿otra PC/usuario?): {e!r}")
        return ""


SECRETOS = ("refresh_token", "mc_token")


class Cuentas:
    def __init__(self):
        self.lista = []
        self._lock = threading.Lock()
        try:
            datos = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            datos = []
        except (ValueError, OSError) as e:
            log_error(f"accounts.json ilegible, se ignora: {e!r}")
            datos = []
        for c in datos if isinstance(datos, list) else []:
            if not isinstance(c, dict) or not {"type", "name", "uuid"} <= c.keys():
                continue
            for k in SECRETOS:
                if k in c:
                    c[k] = descifrar(c[k])
            self.lista.append(c)

    @staticmethod
    def clave(c):
        return f"{c['type']}:{c['uuid']}"

    def buscar(self, clave):
        return next((c for c in self.lista if self.clave(c) == clave), None)

    def guardar(self):
        with self._lock:
            datos = []
            for c in self.lista:
                d = dict(c)
                for k in SECRETOS:
                    if d.get(k):
                        d[k] = cifrar(d[k])
                datos.append(d)
            try:
                ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = ACCOUNTS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(datos, indent=2), encoding="utf-8")
                os.replace(tmp, ACCOUNTS_FILE)
            except OSError as e:
                log_error(f"No se pudo guardar accounts.json: {e!r}")

    def agregar(self, cuenta):
        """Agrega o actualiza; la más reciente queda al final."""
        previa = self.buscar(self.clave(cuenta))
        if previa is not None:
            self.lista.remove(previa)
        self.lista.append(cuenta)
        self.guardar()
        return cuenta

    def quitar(self, cuenta):
        if cuenta in self.lista:
            self.lista.remove(cuenta)
            self.guardar()


def renovar_sesion(cuenta, client_id):
    """Renueva el token de Minecraft si vence en <5 min. True si cambió algo."""
    if cuenta["type"] != "microsoft" or time.time() < cuenta.get("mc_expires", 0) - 300:
        return False
    if not client_id:
        raise LauncherError("Falta el Client ID de Azure para renovar la sesión de Microsoft.")
    if not cuenta.get("refresh_token"):
        raise LauncherError("La sesión guardada ya no es válida. Inicia sesión otra vez con Microsoft.")
    auth = MicrosoftAuth(client_id)
    try:
        tok = auth.refrescar(cuenta["refresh_token"])
        cuenta.update(auth.login_minecraft(tok["access_token"]))
    except (HttpError, KeyError) as e:
        raise LauncherError(f"Microsoft no respondió como se esperaba ({e}).") from e
    cuenta["refresh_token"] = tok.get("refresh_token", cuenta["refresh_token"])
    return True


# --- Instalación de versiones ---------------------------------------------
def reglas_ok(reglas, features=None):
    if not reglas:
        return True
    permitido = False
    for r in reglas:
        coincide = True
        o = r.get("os", {})
        if "name" in o and o["name"] != OS_NAME:
            coincide = False
        if "arch" in o and _ARCH_ALIAS.get(str(o["arch"]).lower(), str(o["arch"]).lower()) != MI_ARCH:
            coincide = False
        for k, v in r.get("features", {}).items():
            if (features or {}).get(k, False) != v:
                coincide = False
        if coincide:
            permitido = r.get("action") == "allow"
    return permitido


def ruta_maven(nombre):
    ext = "jar"
    if "@" in nombre:
        nombre, ext = nombre.split("@", 1)
    p = nombre.split(":")
    grupo, artefacto, version = p[0], p[1], p[2]
    clasif = f"-{p[3]}" if len(p) > 3 else ""
    return f"{grupo.replace('.', '/')}/{artefacto}/{version}/{artefacto}-{version}{clasif}.{ext}"


_ARCH_ALIAS = {"": "x64", "x64": "x64", "x86_64": "x64", "amd64": "x64",
               "arm64": "arm64", "aarch64": "arm64", "x86": "x86", "i386": "x86", "32": "x86"}
_OS_ALIAS = {"windows": "windows", "linux": "linux", "macos": "osx", "osx": "osx"}
MI_ARCH = "arm64" if _ARM else ("x64" if _IS64 else "x86")


def native_es_mio(clasif):
    """'natives-windows', 'natives-windows-x64', 'natives-windows-arm64', 'natives-macos'...
    Acepta cualquier forma de nombrar la arquitectura, no solo la de 1.19-1.21."""
    so, _, arch = clasif[len("natives-"):].partition("-")
    return _OS_ALIAS.get(so) == OS_NAME and _ARCH_ALIAS.get(arch, arch) == MI_ARCH


def clasif_native(nombre, ruta=""):
    """Devuelve 'natives-<so>[-<arch>]' si la librería es un jar de natives, si no ''.
    Cubre el clasificador clásico (org.lwjgl:lwjgl:3.4.3:natives-windows) y el nombre
    con el sufijo en el artefacto (org.lwjgl:lwjgl-glfw-natives-windows:3.4.3 o el path del jar)."""
    nombre = nombre.split("@")[0]
    p = nombre.split(":")
    if len(p) > 3 and p[3].startswith("natives-"):
        return p[3]
    for texto in (p[1] if len(p) > 1 else "", Path(ruta).stem if ruta else ""):
        m = re.search(r"(?:^|[-_])(natives-(?:windows|linux|macos|osx)(?:-[a-z0-9_]+)?)$", texto)
        if m:
            return m.group(1)
    return ""


def recolectar_librerias(vjson):
    """Devuelve (trabajos_descarga, classpath, jars_de_natives).
    Los natives nuevos (1.19+) van también al classpath: LWJGL los busca ahí."""
    trabajos, cp, natives = [], [], []
    for lib in vjson.get("libraries", []):
        if not reglas_ok(lib.get("rules")):
            continue
        dl = lib.get("downloads")
        art_path = ((dl or {}).get("artifact") or {}).get("path", "")
        clasif = clasif_native(lib["name"], art_path)
        if clasif and not native_es_mio(clasif):
            continue
        ruta = None
        if dl is None:  # formato viejo: solo nombre maven + url base
            rel = ruta_maven(lib["name"])
            ruta = LIB_DIR / rel
            base = lib.get("url", "https://libraries.minecraft.net/").rstrip("/")
            trabajos.append((f"{base}/{rel}", ruta, None, None))
        elif dl.get("artifact") and dl["artifact"].get("path"):
            a = dl["artifact"]
            ruta = LIB_DIR / a["path"]
            if a.get("url"):  # sin url = lo genera el instalador de Forge/NeoForge
                trabajos.append((a["url"], ruta, a.get("sha1"), a.get("size")))
        if ruta:
            cp.append(ruta)
            if clasif:
                natives.append(ruta)
        # Natives estilo <1.19: clave "natives" + "classifiers"
        nat = lib.get("natives", {})
        if OS_NAME in nat and dl:
            clave = nat[OS_NAME].replace("${arch}", "64" if _IS64 else "32")
            c = dl.get("classifiers", {}).get(clave)
            if c:
                p = LIB_DIR / c["path"]
                trabajos.append((c["url"], p, c.get("sha1"), c.get("size")))
                natives.append(p)
    return trabajos, cp, natives


def subcarpeta_natives(vjson):
    """Minecraft 26.x pasa -Djava.library.path=${natives_directory}/java: los .dll deben estar ahí.
    Lee la subcarpeta del propio JSON ('' en versiones viejas, 'java' en 26.x)."""
    prefijo = "-Djava.library.path=${natives_directory}"
    for a in vjson.get("arguments", {}).get("jvm", []):
        valores = [a] if isinstance(a, str) else (a.get("value") if isinstance(a, dict) else [])
        for v in ([valores] if isinstance(valores, str) else valores or []):
            if isinstance(v, str) and v.startswith(prefijo):
                return v[len(prefijo):].strip("/\\")
    return ""


def extraer_natives(jars, destino: Path, sub=""):
    """Extrae .dll/.so/.dylib a una carpeta plana (destino/sub, y también destino por seguridad).
    Devuelve cuántas extrajo."""
    if destino.exists():
        shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir(parents=True, exist_ok=True)
    carpetas = [destino] + ([destino / sub] if sub else [])
    for c in carpetas:
        c.mkdir(parents=True, exist_ok=True)
    for jar in jars:
        with zipfile.ZipFile(jar) as z:
            for nombre in z.namelist():
                if nombre.startswith("META-INF/") or nombre.endswith("/"):
                    continue
                if nombre.lower().endswith((".dll", ".so", ".dylib", ".jnilib")):
                    datos = z.read(nombre)
                    for c in carpetas:
                        (c / Path(nombre).name).write_bytes(datos)
    return sum(1 for f in (destino / sub if sub else destino).iterdir() if f.is_file())


def preparar_assets(vjson, progreso):
    """Descarga assets; devuelve la carpeta a usar como ${game_assets}."""
    idx = vjson["assetIndex"]
    idx_path = ASSETS_DIR / "indexes" / f"{idx['id']}.json"
    descargar(idx["url"], idx_path, idx.get("sha1"), idx.get("size"))
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except ValueError:
        idx_path.unlink(missing_ok=True)  # archivo dañado: se vuelve a bajar
        descargar(idx["url"], idx_path, idx.get("sha1"), idx.get("size"))
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    objetos = data.get("objects", {})
    trabajos = []
    for o in objetos.values():
        h = o["hash"]
        trabajos.append((f"{RESOURCES_URL}/{h[:2]}/{h}", ASSETS_DIR / "objects" / h[:2] / h, h, o["size"]))
    descargar_lote(trabajos, progreso, "Assets")

    # Versiones viejas (<1.7.10) leen los assets por nombre, no por hash
    if data.get("virtual") or data.get("map_to_resources"):
        destino = (GAME_DIR / "resources") if data.get("map_to_resources") else (ASSETS_DIR / "virtual" / idx["id"])
        for nombre, o in objetos.items():
            dst = destino / nombre
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ASSETS_DIR / "objects" / o["hash"][:2] / o["hash"], dst)
        if data.get("virtual"):
            return ASSETS_DIR / "virtual" / idx["id"]
    return ASSETS_DIR


def asegurar_java(componente, progreso, log):
    """Descarga el runtime de Java oficial de Mojang que pide la versión."""
    home = RUNTIME_DIR / componente
    exe = home / "bin" / ("javaw.exe" if OS_NAME == "windows" else "java")
    marca = home / ".completo"
    if marca.exists() and exe.exists():
        return exe
    try:
        todos = http_json(JAVA_ALL_URL)
        entradas = todos.get(JAVA_PLATFORM, {}).get(componente) or []
        if not entradas:
            raise LauncherError(f"Mojang no publica '{componente}' para {JAVA_PLATFORM}.")
        manifest = http_json(entradas[0]["manifest"]["url"])
        trabajos, ejecutables, enlaces = [], [], []
        for rel, f in manifest["files"].items():
            p = home / rel
            if f["type"] == "directory":
                p.mkdir(parents=True, exist_ok=True)
            elif f["type"] == "file":
                raw = f["downloads"]["raw"]
                trabajos.append((raw["url"], p, raw["sha1"], raw["size"]))
                if f.get("executable"):
                    ejecutables.append(p)
            elif f["type"] == "link":
                enlaces.append((p, f["target"]))
        descargar_lote(trabajos, progreso, f"Java ({componente})")
        if OS_NAME != "windows":
            for p in ejecutables:
                p.chmod(0o755)
            for p, objetivo in enlaces:
                if not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(objetivo, p)
        marca.write_text("ok")
        return exe
    except (LauncherError, HttpError, KeyError) as e:
        log_error(f"Fallo al descargar Java {componente}: {e!r}")
        sistema = shutil.which("javaw") or shutil.which("java")
        if sistema:
            log(f"[AVISO] No se pudo bajar Java {componente} ({e}). Se usará el Java del sistema: {sistema}")
            return Path(sistema)
        raise LauncherError(f"No se pudo conseguir Java {componente}: {e}") from e


def construir_comando(vjson, java, cuenta, ram_mb, natives_dir, classpath, game_assets, log_cfg, client_id,
                      game_dir=None):
    variables = {
        "auth_player_name": cuenta["name"],
        "version_name": vjson["id"],
        "game_directory": str(game_dir or GAME_DIR),
        "assets_root": str(ASSETS_DIR),
        "game_assets": str(game_assets),
        "assets_index_name": vjson["assetIndex"]["id"],
        "auth_uuid": cuenta["uuid"],
        "auth_access_token": cuenta.get("mc_token") or "0",
        "auth_session": cuenta.get("mc_token") or "0",
        "clientid": client_id or "0",
        "auth_xuid": "0",
        "user_type": "msa" if cuenta["type"] == "microsoft" else "legacy",
        "version_type": vjson.get("type", "release"),
        "user_properties": "{}",
        "natives_directory": str(natives_dir),
        "launcher_name": "NexLauncher",
        "launcher_version": APP_VERSION,
        "classpath": os.pathsep.join(str(p) for p in classpath),
        "classpath_separator": os.pathsep,
        "library_directory": str(LIB_DIR),
    }

    def sub(texto):
        return re.sub(r"\$\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), texto)

    def expandir(args):
        salida = []
        for a in args:
            if isinstance(a, str):
                salida.append(sub(a))
            elif reglas_ok(a.get("rules")):
                v = a["value"]
                salida.extend(sub(x) for x in (v if isinstance(v, list) else [v]))
        return salida

    jvm = [f"-Xmx{ram_mb}M", f"-Xms{min(512, ram_mb)}M",
           # G1 afinado como el launcher oficial: menos tirones por recolección de basura
           "-XX:+UnlockExperimentalVMOptions", "-XX:+UseG1GC", "-XX:G1NewSizePercent=20",
           "-XX:G1ReservePercent=20", "-XX:MaxGCPauseMillis=50", "-XX:G1HeapRegionSize=32M"]
    if "arguments" in vjson:
        jvm += expandir(vjson["arguments"].get("jvm", []))
        juego = expandir(vjson["arguments"].get("game", []))
    else:
        jvm += [f"-Djava.library.path={natives_dir}", "-cp", variables["classpath"]]
        juego = [sub(x) for x in vjson.get("minecraftArguments", "").split()]
    # Asegura que la JVM y LWJGL vean la carpeta de natives aunque el JSON no lo pida
    if not any(a.startswith("-Djava.library.path=") for a in jvm):
        jvm.append(f"-Djava.library.path={natives_dir}")
    if not any(a.startswith("-Dorg.lwjgl.librarypath=") for a in jvm):
        jvm.append(f"-Dorg.lwjgl.librarypath={natives_dir}")
    if log_cfg:
        arg, ruta = log_cfg
        jvm.append(arg.replace("${path}", str(ruta)))
    return [str(java), *jvm, vjson["mainClass"], *juego]




def http_texto(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, url, e.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as e:
        raise LauncherError(f"Sin conexión con {urllib.parse.urlparse(url).netloc} ({e.reason}).") from e


# --- Versiones con herencia (Fabric / Forge / NeoForge) ------------------------
def _clave_lib(nombre):
    """grupo:artefacto[:clasificador], sin versión: así la del loader reemplaza a la de vanilla."""
    p = nombre.split("@")[0].split(":")
    return ":".join(p[:2] + p[3:4])


def fusionar(hijo, padre):
    r = dict(padre)
    for k, v in hijo.items():
        if k not in ("libraries", "arguments", "inheritsFrom"):
            r[k] = v
    libs, vistos = [], set()
    for lib in hijo.get("libraries", []) + padre.get("libraries", []):
        k = _clave_lib(lib["name"])
        if k not in vistos:
            vistos.add(k)
            libs.append(lib)
    r["libraries"] = libs
    if "arguments" in hijo or "arguments" in padre:
        ah, ap = hijo.get("arguments", {}), padre.get("arguments", {})
        r["arguments"] = {"game": ap.get("game", []) + ah.get("game", []),
                          "jvm": ap.get("jvm", []) + ah.get("jvm", [])}
    r["id"] = hijo["id"]
    r["_base"] = padre.get("_base", padre["id"])  # versión vanilla de la que sale el client.jar
    return r


def _json_local_o_remoto(version, manifest):
    local = VERSIONS_DIR / version / f"{version}.json"
    if local.exists():
        try:
            datos = json.loads(local.read_text(encoding="utf-8"))
            entrada = manifest and next((v for v in manifest["versions"] if v["id"] == version), None)
            # JSON de una versión de Mojang que no coincide con el oficial (otro launcher lo reescribió,
            # quedó incompleto o sin librerías): se descarta y se baja el original.
            malo = entrada and (
                (entrada.get("sha1") and sha1_de(local) != entrada["sha1"])
                or (not datos.get("libraries") and "inheritsFrom" not in datos))
            if not malo:
                return datos
            log_error(f"{local} no coincide con el oficial de Mojang; se vuelve a descargar.")
            local.unlink(missing_ok=True)
        except (ValueError, OSError) as e:
            log_error(f"{local} dañado: {e!r}")
            entrada = manifest and next((v for v in manifest["versions"] if v["id"] == version), None)
            if not entrada:
                raise LauncherError(f"Los datos de la versión {version} están dañados. Bórrala de "
                                    f"{VERSIONS_DIR} y vuelve a instalarla.") from e
            local.unlink(missing_ok=True)
    if not manifest:
        raise LauncherError("Sin conexión y esa versión todavía no está instalada.")
    entrada = next((v for v in manifest["versions"] if v["id"] == version), None)
    if not entrada:
        raise LauncherError(f"La versión {version} no existe en la lista de Mojang.")
    descargar(entrada["url"], local, entrada.get("sha1"))
    return json.loads(local.read_text(encoding="utf-8"))


def json_version(version, manifest, profundidad=0):
    datos = _json_local_o_remoto(version, manifest)
    if "inheritsFrom" in datos:
        if profundidad > 5:
            raise LauncherError("La versión tiene una cadena de herencia demasiado larga.")
        datos = fusionar(datos, json_version(datos["inheritsFrom"], manifest, profundidad + 1))
    return datos


def carpeta_juego(tipo, mc):
    """Como un Minecraft normal: todo juega desde .minecraft (saves, mods, resourcepacks…)."""
    return GAME_DIR


def instalar_version(version, manifest, progreso, log, estado):
    """Deja todo listo para arrancar. Devuelve lo que necesita construir_comando."""
    estado("Leyendo datos de la versión…")
    vjson = json_version(version, manifest)

    estado("Preparando Java…")
    java = asegurar_java(vjson.get("javaVersion", {}).get("component", "jre-legacy"), progreso, log)
    log(f"Java: {java}")

    base = vjson.get("_base", vjson["id"])
    cliente = vjson["downloads"]["client"]
    jar_base = VERSIONS_DIR / base / f"{base}.jar"
    trabajos, cp, natives = recolectar_librerias(vjson)
    trabajos.append((cliente["url"], jar_base, cliente.get("sha1"), cliente.get("size")))
    log_cfg = None
    lc = vjson.get("logging", {}).get("client")
    if lc and lc.get("file"):
        f = lc["file"]
        ruta_log = ASSETS_DIR / "log_configs" / f["id"]
        trabajos.append((f["url"], ruta_log, f.get("sha1"), f.get("size")))
        log_cfg = (lc["argument"], ruta_log)  # incluye el parche de seguridad Log4Shell en versiones viejas
    log(f"Librerías en el JSON: {len(vjson.get('libraries', []))} · en uso: {len(cp)}")
    if not cp:
        raise LauncherError(f"La versión {version} no trae librerías válidas para este sistema. "
                            f"Borra la carpeta {VERSIONS_DIR / version} y vuelve a darle a JUGAR.")
    estado("Descargando librerías…")
    descargar_lote(trabajos, progreso, "Librerías")
    faltan = [p.name for p in cp if not p.exists()]
    if faltan:
        raise LauncherError(f"Faltan {len(faltan)} librerías del loader (p. ej. {faltan[0]}). "
                            "Vuelve a darle a JUGAR para reinstalarlo.")
    estado("Descargando assets…")
    game_assets = preparar_assets(vjson, progreso)

    # Forge/NeoForge esperan el client.jar con el nombre de su propia versión (ver -DignoreList)
    jar = VERSIONS_DIR / version / f"{version}.jar"
    if jar != jar_base and (not jar.exists() or jar.stat().st_size != jar_base.stat().st_size):
        jar.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(jar_base, jar)

    natives_dir = VERSIONS_DIR / version / "natives"
    sub = subcarpeta_natives(vjson)
    firma = hashlib.sha1(("|".join(f"{p.name}:{p.stat().st_size}" for p in natives) + f"#{sub}").encode()).hexdigest()
    marca = natives_dir / ".nexlauncher"
    carpeta_dll = natives_dir / sub if sub else natives_dir
    if marca.exists() and marca.read_text() == firma and carpeta_dll.is_dir():
        n = sum(1 for p in carpeta_dll.iterdir() if p.is_file() and p.name != ".nexlauncher")
    else:
        n = extraer_natives(natives, natives_dir, sub)
        marca.write_text(firma)
    log(f"Natives: {len(natives)} jars, {n} librerías nativas.")
    if n == 0:
        log("[AVISO] No se extrajo ninguna librería nativa; el juego probablemente no abrirá.", "warn")
    classpath = list(dict.fromkeys([*cp, jar]))
    return vjson, java, classpath, natives_dir, game_assets, log_cfg


# --- Listas de versiones de cada loader ---------------------------------------
FABRIC_META = "https://meta.fabricmc.net/v2"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEO_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NEO_API = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
CARGADORES = {"vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge"}


def clave_num(texto):
    """Orden numérico de versiones ('1.20.10' > '1.20.9'); las beta van detrás de su estable."""
    return tuple(int(x) for x in re.findall(r"\d+", texto.split("-beta")[0])) + (0 if "beta" in texto else 1,)


def mc_de_neoforge(v):
    """21.1.72 -> 1.21.1 · 20.4.80-beta -> 1.20.4 · 21.0.3 -> 1.21 · 26.1.0.10-beta -> 26.1 · 26.1.1.3 -> 26.1.1"""
    p = v.split("-")[0].split(".")
    if len(p) >= 4:
        return f"{p[0]}.{p[1]}" + ("" if p[2] == "0" else f".{p[2]}")
    if len(p) == 3:
        return f"1.{p[0]}" + ("" if p[1] == "0" else f".{p[1]}")
    return None


def listar_cargador(tipo):
    """{'mcs': [...], 'por_mc': {mc: [(etiqueta, id_instalador)]}, 'recomendado': {mc: id}}"""
    por_mc, recomendado = {}, {}
    if tipo == "fabric":
        juegos = http_json(f"{FABRIC_META}/versions/game")
        loaders = http_json(f"{FABRIC_META}/versions/loader")
        lista = [(l["version"] + ("" if l.get("stable") else " (beta)"), l["version"]) for l in loaders]
        estable = next((l["version"] for l in loaders if l.get("stable")), lista[0][1] if lista else None)
        mcs = [g["version"] for g in juegos if g.get("stable")]
        for mc in mcs:
            por_mc[mc] = lista
            recomendado[mc] = estable
        return {"mcs": mcs, "por_mc": por_mc, "recomendado": recomendado}
    if tipo == "forge":
        xml = http_texto(f"{FORGE_MAVEN}/maven-metadata.xml")
        for v in re.findall(r"<version>([^<]+)</version>", xml):
            mc, _, build = v.partition("-")
            if build and clave_num(mc) >= (1, 7, 10):
                por_mc.setdefault(mc, []).append((build, v))
        try:
            promos = http_json(FORGE_PROMOS).get("promos", {})
        except (LauncherError, HttpError) as e:
            log_error(f"Sin promociones de Forge: {e!r}")
            promos = {}
        for mc, lista in por_mc.items():
            lista.sort(key=lambda x: clave_num(x[0]), reverse=True)
            elegido = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
            recomendado[mc] = next((i for b, i in lista if elegido and b.startswith(elegido)), lista[0][1])
            for k, (b, i) in enumerate(lista):
                if i == recomendado[mc]:
                    lista[k] = (b + "  ★", i)
    elif tipo == "neoforge":
        for v in http_json(NEO_API).get("versions", []):
            mc = mc_de_neoforge(v)
            if mc:
                por_mc.setdefault(mc, []).append((v, v))
        for mc, lista in por_mc.items():
            lista.sort(key=lambda x: clave_num(x[0]), reverse=True)
            recomendado[mc] = next((i for b, i in lista if "beta" not in b), lista[0][1])
    else:
        raise LauncherError(f"Loader desconocido: {tipo}")
    mcs = sorted(por_mc, key=clave_num, reverse=True)
    return {"mcs": mcs, "por_mc": por_mc, "recomendado": recomendado}


# --- Instalación de loaders -----------------------------------------------------
def _escribir_json(vid, datos):
    destino = VERSIONS_DIR / vid / f"{vid}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(datos, indent=2), encoding="utf-8")


def instalar_fabric(mc, loader):
    vid = f"fabric-loader-{loader}-{mc}"
    if (VERSIONS_DIR / vid / f"{vid}.json").exists():
        return vid
    perfil = http_json(f"{FABRIC_META}/versions/loader/{urllib.parse.quote(mc)}/{urllib.parse.quote(loader)}/profile/json")
    _escribir_json(perfil["id"], perfil)
    return perfil["id"]


def _main_class(jar):
    with zipfile.ZipFile(jar) as z:
        texto = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    texto = re.sub(r"\r?\n ", "", texto)  # las líneas largas del manifest continúan con un espacio
    m = re.search(r"^Main-Class:\s*(\S+)", texto, re.M)
    if not m:
        raise LauncherError(f"{jar.name} no indica su clase principal.")
    return m.group(1)


def _java_consola(java):
    """Para los pasos del instalador se usa java.exe (con salida), no javaw.exe."""
    j = Path(java)
    alt = j.with_name("java.exe") if j.name.lower() == "javaw.exe" else j
    return alt if alt.exists() else j


def _forge_legacy(z, perfil):
    """Forge 1.7.10 – 1.12.2: el instalador trae el JSON y el jar universal, sin pasos extra."""
    info = dict(perfil["versionInfo"])
    ins = perfil["install"]
    destino = LIB_DIR / ruta_maven(ins["path"])
    if not destino.exists():
        destino.parent.mkdir(parents=True, exist_ok=True)
        with z.open(ins["filePath"]) as src, open(destino, "wb") as dst:
            shutil.copyfileobj(src, dst)
    libs = []
    for lib in info.get("libraries", []):
        if lib.get("clientreq") is False:
            continue
        lib = dict(lib)
        if lib.get("url"):
            lib["url"] = re.sub(r"^https?://files\.minecraftforge\.net/maven/?", "https://maven.minecraftforge.net/",
                                lib["url"])
        libs.append(lib)
    info["libraries"] = libs
    info.setdefault("inheritsFrom", ins.get("minecraft"))
    _escribir_json(info["id"], info)
    return info["id"]


def instalar_forge(tipo, version, mc, java, progreso, log, estado):
    """Forge y NeoForge: descarga el instalador oficial y ejecuta sus pasos (processors) del lado cliente."""
    nombre = CARGADORES[tipo]
    url = (f"{FORGE_MAVEN}/{version}/forge-{version}-installer.jar" if tipo == "forge"
           else f"{NEO_MAVEN}/{version}/neoforge-{version}-installer.jar")
    carpeta = LAUNCHER_DIR / "instaladores"
    inst = carpeta / url.rsplit("/", 1)[1]
    estado(f"Descargando instalador de {nombre}…")
    try:
        descargar(url, inst)
    except LauncherError as e:
        raise LauncherError(f"No se pudo descargar el instalador de {nombre} {version}: {e}") from e

    with zipfile.ZipFile(inst) as z:
        perfil = json.loads(z.read("install_profile.json"))
        if "versionInfo" in perfil:
            return _forge_legacy(z, perfil)
        vjson = json.loads(z.read(perfil.get("json", "/version.json").lstrip("/")))
        vid = vjson["id"]
        if (VERSIONS_DIR / vid / ".instalado").exists():
            return vid
        estado(f"Instalando {nombre} {version}…")
        for n in z.namelist():  # jars que vienen dentro del instalador
            if n.startswith("maven/") and not n.endswith("/"):
                destino = LIB_DIR / n[len("maven/"):]
                if not destino.exists():
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(n) as src, open(destino, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        tmp = carpeta / f"datos-{vid}"
        datos = {}
        for k, v in perfil.get("data", {}).items():
            val = v.get("client", "")
            if val.startswith("[") and val.endswith("]"):
                datos[k] = str(LIB_DIR / ruta_maven(val[1:-1]))
            elif val.startswith("/"):
                destino = tmp / val.lstrip("/")
                destino.parent.mkdir(parents=True, exist_ok=True)
                with z.open(val.lstrip("/")) as src, open(destino, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                datos[k] = str(destino)
            else:
                datos[k] = val[1:-1] if val.startswith("'") and val.endswith("'") else val

    trabajos = []
    for lib in perfil.get("libraries", []):
        a = (lib.get("downloads") or {}).get("artifact") or {}
        if a.get("url"):
            trabajos.append((a["url"], LIB_DIR / a["path"], a.get("sha1"), a.get("size")))
    descargar_lote(trabajos, progreso, f"{nombre} · librerías")

    datos.update(MINECRAFT_JAR=str(VERSIONS_DIR / mc / f"{mc}.jar"), SIDE="client", MINECRAFT_VERSION=mc,
                 ROOT=str(DATA_DIR), INSTALLER=str(inst), LIBRARY_DIR=str(LIB_DIR))

    def sustituir(arg):
        if arg.startswith("[") and arg.endswith("]"):
            return str(LIB_DIR / ruta_maven(arg[1:-1]))
        arg = re.sub(r"\{(\w+)\}", lambda m: datos.get(m.group(1), m.group(0)), arg)
        return arg[1:-1] if arg.startswith("'") and arg.endswith("'") else arg

    pasos = [p for p in perfil.get("processors", []) if "client" in p.get("sides", ["client"])]
    jv = _java_consola(java)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for i, p in enumerate(pasos, 1):
        progreso(i - 1, len(pasos), f"Instalando {nombre}")
        jar = LIB_DIR / ruta_maven(p["jar"])
        cp = os.pathsep.join([str(jar)] + [str(LIB_DIR / ruta_maven(c)) for c in p.get("classpath", [])])
        cmd = [str(jv), "-cp", cp, _main_class(jar), *[sustituir(a) for a in p.get("args", [])]]
        log(f"[{nombre}] Paso {i}/{len(pasos)}: {p['jar'].split(':')[1]}")
        r = subprocess.run(cmd, cwd=DATA_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=flags)
        if r.returncode != 0:
            for linea in (r.stdout + r.stderr).splitlines()[-25:]:
                log(linea, "error")
            log_error(f"Paso {i} de {nombre} falló ({r.returncode}): {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
            raise LauncherError(f"Falló el paso {i} de {len(pasos)} del instalador de {nombre}. "
                                "El detalle quedó en el registro.")
    progreso(len(pasos), max(len(pasos), 1), f"Instalando {nombre}")
    _escribir_json(vid, vjson)
    (VERSIONS_DIR / vid / ".instalado").write_text("ok")
    shutil.rmtree(carpeta / f"datos-{vid}", ignore_errors=True)
    log(f"{nombre} {version} instalado como {vid}.")
    return vid


def instalar_cargador(tipo, mc, loader, manifest, progreso, log, estado):
    """Instala vanilla (hace falta para Forge/NeoForge) y luego el loader. Devuelve el id a lanzar."""
    if tipo == "vanilla":
        return mc
    estado(f"Preparando Minecraft {mc}…")
    _, java, *_ = instalar_version(mc, manifest, progreso, log, estado)
    if tipo == "fabric":
        estado("Instalando Fabric…")
        vid = instalar_fabric(mc, loader)
    else:
        vid = instalar_forge(tipo, loader, mc, java, progreso, log, estado)
    registrar_perfil(vid, f"{CARGADORES[tipo]} {mc}")
    return vid


def _leer_perfiles():
    ruta = MC_DIR / "launcher_profiles.json"
    try:
        return ruta, (json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {})
    except (ValueError, OSError) as e:
        log_error(f"launcher_profiles.json ilegible, no se modifica: {e!r}")
        return ruta, None  # nunca sobrescribir un archivo que no se pudo leer


def _escribir_perfiles(ruta, datos):
    try:
        tmp = ruta.with_suffix(".nexlauncher.tmp")
        tmp.write_text(json.dumps(datos, indent=2), encoding="utf-8")
        os.replace(tmp, ruta)
    except OSError as e:
        log_error(f"No se pudo actualizar launcher_profiles.json: {e!r}")


def quitar_perfil(clave):
    ruta, datos = _leer_perfiles()
    if datos and clave in datos.get("profiles", {}):
        del datos["profiles"][clave]
        _escribir_perfiles(ruta, datos)


def registrar_perfil(vid, nombre, game_dir=None, clave=None):
    """Agrega la versión a launcher_profiles.json para que el launcher oficial también la vea."""
    ruta, datos = _leer_perfiles()
    if datos is None:
        return
    perfiles = datos.setdefault("profiles", {})
    clave = clave or f"nexlauncher-{vid}"
    if clave in perfiles and perfiles[clave].get("lastVersionId") == vid:
        return
    ahora = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    perfiles[clave] = {"name": nombre, "type": "custom", "lastVersionId": vid, "created": ahora,
                       "lastUsed": ahora, "icon": "Furnace"}
    if game_dir:
        perfiles[clave]["gameDir"] = str(game_dir)
    datos.setdefault("settings", {})
    datos.setdefault("version", 3)
    _escribir_perfiles(ruta, datos)


def migrar_ajustes():
    """Cuentas y ajustes de versiones anteriores (carpeta NexLauncher_data junto al .exe)."""
    viejo = BASE_DIR / "NexLauncher_data"
    for nombre in ("accounts.json", "config.json", "mods_instalados.json"):
        o, d = viejo / nombre, LAUNCHER_DIR / nombre
        if o.exists() and not d.exists():
            try:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(o, d)
            except OSError as e:
                log_error(f"No se pudo migrar {nombre}: {e!r}")


def _fusionar(origen, destino):
    """Mueve los archivos que no existan en destino. Devuelve (movidos, errores)."""
    movidos = errores = 0
    for raiz, _, archivos in os.walk(origen):
        for a in archivos:
            src = Path(raiz) / a
            dst = destino / src.relative_to(origen)
            if dst.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), dst)
                movidos += 1
            except OSError as e:
                errores += 1
                log_error(f"No se pudo mover {src}: {e!r}")
    return movidos, errores


def migrar_juego(log):
    """Mueve lo descargado/jugado con versiones anteriores a .minecraft (sin sobrescribir nada)."""
    marca = LAUNCHER_DIR / ".migrado"
    if marca.exists():
        return
    movidos = 0
    for viejo in (BASE_DIR / "NexLauncher_data", LAUNCHER_DIR):
        for sub in ("versions", "libraries", "assets", "runtime"):  # archivos del juego: se pueden volver a bajar
            origen = viejo / sub
            if origen.is_dir():
                m, err = _fusionar(origen, MC_DIR / sub)
                movidos += m
                if not err:
                    shutil.rmtree(origen, ignore_errors=True)
        # tus mundos, opciones y paquetes: se mueven solo si no existen ya en .minecraft; nada se borra
        if (viejo / "minecraft").is_dir():
            movidos += _fusionar(viejo / "minecraft", MC_DIR)[0]
        if (viejo / "instancias").is_dir():
            for inst in (viejo / "instancias").iterdir():
                for sub in ("saves", "screenshots", "resourcepacks", "shaderpacks"):
                    if (inst / sub).is_dir():
                        movidos += _fusionar(inst / sub, MC_DIR / sub)[0]
    if movidos:
        log(f"Se movieron {movidos} archivos descargados antes a {MC_DIR}.")
    try:
        LAUNCHER_DIR.mkdir(parents=True, exist_ok=True)
        marca.write_text("ok")
    except OSError as e:
        log_error(f"No se pudo marcar la migración: {e!r}")


def client_id_integrado():
    """client_id.txt empaquetado en el .exe: así nadie más tiene que configurar Azure."""
    try:
        v = ruta_recurso("client_id.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return v if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", v) else ""


class ParserLog4j:
    """Convierte los eventos XML que imprime el juego en líneas legibles."""
    RE_EV = re.compile(r'<log4j:Event[^>]*timestamp="(\d+)"[^>]*level="(\w+)"[^>]*thread="([^"]*)"')
    RE_MSG = re.compile(r"<log4j:Message><!\[CDATA\[(.*?)\]\]></log4j:Message>", re.S)
    RE_THR = re.compile(r"<log4j:Throwable><!\[CDATA\[(.*?)\]\]></log4j:Throwable>", re.S)
    NIVELES = {"warn": "warn", "error": "error", "fatal": "error"}

    def __init__(self):
        self.buf = None

    def feed(self, linea):
        if self.buf is None:
            if "<log4j:Event" not in linea:
                return [(linea, "info")] if linea.strip() else []
            self.buf = [linea]
        else:
            self.buf.append(linea)
        if "</log4j:Event>" not in linea:
            if len(self.buf) > 400:  # evento roto: no acumular para siempre
                txt, self.buf = "\n".join(self.buf), None
                return [(txt, "info")]
            return []
        txt, self.buf = "\n".join(self.buf), None
        m = self.RE_EV.search(txt)
        if not m:
            return [(txt, "info")]
        hora = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000).strftime("%H:%M:%S")
        nivel = self.NIVELES.get(m.group(2).lower(), "info")
        msg = self.RE_MSG.search(txt)
        salida = [(f"[{hora}] [{m.group(3)}/{m.group(2)}] {msg.group(1) if msg else ''}", nivel)]
        thr = self.RE_THR.search(txt)
        if thr:
            salida += [(l, nivel) for l in thr.group(1).splitlines() if l.strip()]
        return salida


def ram_total_mb():
    try:
        if os.name == "nt":
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = _MS()
            m.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullTotalPhys // (1024 * 1024))
        else:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except (OSError, ValueError, AttributeError) as e:
        log_error(f"No se pudo leer la RAM total: {e!r}")
    return 8192


# --- Modrinth: búsqueda e instalación de mods -----------------------------------
MODRINTH = "https://api.modrinth.com/v2"
UA_MODRINTH = f"ZyoCr0w/NexLauncher/{APP_VERSION} (launcher de Minecraft)"  # Modrinth pide identificar la app
CARPETA_TIPO = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks"}


def modrinth(ruta, **params):
    q = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (list, dict)) else v
                                for k, v in params.items() if v is not None})
    try:
        return http_json(f"{MODRINTH}{ruta}" + (f"?{q}" if q else ""), headers={"User-Agent": UA_MODRINTH})
    except HttpError as e:
        if e.code == 429:
            raise LauncherError("Modrinth pidió esperar un momento (demasiadas solicitudes). Intenta en unos segundos.") from e
        raise LauncherError(f"Modrinth respondió con un error ({e.code}).") from e


def buscar_modrinth(texto, tipo_proyecto, cargador, mc, orden="relevance", offset=0, limite=20):
    facets = [[f"project_type:{tipo_proyecto}"]]
    if mc:
        facets.append([f"versions:{mc}"])
    if tipo_proyecto == "mod" and cargador != "vanilla":
        facets.append([f"categories:{cargador}"])
    return modrinth("/search", query=texto, facets=facets, index=orden, offset=offset, limit=limite)


def formato_num(n):
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M".replace(".0 ", " ")
    if n >= 1_000:
        return f"{n / 1_000:.1f} k".replace(".0 ", " ")
    return str(n)


class Instancia:
    """Carpeta de juego de un loader + versión, con registro de lo instalado desde Modrinth."""

    def __init__(self, tipo, mc, carpeta=None, ruta_reg=None):
        self.tipo, self.mc = tipo, mc
        self.dir = Path(carpeta) if carpeta else carpeta_juego(tipo, mc)
        self.ruta_reg = Path(ruta_reg) if ruta_reg else LAUNCHER_DIR / "mods_instalados.json"
        try:
            self.reg = json.loads(self.ruta_reg.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.reg = {}
        except (ValueError, OSError) as e:
            log_error(f"Registro de mods ilegible ({self.ruta_reg}): {e!r}")
            self.reg = {}
        self._lock = threading.Lock()

    def guardar(self):
        with self._lock:
            try:
                self.ruta_reg.parent.mkdir(parents=True, exist_ok=True)
                self.ruta_reg.write_text(json.dumps(self.reg, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                log_error(f"No se pudo guardar el registro de mods: {e!r}")

    def instalado(self, pid):
        e = self.reg.get(pid)
        return bool(e) and (self.dir / e["carpeta"] / e["archivo"]).exists()

    def loaders(self, tp):
        if tp == "mod":
            return [self.tipo]
        if tp == "resourcepack":
            return ["minecraft"]
        return None  # shaders: Iris / Oculus / OptiFine, se filtra solo por versión

    def mejor_version(self, pid, tp):
        vs = modrinth(f"/project/{pid}/version", game_versions=[self.mc], loaders=self.loaders(tp))
        if not vs:
            return None
        return next((v for v in vs if v.get("version_type") == "release"), vs[0])

    def instalar(self, pid, tp, titulo, visto=None):
        """Instala el proyecto y sus dependencias obligatorias. Devuelve los títulos instalados."""
        visto = set() if visto is None else visto
        if pid in visto or self.instalado(pid):
            return []
        visto.add(pid)
        v = self.mejor_version(pid, tp)
        if not v:
            raise LauncherError(f"«{titulo}» no tiene versión para {CARGADORES[self.tipo]} {self.mc}.")
        return self._instalar_version(v, tp, titulo, visto)

    def _instalar_version(self, v, tp, titulo, visto):
        archivo = next((f for f in v["files"] if f.get("primary")), v["files"][0])
        carpeta = CARPETA_TIPO[tp]
        descargar(archivo["url"], self.dir / carpeta / archivo["filename"],
                  archivo.get("hashes", {}).get("sha1"), archivo.get("size"))
        self.reg[v["project_id"]] = {"titulo": titulo, "archivo": archivo["filename"], "carpeta": carpeta,
                                     "version": v.get("version_number", ""), "tipo": tp,
                                     "cargador": self.tipo, "mc": self.mc}
        self.guardar()
        hechos = [titulo]
        for d in v.get("dependencies", []):
            if d.get("dependency_type") != "required":
                continue
            if d.get("version_id"):
                dv = modrinth(f"/version/{d['version_id']}")
                dpid = dv["project_id"]
                if dpid in visto or self.instalado(dpid):
                    continue
                visto.add(dpid)
                hechos += self._instalar_version(dv, "mod", modrinth(f"/project/{dpid}")["title"], visto)
            elif d.get("project_id"):
                dpid = d["project_id"]
                if dpid in visto or self.instalado(dpid):
                    continue
                hechos += self.instalar(dpid, "mod", modrinth(f"/project/{dpid}")["title"], visto)
        return hechos

    def quitar(self, pid):
        e = self.reg.pop(pid, None)
        if e:
            try:
                (self.dir / e["carpeta"] / e["archivo"]).unlink(missing_ok=True)
            except OSError as ex:
                raise LauncherError(f"No se pudo borrar {e['archivo']} (¿el juego está abierto?): {ex}") from ex
            self.guardar()

    def manuales(self):
        """Archivos que están en mods/ pero no se instalaron desde NexLauncher."""
        conocidos = {(e["carpeta"], e["archivo"]) for e in self.reg.values()}
        carpeta = self.dir / "mods"
        if not carpeta.exists():
            return []
        return sorted(p.name for p in carpeta.iterdir()
                      if p.suffix.lower() == ".jar" and ("mods", p.name) not in conocidos)


# --- Modpacks de Modrinth (.mrpack) ------------------------------------------------
PACKS_DIR = MC_DIR / "modpacks"  # cada modpack es su propia carpeta de juego dentro de .minecraft
# Únicos sitios desde los que un .mrpack puede pedir archivos (los mismos que exige Modrinth)
DOMINIOS_MRPACK = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com"}


def _ruta_segura(base, rel):
    """Evita que un modpack malicioso escriba fuera de su carpeta (../../ o rutas absolutas)."""
    destino = (base / rel).resolve()
    if not destino.is_relative_to(base.resolve()):
        raise LauncherError(f"El modpack intenta escribir fuera de su carpeta ({rel}). Instalación cancelada.")
    return destino


def _nombre_carpeta(titulo):
    limpio = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", titulo).strip(" .")
    return (limpio or "modpack")[:60]


def listar_modpacks():
    packs = []
    if PACKS_DIR.is_dir():
        for d in sorted(PACKS_DIR.iterdir(), key=lambda x: x.name.lower()):
            f = d / "nexlauncher_pack.json"
            if f.exists():
                try:
                    meta = json.loads(f.read_text(encoding="utf-8"))
                    meta["dir"] = str(d)
                    packs.append(meta)
                except (ValueError, OSError) as e:
                    log_error(f"Modpack ilegible {f}: {e!r}")
    return packs


def instalar_modpack(pid, titulo, manifest, progreso, log, estado, icon_url=None):
    estado(f"Buscando la última versión de {titulo}…")
    vs = modrinth(f"/project/{pid}/version")
    if not vs:
        raise LauncherError(f"«{titulo}» no tiene versiones publicadas.")
    v = next((x for x in vs if x.get("version_type") == "release"), vs[0])
    archivo = next((f for f in v["files"] if f.get("primary")), v["files"][0])
    if not archivo["filename"].endswith(".mrpack"):
        raise LauncherError("Ese modpack no usa el formato de Modrinth (.mrpack).")
    mr = LAUNCHER_DIR / "instaladores" / archivo["filename"]
    sha = archivo.get("hashes", {}).get("sha1")
    if mr.exists() and (not sha or sha1_de(mr) != sha):
        mr.unlink()  # copia vieja con el mismo nombre (otra versión del pack): se baja de nuevo
    estado(f"Descargando {titulo}…")
    descargar(archivo["url"], mr, sha, archivo.get("size"))

    previo = next((m for m in listar_modpacks() if m.get("project_id") == pid), None)
    if previo:  # actualización: misma carpeta, se conservan mundos y ajustes
        destino = Path(previo["dir"])
    else:
        base = _nombre_carpeta(titulo)
        destino, k = PACKS_DIR / base, 2
        while destino.exists():
            destino, k = PACKS_DIR / f"{base} ({k})", k + 1
    destino.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(mr) as z:
            try:
                idx = json.loads(z.read("modrinth.index.json"))
            except (KeyError, ValueError) as e:
                raise LauncherError("El archivo del modpack está dañado.") from e
            deps = idx.get("dependencies", {})
            mc = deps.get("minecraft")
            if not mc:
                raise LauncherError("El modpack no indica su versión de Minecraft.")
            if "fabric-loader" in deps:
                tipo, loader = "fabric", deps["fabric-loader"]
            elif "neoforge" in deps:
                tipo, loader = "neoforge", deps["neoforge"]
            elif "forge" in deps:
                tipo, loader = "forge", f"{mc}-{deps['forge']}"
            elif "quilt-loader" in deps:
                raise LauncherError("Los modpacks de Quilt todavía no están soportados.")
            else:
                tipo, loader = "vanilla", None
            trabajos, rutas = [], []
            for f in idx.get("files", []):
                if (f.get("env") or {}).get("client") == "unsupported":
                    continue
                urls = [u for u in f.get("downloads", []) if urllib.parse.urlparse(u).hostname in DOMINIOS_MRPACK]
                if not urls:
                    raise LauncherError(f"El modpack pide bajar {f.get('path')} desde un sitio no permitido.")
                ruta = _ruta_segura(destino, f["path"])
                trabajos.append((urls[0], ruta, f.get("hashes", {}).get("sha1"), f.get("fileSize")))
                rutas.append(f["path"])
            estado(f"Copiando configuraciones de {titulo}…")
            for pref in ("overrides/", "client-overrides/"):  # client-overrides va después: tiene prioridad
                for n in z.namelist():
                    if n.startswith(pref) and not n.endswith("/"):
                        dst = _ruta_segura(destino, n[len(pref):])
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(n) as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)

        if previo:  # quitar los archivos de la versión anterior que ya no vienen (evita mods duplicados)
            for viejo in set(previo.get("archivos", [])) - set(rutas):
                try:
                    _ruta_segura(destino, viejo).unlink(missing_ok=True)
                except (OSError, LauncherError) as e:
                    log_error(f"No se pudo quitar {viejo}: {e!r}")
        estado(f"Descargando mods de {titulo}…")
        descargar_lote(trabajos, progreso, f"{titulo} · archivos")
        vid = instalar_cargador(tipo, mc, loader, manifest, progreso, log, estado)
        meta = {"nombre": titulo, "project_id": pid, "version_id": v["id"], "version": v.get("version_number", ""),
                "mc": mc, "tipo": tipo, "loader": loader, "vid": vid, "icon_url": icon_url, "archivos": rutas}
        (destino / "nexlauncher_pack.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        registrar_perfil(vid, titulo, destino, clave=f"nexlauncher-pack-{pid}")
        log(f"Modpack {titulo} {meta['version']} instalado en {destino} ({len(trabajos)} archivos).")
    except Exception:
        if not previo:  # instalación nueva fallida: no dejar una carpeta a medias
            shutil.rmtree(destino, ignore_errors=True)
        raise
    meta["dir"] = str(destino)
    return meta


def quitar_modpack(meta):
    shutil.rmtree(meta["dir"], ignore_errors=False)
    quitar_perfil(f"nexlauncher-pack-{meta.get('project_id')}")


# --- Actualizaciones automáticas (GitHub Releases) -----------------------------------
# Repositorio PÚBLICO de GitHub donde publicas los releases (usuario/repositorio).
REPO_ACTUALIZACIONES = "ZyoCr0w/NexLauncher"


def ruta_exe():
    return Path(sys.argv[0]).resolve()


def buscar_actualizacion():
    """Devuelve datos del release nuevo, o None si ya está al día. Nunca instala nada sin hash."""
    datos = http_json(f"https://api.github.com/repos/{REPO_ACTUALIZACIONES}/releases/latest",
                      headers={"Accept": "application/vnd.github+json"}, timeout=15)
    tag = (datos.get("tag_name") or "").lstrip("vV")
    if not tag or clave_num(tag) <= clave_num(APP_VERSION):
        return None
    assets = datos.get("assets", [])
    exe = next((a for a in assets if a.get("name", "").lower() == "nexlauncher.exe"), None)
    if not exe:
        return None
    sha = (exe.get("digest") or "").removeprefix("sha256:") or None
    if not sha:
        archivo = next((a for a in assets if a.get("name", "").lower() == "nexlauncher.exe.sha256"), None)
        if archivo:
            sha = http_texto(archivo["browser_download_url"]).split()[0]
    if not sha or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        log_error(f"El release {tag} no trae SHA256 válido: no se instala.")
        return None
    return {"version": tag, "url": exe["browser_download_url"], "size": exe.get("size"),
            "sha256": sha.lower(), "notas": datos.get("body") or ""}


def descargar_actualizacion(info):
    exe = ruta_exe()
    nuevo = exe.with_name(exe.stem + ".nuevo.exe")
    h = hashlib.sha256()
    req = urllib.request.Request(info["url"], headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(nuevo, "wb") as f:
            while bloque := r.read(1 << 16):
                h.update(bloque)
                f.write(bloque)
    except (urllib.error.URLError, OSError) as e:
        nuevo.unlink(missing_ok=True)
        raise LauncherError(f"No se pudo descargar la actualización: {e}") from e
    if h.hexdigest() != info["sha256"] or (info.get("size") and nuevo.stat().st_size != info["size"]):
        nuevo.unlink(missing_ok=True)
        raise LauncherError("La actualización descargada no coincide con su firma SHA256; se descartó.")
    return nuevo


def aplicar_actualizacion(nuevo, relanzar=False):
    """Windows deja renombrar un .exe en uso: el actual pasa a .viejo.exe y el nuevo ocupa su lugar."""
    exe = ruta_exe()
    viejo = exe.with_name(exe.stem + ".viejo.exe")
    try:
        viejo.unlink(missing_ok=True)
    except OSError:
        viejo = exe.with_name(f"{exe.stem}.viejo-{int(time.time())}.exe")
    os.replace(exe, viejo)
    try:
        os.replace(nuevo, exe)
    except OSError:
        os.replace(viejo, exe)  # deshacer: nunca quedarse sin launcher
        raise
    if relanzar:
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")  # que el nuevo .exe arranque de cero
        flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent), creationflags=flags, close_fds=True)


def limpiar_actualizaciones():
    if not IS_FROZEN:
        return
    exe = ruta_exe()
    for p in [*exe.parent.glob(exe.stem + ".viejo*.exe"), exe.with_name(exe.stem + ".nuevo.exe")]:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass  # el proceso viejo aún está cerrando; se borra la próxima vez


# --- Configuración --------------------------------------------------------
CONFIG_DEFAULT = {"microsoft_client_id": "", "ram_mb": 2048, "ultima_version": "",
                  "mostrar_snapshots": False, "recordar": True, "cuenta_activa": ""}


def cargar_config():
    cfg = dict(CONFIG_DEFAULT)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as e:
        log_error(f"config.json ilegible, se usan valores por defecto: {e!r}")
    return cfg


def guardar_config(cfg):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError as e:
        log_error(f"No se pudo guardar config.json: {e!r}")


# ============================== INTERFAZ ===================================
G = {
    "bg": "#0b0b0c", "bg2": "#111113", "card": "#18181b", "card2": "#202024",
    "linea": "#2c2c31", "tenue": "#8a8a93", "texto": "#f4f4f5", "blanco": "#ffffff",
    "prim": "#d9d9de", "prim_h": "#ffffff", "prim_txt": "#0b0b0c",
    "sec": "#26262b", "sec_h": "#35353b", "desact": "#2f2f34", "desact_txt": "#6b6b73",
}
FUENTE = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
MONO = "Consolas" if os.name == "nt" else "DejaVu Sans Mono"


def fnt(tam, peso="normal"):
    if peso == "semi":
        return ("Segoe UI Semibold", tam) if os.name == "nt" else (FUENTE, tam, "bold")
    return (FUENTE, tam, peso)


# --- Curvas de animación -----------------------------------------------------
def out_cubic(t):
    return 1 - (1 - t) ** 3


def in_cubic(t):
    return t ** 3


def in_out_cubic(t):
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def out_back(t):
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def mezclar(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Tween:
    """Anima de 0 a 1 en `dur` ms llamando paso(t) con la curva dada (no bloquea la UI)."""

    def __init__(self, w, dur, paso, fin=None, ease=out_cubic):
        self.w, self.dur, self.paso, self.fin, self.ease = w, dur, paso, fin, ease
        self.t0 = time.perf_counter()
        self.vivo, self._id = True, None
        self._tick()

    def _tick(self):
        if not self.vivo:
            return
        try:
            if not self.w.winfo_exists():
                return
        except tk.TclError:
            return
        t = min((time.perf_counter() - self.t0) * 1000 / self.dur, 1.0)
        try:
            self.paso(self.ease(t))
        except tk.TclError:  # el widget animado ya no existe
            self.vivo = False
            return
        if t < 1:
            self._id = self.w.after(max(1, 1000 // FPS), self._tick)
        else:
            self.vivo = False
            if self.fin:
                self.fin()

    def cancel(self):
        self.vivo = False
        if self._id:
            try:
                self.w.after_cancel(self._id)
            except tk.TclError:
                pass


def puntos_redondeados(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def rect_redondo(cv, x1, y1, x2, y2, r, **kw):
    return cv.create_polygon(puntos_redondeados(x1, y1, x2, y2, r), smooth=True, splinesteps=16, **kw)


def barra_oscura(win):
    """Barra de título oscura en Windows 10/11."""
    if os.name != "nt":
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        v = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0:
                break
    except Exception as e:
        log_error(f"No se pudo oscurecer la barra de título: {e!r}")


def quitar_barra_nativa(win):
    """Quita la barra de título de Windows (conserva borde para redimensionar, ícono en la barra
    de tareas y minimizar/maximizar). Devuelve True si lo logró."""
    if os.name != "nt":
        return False
    try:
        win.update_idletasks()
        u = ctypes.windll.user32
        u.GetParent.argtypes, u.GetParent.restype = [wintypes.HWND], wintypes.HWND
        u.GetWindowLongW.argtypes, u.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
        u.SetWindowLongW.argtypes, u.SetWindowLongW.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        hwnd = u.GetParent(win.winfo_id())
        GWL_STYLE, WS_CAPTION = -16, 0x00C00000
        WS_THICKFRAME, WS_SYSMENU, WS_MIN, WS_MAX = 0x00040000, 0x00080000, 0x00020000, 0x00010000
        estilo = u.GetWindowLongW(hwnd, GWL_STYLE)
        estilo = (estilo & ~WS_CAPTION) | WS_THICKFRAME | WS_SYSMENU | WS_MIN | WS_MAX
        u.SetWindowLongW(hwnd, GWL_STYLE, estilo)
        # NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020)
        return True
    except Exception as e:
        log_error(f"No se pudo quitar la barra de título: {e!r}")
        return False


def _hwnd_marco(win):
    u = ctypes.windll.user32
    u.GetParent.argtypes, u.GetParent.restype = [wintypes.HWND], wintypes.HWND
    return u, u.GetParent(win.winfo_id())


def tiene_barra_nativa(win):
    """True si la ventana todavía tiene la barra de título de Windows (Tk la puede volver a poner)."""
    try:
        u, hwnd = _hwnd_marco(win)
        u.GetWindowLongW.argtypes, u.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
        return bool(u.GetWindowLongW(hwnd, -16) & 0x00C00000)
    except Exception:
        return False


def quitar_capa_transparente(win):
    """Tras el efecto de aparición, la ventana sigue 'en capas' y mover/repintar parpadea: se desactiva."""
    try:
        u, hwnd = _hwnd_marco(win)
        u.GetWindowLongW.argtypes, u.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
        u.SetWindowLongW.argtypes, u.SetWindowLongW.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long
        ex = u.GetWindowLongW(hwnd, -20)
        if ex & 0x00080000:
            u.SetWindowLongW(hwnd, -20, ex & ~0x00080000)
            u.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            u.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100)  # INVALIDATE|ERASE|ALLCHILDREN|FRAME
    except Exception as e:
        log_error(f"No se pudo quitar la capa transparente: {e!r}")


def origen_ventana(win):
    """(x, y) de la esquina superior izquierda de la ventana, en las mismas coordenadas que e.x_root."""
    u, hwnd = _hwnd_marco(win)
    r = wintypes.RECT()
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top


def mover_ventana(win, x, y):
    """Mueve la ventana directo con Windows (sin tocar su tamaño ni redibujar de más)."""
    u, hwnd = _hwnd_marco(win)
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE


# --- Imágenes ------------------------------------------------------------------
class Imagenes:
    def __init__(self):
        try:
            self.base = Image.open(ruta_recurso("logo.png")).convert("RGBA")
        except OSError as e:
            log_error(f"No se encontró logo.png: {e!r}")
            self.base = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            ImageDraw.Draw(self.base).rectangle((48, 48, 208, 208), fill=(200, 200, 205, 255))
        self._cache = {}

    def logo(self, tam):
        tam = max(8, int(tam))
        k = ("logo", tam)
        if k not in self._cache:
            self._cache[k] = ImageTk.PhotoImage(self.base.resize((tam, tam), Image.LANCZOS))
        return self._cache[k]

    def brillo(self, tam, niveles=24):
        """Halo blanco difuminado, en varias intensidades para el efecto de pulso."""
        k = ("brillo", tam)
        if k not in self._cache:
            pad = tam // 3
            total = tam + 2 * pad
            lienzo = Image.new("L", (total, total), 0)
            lienzo.paste(self.base.resize((tam, tam), Image.LANCZOS).getchannel("A"), (pad, pad))
            blur = lienzo.filter(ImageFilter.GaussianBlur(tam / 9))
            frames = []
            for i in range(niveles):
                fuerza = 0.10 + 0.35 * i / (niveles - 1)
                img = Image.new("RGBA", (total, total), (255, 255, 255, 0))
                img.putalpha(blur.point(lambda v, f=fuerza: int(v * f)))
                frames.append(ImageTk.PhotoImage(img))
            self._cache[k] = frames
        return self._cache[k]


def avatar_circular(img, tam):
    img = ImageOps.grayscale(img.convert("RGB")).convert("RGBA").resize((tam, tam), Image.NEAREST)
    mascara = Image.new("L", (tam * 4, tam * 4), 0)
    ImageDraw.Draw(mascara).ellipse((0, 0, tam * 4 - 1, tam * 4 - 1), fill=255)
    img.putalpha(mascara.resize((tam, tam), Image.LANCZOS))
    return ImageTk.PhotoImage(img)


# --- Widgets propios ----------------------------------------------------------
def icono_microsoft(cv, x, y):
    tonos = ("#3f3f46", "#52525b", "#71717a", "#27272a")
    s, g = 7, 2
    for i, (dx, dy) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
        x1, y1 = x + dx * (s + g), y - s - g / 2 + dy * (s + g)
        cv.create_rectangle(x1, y1, x1 + s, y1 + s, fill=tonos[i], outline="", tags="icono")


class Boton(tk.Canvas):
    ESTILOS = {
        "primario": ("prim", "prim_h", "prim_txt", "prim_txt"),
        "secundario": ("sec", "sec_h", "texto", "texto"),
        "fantasma": (None, "sec", "tenue", "texto"),
    }

    def __init__(self, padre, texto, comando, estilo="secundario", alto=42, ancho=120,
                 fuente=None, radio=10, brillo=False, icono=None):
        self.fondo = padre.cget("bg")
        super().__init__(padre, height=alto, width=ancho, bg=self.fondo, highlightthickness=0,
                         bd=0, cursor="hand2")
        b, h, t, th = self.ESTILOS[estilo]
        self.c_base = G[b] if b else self.fondo
        self.c_hover, self.c_txt, self.c_txt_h = G[h], G[t], G[th]
        self.estilo, self.texto, self.comando, self.radio = estilo, texto, comando, radio
        self.fuente = fuente or fnt(10, "semi")
        self._f = tkfont.Font(self, font=self.fuente)
        self.icono, self.activo, self.h, self._tw, self.cuerpo = icono, True, 0.0, None, None
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Enter>", lambda e: self._hover(1.0))
        self.bind("<Leave>", lambda e: self._hover(0.0))
        self.bind("<ButtonPress-1>", lambda e: self._dibujar(2) if self.activo else None)
        self.bind("<ButtonRelease-1>", self._soltar)
        if brillo:
            self.after(1800, self._ciclo_brillo)

    def _colores(self):
        if not self.activo:
            return (self.fondo if self.estilo == "fantasma" else G["desact"]), G["desact_txt"]
        return mezclar(self.c_base, self.c_hover, self.h), mezclar(self.c_txt, self.c_txt_h, self.h)

    def _dibujar(self, hundido=0):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        relleno, txt = self._colores()
        i = hundido
        self.cuerpo = rect_redondo(self, i, i, w - i, h - i, self.radio, fill=relleno, outline="")
        ancho_txt = self._f.measure(self.texto)
        ancho_ico = 24 if self.icono else 0
        x0 = w / 2 - (ancho_txt + ancho_ico) / 2
        if self.icono:
            self.icono(self, x0, h / 2)
        self.create_text(x0 + ancho_ico + ancho_txt / 2, h / 2, text=self.texto, fill=txt,
                         font=self.fuente, tags="txt")

    def _set_h(self, v):
        self.h = v
        if self.cuerpo is not None and self.activo:
            relleno, txt = self._colores()
            self.itemconfig(self.cuerpo, fill=relleno)
            self.itemconfig("txt", fill=txt)

    def _hover(self, objetivo):
        if not self.activo:
            return
        if self._tw:
            self._tw.cancel()
        ini = self.h
        self._tw = Tween(self, 170, lambda t: self._set_h(ini + (objetivo - ini) * t))

    def _soltar(self, e):
        if not self.activo:
            return
        self._dibujar()
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height() and self.comando:
            self.comando()

    def _ciclo_brillo(self):
        if not self.winfo_exists():
            return
        if self.activo and self.winfo_ismapped():
            Tween(self, 1100, self._banda, fin=lambda: self.delete("banda"), ease=in_out_cubic)
        self.after(3800, self._ciclo_brillo)

    def _banda(self, t):
        """Destello diagonal que recorre el botón (se mantiene dentro de los bordes redondeados)."""
        w, h, r = self.winfo_width(), self.winfo_height(), self.radio
        x = -60 + (w + 120) * t
        pts = [x, 0, x + 38, 0, x + 6, h, x - 32, h]
        pts = [min(max(v, r), w - r) if i % 2 == 0 else v for i, v in enumerate(pts)]
        self.delete("banda")
        if self.activo:
            relleno, _ = self._colores()
            self.create_polygon(pts, fill=mezclar(relleno, "#ffffff", 0.75), outline="", tags="banda")
            self.tag_raise("icono")
            self.tag_raise("txt")

    def set_texto(self, texto):
        self.texto = texto
        self._dibujar()

    def set_fondo(self, color):
        if color != self.fondo:
            if self.estilo == "fantasma":
                self.c_base = color
            self.fondo = color
            self.configure(bg=color)
            self._dibujar()

    def set_estilo(self, estilo):
        b, h, t, th = self.ESTILOS[estilo]
        self.estilo = estilo
        self.c_base = G[b] if b else self.fondo
        self.c_hover, self.c_txt, self.c_txt_h = G[h], G[t], G[th]
        self._dibujar()

    def set_activo(self, activo):
        self.activo = activo
        self.configure(cursor="hand2" if activo else "arrow")
        if not activo:
            self.h = 0.0
        self._dibujar()


class Selector(tk.Canvas):
    """Control segmentado con un resaltado que se desliza entre opciones."""

    def __init__(self, padre, opciones, valor, al_cambiar):
        super().__init__(padre, height=38, bg=padre.cget("bg"), highlightthickness=0, cursor="hand2")
        self.ops, self.al_cambiar = opciones, al_cambiar
        self.i = opciones.index(valor) if valor in opciones else 0
        self.pos, self.hover, self._tw = float(self.i), -1, None
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._mover)
        self.bind("<Leave>", lambda e: self._mover(None))

    def _indice(self, x):
        w = self.winfo_width()
        return max(0, min(len(self.ops) - 1, int((x - 3) // ((w - 6) / len(self.ops)))))

    def _mover(self, e):
        h = -1 if e is None else self._indice(e.x)
        if h != self.hover:
            self.hover = h
            self._dibujar()

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40:
            return
        n = len(self.ops)
        cw = (w - 6) / n
        rect_redondo(self, 0, 0, w, h, 11, fill=G["card2"], outline="")
        if 0 <= self.hover < n and self.hover != self.i:
            xh = 3 + self.hover * cw
            rect_redondo(self, xh, 3, xh + cw, h - 3, 9, fill=G["sec"], outline="")
        x = 3 + self.pos * cw
        rect_redondo(self, x, 3, x + cw, h - 3, 9, fill=G["prim"], outline="")
        for k, op in enumerate(self.ops):
            cerca = max(0.0, 1 - abs(self.pos - k))
            base = G["texto"] if k == self.hover else G["tenue"]
            self.create_text(3 + cw * (k + 0.5), h / 2, text=op, fill=mezclar(base, G["prim_txt"], cerca),
                             font=fnt(9 if n <= 4 else 8, "semi"))

    def _click(self, e):
        k = self._indice(e.x)
        if k == self.i:
            return
        self.i = k
        if self._tw:
            self._tw.cancel()
        ini = self.pos

        def paso(t):
            self.pos = ini + (k - ini) * t
            self._dibujar()
        self._tw = Tween(self, 340, paso, ease=out_back)
        self.al_cambiar(self.ops[k])


class Entrada(tk.Frame):
    """Campo de texto con etiqueta flotante y subrayado animado."""

    def __init__(self, padre, etiqueta, al_enter=None):
        super().__init__(padre, bg=G["card2"], height=54, highlightthickness=1,
                         highlightbackground=G["linea"], highlightcolor=G["linea"])
        self.pack_propagate(False)
        self.var = tk.StringVar()
        self.e = tk.Entry(self, textvariable=self.var, bg=G["card2"], fg=G["texto"],
                          insertbackground=G["texto"], relief="flat", bd=0, font=fnt(11),
                          highlightthickness=0)
        self.e.place(x=12, y=24, relwidth=1, width=-24, height=24)
        self.lbl = tk.Label(self, text=etiqueta, bg=G["card2"], fg=G["tenue"], font=fnt(11))
        self.lbl.place(x=12, y=15)
        self.linea = tk.Canvas(self, height=2, bg=G["card2"], highlightthickness=0)
        self.linea.place(x=0, rely=1, y=-2, relwidth=1, height=2)
        self.p, self.f, self._tw_p, self._tw_f = 0.0, 0.0, None, None
        for w in (self.lbl, self):
            w.bind("<Button-1>", lambda e: self.e.focus_set())
        self.e.bind("<FocusIn>", lambda e: self._animar(True))
        self.e.bind("<FocusOut>", lambda e: self._animar(False))
        self.var.trace_add("write", lambda *a: self._flotar())
        if al_enter:
            self.e.bind("<Return>", lambda e: al_enter())

    def _tiene_foco(self):
        try:
            return self.focus_get() is self.e
        except (KeyError, tk.TclError):
            return False

    def _flotar(self):
        objetivo = 1.0 if (self._tiene_foco() or self.var.get()) else 0.0
        if self._tw_p:
            self._tw_p.cancel()
        ini = self.p

        def paso(t):
            self.p = ini + (objetivo - ini) * t
            self.lbl.place_configure(y=15 - 11 * self.p)
            self.lbl.configure(font=fnt(round(11 - 3 * self.p)))
        self._tw_p = Tween(self, 180, paso)

    def _animar(self, foco):
        self._flotar()
        if self._tw_f:
            self._tw_f.cancel()
        ini, obj = self.f, (1.0 if foco else 0.0)

        def paso(t):
            self.f = ini + (obj - ini) * t
            w = self.linea.winfo_width()
            self.linea.delete("all")
            if self.f > 0.01:
                self.linea.create_line(w / 2 - w / 2 * self.f, 1, w / 2 + w / 2 * self.f, 1,
                                       fill=G["texto"], width=2)
        self._tw_f = Tween(self, 260, paso)

    def get(self):
        return self.var.get().strip()

    def destello(self):
        """Marca el campo como inválido un momento."""
        self.configure(highlightbackground=G["texto"], highlightcolor=G["texto"])
        self.after(700, lambda: self.winfo_exists() and self.configure(
            highlightbackground=G["linea"], highlightcolor=G["linea"]))


class Interruptor(tk.Canvas):
    def __init__(self, padre, valor=True, al_cambiar=None):
        super().__init__(padre, width=42, height=24, bg=padre.cget("bg"), highlightthickness=0,
                         cursor="hand2")
        self.valor, self.p, self.al_cambiar = valor, (1.0 if valor else 0.0), al_cambiar
        self.bind("<Button-1>", self.alternar)
        self._dibujar()

    def _dibujar(self):
        self.delete("all")
        rect_redondo(self, 1, 1, 41, 23, 11, fill=mezclar(G["sec_h"], G["prim"], self.p), outline="")
        x = 12 + 18 * self.p
        self.create_oval(x - 8, 4, x + 8, 20, fill=mezclar(G["tenue"], G["bg"], self.p), outline="")

    def alternar(self, _e=None):
        self.valor = not self.valor
        ini, fin = self.p, (1.0 if self.valor else 0.0)

        def paso(t):
            self.p = ini + (fin - ini) * t
            self._dibujar()
        Tween(self, 240, paso, ease=out_back)
        if self.al_cambiar:
            self.al_cambiar(self.valor)


class Deslizador(tk.Canvas):
    PAD = 12

    def __init__(self, padre, minimo, maximo, paso, valor, al_cambiar=None):
        super().__init__(padre, height=28, bg=padre.cget("bg"), highlightthickness=0, cursor="hand2")
        self.mn, self.paso = minimo, paso
        self.mx = max(maximo, minimo + paso)
        self.valor, self.r, self.al_cambiar, self._tw = self._ajustar(valor), 7.0, al_cambiar, None
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Button-1>", self._mover)
        self.bind("<B1-Motion>", self._mover)
        self.bind("<Enter>", lambda e: self._radio(9.5))
        self.bind("<Leave>", lambda e: self._radio(7.0))

    def _ajustar(self, v):
        v = round((v - self.mn) / self.paso) * self.paso + self.mn
        return int(min(max(v, self.mn), self.mx))

    def _radio(self, objetivo):
        if self._tw:
            self._tw.cancel()
        ini = self.r

        def paso(t):
            self.r = ini + (objetivo - ini) * t
            self._dibujar()
        self._tw = Tween(self, 160, paso)

    def _mover(self, e):
        w = self.winfo_width()
        frac = (e.x - self.PAD) / max(1, w - 2 * self.PAD)
        v = self._ajustar(self.mn + frac * (self.mx - self.mn))
        if v != self.valor:
            self.valor = v
            if self.al_cambiar:
                self.al_cambiar(v)
            self._dibujar()

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 30:
            return
        y = h / 2
        x = self.PAD + (self.valor - self.mn) / (self.mx - self.mn) * (w - 2 * self.PAD)
        self.create_line(self.PAD, y, w - self.PAD, y, width=4, fill=G["sec_h"], capstyle="round")
        self.create_line(self.PAD, y, x, y, width=4, fill=G["prim"], capstyle="round")
        self.create_oval(x - self.r, y - self.r, x + self.r, y + self.r, fill=G["blanco"], outline="")


class BarraProgreso(tk.Canvas):
    def __init__(self, padre):
        super().__init__(padre, height=6, bg=padre.cget("bg"), highlightthickness=0)
        self.obj = self.act = 0.0
        self.indeterminado = False
        self._t = None
        self.brillo = False

    def set(self, frac):
        self.obj = max(0.0, min(1.0, frac))
        self.indeterminado = False

    def tick(self, t):
        w = self.winfo_width()
        if w < 10:
            return
        dt = 0.0 if self._t is None else min(0.1, t - self._t)
        self._t = t
        self.act += (self.obj - self.act) * (1 - math.exp(-dt * 7))  # suavizado independiente de los fps
        self.delete("all")
        self.create_line(3, 3, w - 3, 3, width=6, fill=G["sec"], capstyle="round")
        if self.indeterminado:
            pos = (t * 0.7) % 1.4 - 0.25
            x1, x2 = max(3, pos * w), min(w - 3, (pos + 0.25) * w)
            if x2 > x1:
                self.create_line(x1, 3, x2, 3, width=6, fill=G["prim"], capstyle="round")
        elif self.act > 0.002:
            x = 3 + (w - 6) * self.act
            self.create_line(3, 3, x, 3, width=6, fill=G["prim"], capstyle="round")
            if self.brillo and x > 40:
                sx = 3 + ((t * 0.6) % 1) * (x - 3)
                self.create_line(max(3, sx - 40), 3, min(x, sx), 3, width=6, fill=G["blanco"],
                                 capstyle="round")


class Particulas:
    """Cubitos grises que suben flotando al fondo de un canvas."""

    def __init__(self, cv, n=34):
        self.cv, self.n, self.p, self._t = cv, n, [], None

    def _nueva(self, w, h, abajo=True):
        z = random.random()
        s = 3 + z * 9
        x = random.uniform(0, w)
        y = h + s + random.uniform(0, h * 0.3) if abajo else random.uniform(0, h)
        color = mezclar(G["bg"], "#6b6b73", 0.12 + 0.5 * z)
        i = self.cv.create_rectangle(x, y, x + s, y + s, fill=color, outline="", tags="particula")
        self.cv.tag_lower(i)
        return [i, x, y, s, 0.25 + z * 0.9, random.uniform(0, 6.28)]

    def tick(self, t):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        if w < 20 or h < 20:
            return
        if not self.p:
            self.p = [self._nueva(w, h, False) for _ in range(self.n)]
        dt = 0.0 if self._t is None else min(0.1, t - self._t)
        self._t = t
        for k, q in enumerate(self.p):
            q[2] -= q[4] * 30 * dt  # px/seg: igual de rápido a cualquier fps
            if q[2] < -q[3] - 4 or q[1] > w + 20:
                self.cv.delete(q[0])
                self.p[k] = self._nueva(w, h)
                continue
            x = q[1] + math.sin(t * 0.6 + q[5]) * 12
            self.cv.coords(q[0], x, q[2], x + q[3], q[2] + q[3])


class LogoFlotante:
    """Logo que flota, con halo que pulsa y sombra que acompaña el movimiento."""

    def __init__(self, cv, imgs, tam):
        self.cv, self.imgs, self.tam = cv, imgs, tam
        self.brillos = imgs.brillo(tam)
        self.sombra = cv.create_oval(0, 0, 0, 0, fill="#141416", outline="")
        self.halo = cv.create_image(0, 0, image=self.brillos[0])
        self.logo = cv.create_image(0, 0, image=imgs.logo(tam))
        self.x = self.y = 0
        self.escala = 1.0
        self._img = self._halo_img = None

    def mover(self, x, y):
        self.x, self.y = x, y

    def entrada(self, retraso=0):
        self.escala = 0.0

        def iniciar():
            Tween(self.cv, 950, lambda t: setattr(self, "escala", 0.35 + 0.65 * t), ease=out_back)
        self.cv.after(retraso, iniciar)

    def tick(self, t):
        dy = math.sin(t * 1.5) * 8
        if self.escala < 0.3:
            self.cv.itemconfig(self.logo, state="hidden")
            self.cv.itemconfig(self.halo, state="hidden")
            self.cv.itemconfig(self.sombra, state="hidden")
            return
        img = self.imgs.logo(int(self.tam * self.escala / 2) * 2)
        if img is not self._img:  # solo reconfigurar cuando cambia el tamaño
            self._img = img
            self.cv.itemconfig(self.logo, image=img)
        self.cv.itemconfig(self.logo, state="normal")
        self.cv.itemconfig(self.sombra, state="normal")
        self.cv.coords(self.logo, self.x, self.y + dy)
        if self.escala > 0.97:
            k = (math.sin(t * 1.1) + 1) / 2
            halo = self.brillos[round(k * (len(self.brillos) - 1))]
            if halo is not self._halo_img:
                self._halo_img = halo
                self.cv.itemconfig(self.halo, image=halo)
            self.cv.itemconfig(self.halo, state="normal")
            self.cv.coords(self.halo, self.x, self.y + dy)
        else:
            self.cv.itemconfig(self.halo, state="hidden")
        ancho = self.tam * 0.40 * (1 - dy / 70) * min(1, self.escala)
        sy = self.y + self.tam * 0.56
        self.cv.coords(self.sombra, self.x - ancho, sy - 8, self.x + ancho, sy + 8)


def escribir_texto(cv, item, texto, dur=900, retraso=0):
    """Efecto máquina de escribir para un texto de canvas."""
    cv.itemconfig(item, text="")

    def iniciar():
        Tween(cv, dur, lambda t: cv.itemconfig(item, text=texto[:round(len(texto) * t)]), ease=lambda t: t)
    cv.after(retraso, iniciar)


# --- Diálogos ------------------------------------------------------------------
class Dialogo(tk.Toplevel):
    def __init__(self, app, titulo):
        super().__init__(app, bg=G["card"])
        self.app = app
        self.withdraw()
        self.title(titulo)
        self.resizable(False, False)
        self.transient(app)
        self.cuerpo = tk.Frame(self, bg=G["card"], padx=28, pady=24)
        self.cuerpo.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.cerrar)

    def mostrar(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = self.app.winfo_rootx() + (self.app.winfo_width() - w) // 2
        y = self.app.winfo_rooty() + (self.app.winfo_height() - h) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.deiconify()
        barra_oscura(self)
        Tween(self, 260, lambda t: self._alpha(t))
        self.after(60, self._capturar)

    def _alpha(self, v):
        try:
            self.attributes("-alpha", v)
        except tk.TclError:
            pass

    def _capturar(self):
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass

    def cerrar(self):
        """Solo cierra este diálogo; la ventana principal sigue abierta."""
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def titulo(self, texto, sub=None):
        tk.Label(self.cuerpo, text=texto, bg=G["card"], fg=G["texto"], font=fnt(16, "bold"),
                 justify="left").pack(anchor="w")
        if sub:
            tk.Label(self.cuerpo, text=sub, bg=G["card"], fg=G["tenue"], font=fnt(10),
                     wraplength=400, justify="left").pack(anchor="w", pady=(4, 0))


class DialogoAviso(Dialogo):
    def __init__(self, app, titulo, mensaje):
        super().__init__(app, titulo)
        fila = tk.Frame(self.cuerpo, bg=G["card"])
        fila.pack(fill="x")
        ico = tk.Canvas(fila, width=40, height=40, bg=G["card"], highlightthickness=0)
        ico.create_oval(2, 2, 38, 38, fill=G["sec_h"], outline="")
        ico.create_text(20, 20, text="!", fill=G["texto"], font=fnt(16, "bold"))
        ico.pack(side="left", anchor="n", padx=(0, 14))
        texto = tk.Frame(fila, bg=G["card"])
        texto.pack(side="left", fill="x")
        tk.Label(texto, text=titulo, bg=G["card"], fg=G["texto"], font=fnt(14, "bold")).pack(anchor="w")
        tk.Label(texto, text=mensaje, bg=G["card"], fg=G["tenue"], font=fnt(10), wraplength=380,
                 justify="left").pack(anchor="w", pady=(4, 0))
        Boton(self.cuerpo, "Entendido", self.cerrar, "primario", alto=40, ancho=140).pack(anchor="e", pady=(20, 0))
        self.bind("<Return>", lambda e: self.cerrar())
        self.mostrar()


class DialogoTexto(Dialogo):
    def __init__(self, app, titulo, mensaje, etiqueta, valor, al_guardar, validar=None, enlace=None):
        super().__init__(app, titulo)
        self.al_guardar, self.validar = al_guardar, validar
        self.titulo(titulo, mensaje)
        if enlace:
            texto, url = enlace
            l = tk.Label(self.cuerpo, text=texto, bg=G["card"], fg=G["texto"], font=fnt(10, "underline"),
                         cursor="hand2")
            l.pack(anchor="w", pady=(8, 0))
            l.bind("<Button-1>", lambda e: webbrowser.open(url))
        self.campo = Entrada(self.cuerpo, etiqueta, al_enter=self._guardar)
        self.campo.configure(width=420)
        self.campo.pack(fill="x", pady=(16, 4))
        if valor:
            self.campo.var.set(valor)
        self.error = tk.Label(self.cuerpo, text="", bg=G["card"], fg=G["texto"], font=fnt(9))
        self.error.pack(anchor="w")
        fila = tk.Frame(self.cuerpo, bg=G["card"])
        fila.pack(fill="x", pady=(14, 0))
        Boton(fila, "Guardar", self._guardar, "primario", alto=40, ancho=130).pack(side="right")
        Boton(fila, "Cancelar", self.cerrar, "fantasma", alto=40, ancho=110).pack(side="right", padx=8)
        self.mostrar()
        self.after(120, self.campo.e.focus_set)

    def _guardar(self):
        valor = self.campo.get()
        problema = self.validar(valor) if self.validar else (None if valor else "Escribe un valor.")
        if problema:
            self.error.configure(text=problema)
            self.campo.destello()
            return
        self.cerrar()
        self.al_guardar(valor)


class DialogoConfirmar(Dialogo):
    def __init__(self, app, titulo, mensaje, texto_ok, al_ok):
        super().__init__(app, titulo)
        self.al_ok = al_ok
        self.titulo(titulo, mensaje)
        fila = tk.Frame(self.cuerpo, bg=G["card"])
        fila.pack(fill="x", pady=(22, 0))
        Boton(fila, texto_ok, self._ok, "primario", alto=40, ancho=150).pack(side="right")
        Boton(fila, "Cancelar", self.cerrar, "fantasma", alto=40, ancho=110).pack(side="right", padx=8)
        self.bind("<Escape>", lambda e: self.cerrar())
        self.mostrar()

    def _ok(self):
        self.cerrar()
        self.al_ok()


class DialogoActualizacion(Dialogo):
    def __init__(self, app, info):
        super().__init__(app, "Actualización disponible")
        self.titulo(f"NexLauncher {info['version']} está listo",
                    f"Tienes la versión {APP_VERSION}. La nueva ya se descargó y se verificó su firma SHA256.")
        notas = re.sub(r"\r?\n{3,}", "\n\n", info.get("notas", "").strip())[:900]
        if notas:
            caja = tk.Text(self.cuerpo, height=7, width=54, bg=G["card2"], fg="#b4b4bb", relief="flat", wrap="word",
                           font=fnt(9), padx=12, pady=10, highlightthickness=0)
            caja.insert("1.0", notas)
            caja.configure(state="disabled")
            caja.pack(fill="x", pady=(14, 0))
        fila = tk.Frame(self.cuerpo, bg=G["card"])
        fila.pack(fill="x", pady=(18, 0))
        Boton(fila, "Reiniciar y actualizar", self._ahora, "primario", alto=40, ancho=190, brillo=True).pack(side="right")
        Boton(fila, "Al cerrar", self._despues, "fantasma", alto=40, ancho=110).pack(side="right", padx=8)
        self.mostrar()

    def _ahora(self):
        self.app.relanzar = True
        self.cerrar()
        self.app.destroy()  # main() aplica la actualización y abre la versión nueva

    def _despues(self):
        self.cerrar()
        self.app.toast("La actualización se instalará cuando cierres NexLauncher.", "info")


class DialogoMicrosoft(Dialogo):
    def __init__(self, app, url, codigo, al_cancelar):
        super().__init__(app, "Iniciar sesión con Microsoft")
        self.url, self.codigo, self.al_cancelar = url, codigo, al_cancelar
        self.titulo("Conecta tu cuenta de Microsoft",
                    "Abre la página de Microsoft, escribe este código y autoriza el acceso.")
        caja = tk.Frame(self.cuerpo, bg=G["card2"], highlightthickness=1, highlightbackground=G["linea"])
        caja.pack(fill="x", pady=18)
        self.lbl_codigo = tk.Label(caja, text="", bg=G["card2"], fg=G["blanco"], font=(MONO, 26, "bold"),
                                   pady=16)
        self.lbl_codigo.pack()
        link = tk.Label(caja, text=url, bg=G["card2"], fg=G["tenue"], font=fnt(9, "underline"),
                        cursor="hand2", pady=0)
        link.pack(pady=(0, 12))
        link.bind("<Button-1>", lambda e: self.abrir())
        fila = tk.Frame(self.cuerpo, bg=G["card"])
        fila.pack(fill="x")
        fila.columnconfigure((0, 1), weight=1)
        Boton(fila, "Copiar y abrir página", self.abrir, "primario", alto=42, ancho=200,
              brillo=True).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        Boton(fila, "Copiar código", self.copiar, "secundario", alto=42, ancho=150).grid(
            row=0, column=1, sticky="ew", padx=(5, 0))
        espera = tk.Frame(self.cuerpo, bg=G["card"])
        espera.pack(fill="x", pady=(18, 0))
        self.spin = tk.Canvas(espera, width=22, height=22, bg=G["card"], highlightthickness=0)
        self.spin.pack(side="left")
        self.arco = self.spin.create_arc(3, 3, 19, 19, start=0, extent=100, style="arc",
                                         outline=G["texto"], width=2)
        tk.Label(espera, text="Esperando que confirmes en el navegador…", bg=G["card"], fg=G["tenue"],
                 font=fnt(10)).pack(side="left", padx=8)
        Boton(espera, "Cancelar", self.cancelar, "fantasma", alto=34, ancho=100).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.cancelar)
        self.mostrar()
        Tween(self, 700, lambda t: self.lbl_codigo.configure(text=codigo[:round(len(codigo) * t)]),
              ease=lambda t: t)
        self._girar()

    def _girar(self):
        if not self.winfo_exists():
            return
        t = time.perf_counter()
        self.spin.itemconfig(self.arco, start=(-t * 320) % 360, extent=70 + 50 * math.sin(t * 3))
        self.after(max(1, 1000 // FPS), self._girar)

    def copiar(self):
        self.clipboard_clear()
        self.clipboard_append(self.codigo)
        self.app.toast("Código copiado al portapapeles.", "ok")

    def abrir(self):
        self.copiar()
        webbrowser.open(self.url)

    def cancelar(self):
        self.al_cancelar()
        self.cerrar()


def es_descendiente(w, ancestro):
    while w is not None:
        if w is ancestro:
            return True
        w = getattr(w, "master", None)
    return False


class Pestanas(tk.Canvas):
    """Pestañas de la barra superior con subrayado que se desliza."""

    def __init__(self, padre, opciones, al_cambiar):
        super().__init__(padre, height=64, width=60 + 96 * len(opciones), bg=padre.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self.ops, self.al_cambiar, self.i, self.pos, self._tw, self.hover = opciones, al_cambiar, 0, 0.0, None, -1
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self._set_hover(self._indice(e.x)))
        self.bind("<Leave>", lambda e: self._set_hover(-1))

    def _indice(self, x):
        return max(0, min(len(self.ops) - 1, int((x - 30) // 96)))

    def _set_hover(self, h):
        if h != self.hover:
            self.hover = h
            self._dibujar()

    def _dibujar(self):
        self.delete("all")
        h = self.winfo_height()
        for k, op in enumerate(self.ops):
            cerca = max(0.0, 1 - abs(self.pos - k))
            base = G["texto"] if k == self.hover else G["tenue"]
            self.create_text(30 + 96 * k + 48, h / 2, text=op, fill=mezclar(base, G["blanco"], cerca),
                             font=fnt(11, "semi"))
        x = 30 + 96 * self.pos
        self.create_line(x + 24, h - 2, x + 72, h - 2, fill=G["blanco"], width=3, capstyle="round")

    def seleccionar(self, k, avisar=True):
        if k == self.i:
            return
        self.i = k
        if self._tw:
            self._tw.cancel()
        ini = self.pos

        def paso(t):
            self.pos = ini + (k - ini) * t
            self._dibujar()
        self._tw = Tween(self, 380, paso, ease=out_back)
        if avisar:
            self.al_cambiar(k)

    def _click(self, e):
        self.seleccionar(self._indice(e.x))


class TarjetaMod(tk.Frame):
    TAM_ICONO = 52

    def __init__(self, panel, hit, instalado, manual=None):
        super().__init__(panel.interior, bg=G["card"], padx=16, pady=14, highlightthickness=1,
                         highlightbackground=G["linea"])
        self.panel, self.hit, self.manual, self.h, self._tw = panel, hit, manual, 0.0, None
        self.columnconfigure(1, weight=1)
        self.icono = tk.Label(self, bg=G["card"], image=panel.icono_vacio(hit.get("title", "?")))
        self.icono.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 14))
        tk.Label(self, text=hit.get("title", ""), bg=G["card"], fg=G["texto"], font=fnt(12, "bold"),
                 anchor="w").grid(row=0, column=1, sticky="ew")
        meta = []
        if hit.get("author"):
            meta.append(f"por {hit['author']}")
        if hit.get("downloads") is not None:
            meta.append(f"⬇ {formato_num(hit['downloads'])}")
        cats = [c for c in hit.get("categories", []) if c not in CARGADORES][:3]
        if cats:
            meta.append(", ".join(cats))
        tk.Label(self, text="  ·  ".join(meta), bg=G["card"], fg=G["tenue"], font=fnt(9), anchor="w").grid(
            row=1, column=1, sticky="ew")
        self.desc = tk.Label(self, text=hit.get("description", ""), bg=G["card"], fg="#b4b4bb", font=fnt(10),
                             anchor="w", justify="left", wraplength=420)
        self.desc.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        self.btn = Boton(self, "", self._accion, "primario", alto=36, ancho=118, fuente=fnt(10, "semi"))
        self.btn.grid(row=0, column=2, rowspan=3, padx=(14, 0))
        self.set_instalado(instalado)
        self.bind("<Configure>", lambda e: self.desc.configure(wraplength=max(180, e.width - 240)))
        for w in self._todos():
            w.bind("<Enter>", lambda e: self._hover(1.0), add="+")
            w.bind("<Leave>", self._salir, add="+")
        if hit.get("icon_url"):
            panel.pedir_icono(hit["project_id"], hit["icon_url"], self)

    def _todos(self):
        pila, salida = [self], []
        while pila:
            w = pila.pop()
            salida.append(w)
            pila.extend(w.winfo_children())
        return salida

    def _salir(self, e):
        w = self.winfo_containing(*self.winfo_pointerxy())
        if not es_descendiente(w, self):
            self._hover(0.0)

    def _hover(self, objetivo):
        if self._tw:
            self._tw.cancel()
        ini = self.h

        def paso(t):
            self.h = ini + (objetivo - ini) * t
            c = mezclar(G["card"], G["card2"], self.h)
            for w in self._todos():
                if isinstance(w, Boton):
                    w.set_fondo(c)
                else:
                    w.configure(bg=c)
            self.configure(highlightbackground=mezclar(G["linea"], "#4a4a52", self.h))
        self._tw = Tween(self, 180, paso)
        imgs = self.panel.iconos.get(self.hit.get("project_id"))
        if imgs:  # el ícono se ve a color al pasar el mouse
            self.icono.configure(image=imgs[1] if objetivo else imgs[0])

    def poner_icono(self, gris, color):
        self.icono.configure(image=color if self.h > 0.5 else gris)

    def set_instalado(self, valor):
        self.instalado = valor
        self.btn.set_estilo("secundario" if valor else "primario")
        self.btn.set_texto("Quitar" if valor else "Instalar")
        self.btn.set_activo(True)

    def _accion(self):
        if self.instalado:
            self.panel.quitar(self)
        else:
            self.panel.instalar(self)


class PanelMods(tk.Frame):
    TIPOS = {"Mods": "mod", "Modpacks": "modpack", "Recursos": "resourcepack", "Shaders": "shader"}
    ORDEN = {"Relevancia": "relevance", "Descargas": "downloads", "Seguidores": "follows",
             "Recientes": "newest", "Actualizados": "updated"}

    def __init__(self, padre, app, principal):
        super().__init__(padre, bg=G["bg"])
        self.app, self.principal = app, principal
        self.iconos, self._vacios, self.tarjetas, self.esqueletos = {}, {}, [], []
        self.gen = 0
        self.offset, self.total, self.cargando = 0, 0, False
        self.vista = 0  # 0 explorar, 1 instalados
        self.inst = None
        self.scroll_obj = self.scroll_act = 0.0
        self._pool = ThreadPoolExecutor(max_workers=6)
        self._busqueda_pend = None

        cab = tk.Frame(self, bg=G["bg"], padx=28, pady=18)
        cab.pack(fill="x")
        fila = tk.Frame(cab, bg=G["bg"])
        fila.pack(fill="x")
        tk.Label(fila, text="Modrinth", bg=G["bg"], fg=G["texto"], font=fnt(20, "bold")).pack(side="left")
        self.lbl_ctx = tk.Label(fila, text="", bg=G["bg"], fg=G["tenue"], font=fnt(10))
        self.lbl_ctx.pack(side="left", padx=14, pady=(6, 0))
        self.sel_vista = Selector(fila, ["Explorar", "Instalados"], "Explorar", self._vista_cambiada)
        self.sel_vista.configure(width=230)
        self.sel_vista.pack(side="right")
        fila = tk.Frame(cab, bg=G["bg"])
        fila.pack(fill="x", pady=(14, 0))
        self.busqueda = Entrada(fila, "Buscar en Modrinth…", al_enter=lambda: self.buscar(True))
        self.busqueda.configure(bg=G["card2"])
        self.busqueda.pack(side="left", fill="x", expand=True)
        self.busqueda.var.trace_add("write", lambda *a: self._programar_busqueda())
        self.cb_orden = ttk.Combobox(fila, state="readonly", values=list(self.ORDEN), width=12, font=fnt(10),
                                     style="Nex.TCombobox")
        self.cb_orden.set("Relevancia")
        self.cb_orden.pack(side="right", padx=(10, 0))
        self.cb_orden.bind("<<ComboboxSelected>>", lambda e: self.buscar(True))
        self.sel_tipo = Selector(fila, list(self.TIPOS), "Mods", lambda v: self.buscar(True))
        self.sel_tipo.configure(width=360, height=54)
        self.sel_tipo.pack(side="right", padx=(10, 0))

        cuerpo = tk.Frame(self, bg=G["bg"])
        cuerpo.pack(fill="both", expand=True, padx=(28, 12), pady=(0, 12))
        self.lienzo = tk.Canvas(cuerpo, bg=G["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(cuerpo, command=self.lienzo.yview, style="Nex.Vertical.TScrollbar")
        sb.pack(side="right", fill="y", padx=(8, 0))
        self.lienzo.pack(side="left", fill="both", expand=True)
        self.lienzo.configure(yscrollcommand=sb.set)
        self.interior = tk.Frame(self.lienzo, bg=G["bg"])
        self.win = self.lienzo.create_window(0, 0, window=self.interior, anchor="nw")
        self.interior.bind("<Configure>", lambda e: self.lienzo.configure(scrollregion=(0, 0, e.width, e.height)))
        self.lienzo.bind("<Configure>", lambda e: self.lienzo.itemconfig(self.win, width=e.width))
        self.lienzo.bind("<Enter>", lambda e: self._rueda(True))
        self.lienzo.bind("<Leave>", lambda e: self._rueda(False))
        self.mensaje = tk.Label(self.interior, text="", bg=G["bg"], fg=G["tenue"], font=fnt(11), pady=40,
                                justify="center", wraplength=520)

    # ---- contexto (loader + versión elegidos en Inicio)
    def contexto(self):
        """Dónde se instalan los mods: .minecraft, o la carpeta del modpack elegido en Inicio."""
        p = self.principal
        if p.tipo == "modpack":
            m = p.pack_elegido()
            if not m:
                return {"tipo": "vanilla", "mc": None, "clave": ("pack", None),
                        "etiqueta": "Instala un modpack o elige otra opción en Inicio"}
            d = Path(m["dir"])
            return {"tipo": m["tipo"], "mc": m["mc"], "dir": d, "reg": d / "nexlauncher_mods.json",
                    "clave": ("pack", m["dir"]),
                    "etiqueta": f"Modpack {m['nombre']} · {CARGADORES[m['tipo']]} {m['mc']}"}
        mc = p.cb.get()
        if mc not in tuple(p.cb["values"]):
            return {"tipo": p.tipo, "mc": None, "clave": (p.tipo, None), "etiqueta": "Elige una versión en Inicio"}
        return {"tipo": p.tipo, "mc": mc, "dir": None, "reg": None, "clave": (p.tipo, mc),
                "etiqueta": f"{CARGADORES[p.tipo]} · Minecraft {mc}  ·  .minecraft"}

    def al_mostrar(self):
        ctx = self.contexto()
        self.lbl_ctx.configure(text=ctx["etiqueta"])
        if self.inst is None or getattr(self, "_clave", None) != ctx["clave"]:
            self._clave = ctx["clave"]
            self.inst = Instancia(ctx["tipo"], ctx["mc"], ctx.get("dir"), ctx.get("reg")) if ctx["mc"] else None
            self.buscar(True)
        else:  # refrescar estado de botones (pudieron quitarse archivos a mano)
            for t in self.tarjetas:
                if t.manual is None:
                    t.set_instalado(self._esta_instalado(t.hit["project_id"], t.tp))

    def _esta_instalado(self, pid, tp):
        if tp == "modpack":
            return any(m.get("project_id") == pid for m in listar_modpacks())
        return bool(self.inst) and self.inst.instalado(pid)

    # ---- búsqueda
    def _programar_busqueda(self):
        if self._busqueda_pend:
            self.after_cancel(self._busqueda_pend)
        self._busqueda_pend = self.after(450, lambda: self.buscar(True))

    def _vista_cambiada(self, valor):
        self.vista = 0 if valor == "Explorar" else 1
        self.buscar(True)

    def _limpiar(self):
        for t in self.tarjetas + self.esqueletos:
            t.destroy()
        self.tarjetas, self.esqueletos = [], []
        self.mensaje.pack_forget()
        self.scroll_obj = self.scroll_act = 0.0
        self.lienzo.yview_moveto(0)

    def _aviso(self, texto):
        self.mensaje.configure(text=texto)
        self.mensaje.pack(fill="x")

    def _esqueletos(self, n=5):
        for _ in range(n):
            f = tk.Frame(self.interior, bg=G["card"], height=96, highlightthickness=1, highlightbackground=G["linea"])
            f.pack(fill="x", pady=(0, 10))
            f.pack_propagate(False)
            cv = tk.Canvas(f, bg=G["card"], highlightthickness=0, height=94)
            cv.pack(fill="both", expand=True)
            cv.create_rectangle(16, 20, 68, 72, outline="", tags="b")
            cv.create_rectangle(84, 24, 260, 38, outline="", tags="b")
            cv.create_rectangle(84, 48, 420, 58, outline="", tags="b")
            cv.create_rectangle(84, 66, 340, 76, outline="", tags="b")
            f.cv = cv
            self.esqueletos.append(f)

    def buscar(self, reiniciar=False):
        ctx = self.contexto()
        tipo, mc = ctx["tipo"], ctx["mc"]
        tp = self.TIPOS[self.sel_tipo.ops[self.sel_tipo.i]]
        if reiniciar:
            self.gen += 1
            self.offset, self.total = 0, 0
            self._limpiar()
        if tp != "modpack":  # los modpacks traen su propia versión y loader
            if not mc:
                self._aviso("Primero elige una versión de Minecraft (o un modpack) en Inicio.")
                return
            if tp in ("mod", "shader") and tipo == "vanilla":
                self._aviso("Para mods y shaders elige Fabric, Forge, NeoForge o un modpack en Inicio.\n"
                            "Los paquetes de recursos sí funcionan en Vanilla.")
                return
        if self.vista == 1:
            self._mostrar_instalados()
            return
        if self.cargando and not reiniciar:
            return
        self.cargando = True
        gen, offset = self.gen, self.offset
        if offset == 0:
            self._esqueletos()
        texto, orden = self.busqueda.get(), self.ORDEN[self.cb_orden.get()]
        b_tipo, b_mc = ("vanilla", None) if tp == "modpack" else (tipo, mc)

        def trabajo():
            try:
                r = buscar_modrinth(texto, tp, b_tipo, b_mc, orden, offset)
            finally:
                self.cargando = False
            self.app.cola.put(("llamar", lambda: self._resultados(gen, r, tp)))

        def fallo():
            if gen == self.gen:
                for s in self.esqueletos:
                    s.destroy()
                self.esqueletos = []
                if self.tarjetas:
                    self.app.toast("No se pudieron cargar más resultados de Modrinth.", "error")
                else:
                    self._aviso("No se pudo conectar con Modrinth. Revisa tu conexión.")
        self.app.en_hilo(trabajo, al_error=fallo, silencioso=True)

    def _resultados(self, gen, r, tp):
        if gen != self.gen:
            return  # llegó tarde: el usuario ya cambió la búsqueda
        for s in self.esqueletos:
            s.destroy()
        self.esqueletos = []
        hits = r.get("hits", [])
        self.total = r.get("total_hits", 0)
        self.offset += len(hits)
        if not hits and not self.tarjetas:
            self._aviso("No se encontró nada con esos filtros.")
            return
        packs = {m.get("project_id") for m in listar_modpacks()} if tp == "modpack" else set()
        self._aparecer([(h, (h["project_id"] in packs) if tp == "modpack" else self._esta_instalado(h["project_id"], tp),
                         None) for h in hits], gen, tp)

    def _aparecer(self, lista, gen, tp):
        """Las tarjetas entran una tras otra (efecto cascada)."""
        if not lista or gen != self.gen:
            return
        hit, instalado, manual = lista[0]
        t = TarjetaMod(self, hit, instalado, manual)
        t.tp = tp
        t.pack(fill="x", pady=(0, 10))
        self.tarjetas.append(t)
        self.after(28, lambda: self._aparecer(lista[1:], gen, tp))

    def _mostrar_instalados(self):
        gen = self.gen
        tp = self.TIPOS[self.sel_tipo.ops[self.sel_tipo.i]]
        if tp == "modpack":
            packs = listar_modpacks()
            if not packs:
                self._aviso("Todavía no tienes modpacks.\nBúscalos en Explorar y dale a Instalar.")
                return
            self._aparecer([({"project_id": m.get("project_id"), "title": m["nombre"], "icon_url": m.get("icon_url"),
                              "description": f"{CARGADORES[m['tipo']]} · Minecraft {m['mc']}  ·  Versión "
                                             f"{m.get('version', '')}  ·  {Path(m['dir']).name}"}, True, None)
                            for m in packs], gen, tp)
            return
        inst = self.inst
        if inst is None:
            return
        ids = [pid for pid, e in inst.reg.items() if e.get("tipo") == tp and inst.instalado(pid)]
        manuales = inst.manuales() if tp == "mod" else []
        if not ids and not manuales:
            self._aviso("Todavía no hay nada instalado aquí.\nBúscalo en Explorar y dale a Instalar.")
            return
        self._esqueletos(min(4, len(ids) + len(manuales)))

        def trabajo():
            info = {}
            if ids:
                for p in modrinth("/projects", ids=ids):
                    info[p["id"]] = p
            self.app.cola.put(("llamar", lambda: listo(info)))

        def listo(info):
            if gen != self.gen:
                return
            for s in self.esqueletos:
                s.destroy()
            self.esqueletos = []
            lista = []
            for pid in ids:
                e, p = inst.reg[pid], info.get(pid, {})
                lista.append(({"project_id": pid, "title": e["titulo"], "icon_url": p.get("icon_url"),
                               "description": (f"{CARGADORES.get(e.get('cargador'), '')} {e.get('mc', '')}  ·  "
                                               f"Versión {e['version']}  ·  {e['archivo']}").strip(" ·"),
                               "categories": p.get("categories", [])}, True, None))
            for archivo in manuales:
                lista.append(({"project_id": None, "title": archivo, "description": "Agregado a mano a la carpeta mods."},
                              True, archivo))
            self._aparecer(lista, gen, tp)

        def sin_red():  # sin conexión: se muestra igual, solo sin íconos
            listo({})
        self.app.en_hilo(trabajo, al_error=sin_red, silencioso=True)

    # ---- instalar / quitar
    def instalar(self, tarjeta):
        if tarjeta.tp == "modpack":
            self.app.instalar_modpack(tarjeta)
            return
        if self.inst is None:
            self.app.toast("Elige primero una versión o un modpack en Inicio.", "error")
            return
        tarjeta.btn.set_activo(False)
        tarjeta.btn.set_texto("Instalando…")
        hit, inst = tarjeta.hit, self.inst

        def trabajo():
            hechos = inst.instalar(hit["project_id"], tarjeta.tp, hit["title"])

            def listo():
                if tarjeta.winfo_exists():
                    tarjeta.set_instalado(True)
                extra = len(hechos) - 1
                self.app.toast(f"{hit['title']} instalado" + (f" con {extra} dependencia{'s' if extra > 1 else ''}."
                                                              if extra > 0 else "."), "ok")
                self.app.log(f"[Modrinth] Instalado: {', '.join(hechos)} → {inst.dir}")
            self.app.cola.put(("llamar", listo))

        def fallo():
            if tarjeta.winfo_exists():
                tarjeta.set_instalado(False)
        self.app.en_hilo(trabajo, al_error=fallo)

    def _quitar_tarjeta(self, tarjeta):
        self.app.toast(f"Se quitó {tarjeta.hit['title']}.", "info")
        if self.vista == 1:
            if tarjeta in self.tarjetas:
                self.tarjetas.remove(tarjeta)
            tarjeta.destroy()
            if not self.tarjetas:
                self._aviso("Todavía no hay nada instalado aquí.\nBúscalo en Explorar y dale a Instalar.")
        elif tarjeta.winfo_exists():
            tarjeta.set_instalado(False)

    def quitar(self, tarjeta):
        if self.app.jugando:
            self.app.toast("Cierra Minecraft antes de quitar mods o modpacks.", "error")
            return
        if tarjeta.tp == "modpack":
            pack = next((m for m in listar_modpacks() if m.get("project_id") == tarjeta.hit["project_id"]), None)
            if not pack:
                tarjeta.set_instalado(False)
                return

            def borrar():
                try:
                    quitar_modpack(pack)
                except OSError as e:
                    self.app.toast(f"No se pudo borrar el modpack (¿está abierto?): {e}", "error")
                    return
                self._quitar_tarjeta(tarjeta)
                if self.principal.tipo == "modpack":
                    self.principal.llenar_versiones()
            DialogoConfirmar(self.app, "Quitar modpack",
                             f"Se borrará la carpeta «{Path(pack['dir']).name}» con sus mods, configuraciones y "
                             "mundos. Esto no se puede deshacer.", "Borrar modpack", borrar)
            return
        try:
            if tarjeta.manual:
                (self.inst.dir / "mods" / tarjeta.manual).unlink(missing_ok=True)
            else:
                self.inst.quitar(tarjeta.hit["project_id"])
        except (LauncherError, OSError) as e:
            self.app.toast(str(e), "error")
            return
        self._quitar_tarjeta(tarjeta)

    # ---- íconos
    def icono_vacio(self, titulo):
        letra = (titulo[:1] or "?").upper()
        if letra not in self._vacios:
            t = TarjetaMod.TAM_ICONO * 2
            img = Image.new("RGBA", (t, t), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, t - 1, t - 1), radius=22, fill=(44, 44, 49, 255))
            self._vacios[letra] = ImageTk.PhotoImage(img.resize((TarjetaMod.TAM_ICONO,) * 2, Image.LANCZOS))
        return self._vacios[letra]

    def pedir_icono(self, pid, url, tarjeta):
        if pid in self.iconos:
            tarjeta.poner_icono(*self.iconos[pid])
            return
        cache = LAUNCHER_DIR / "cache" / "iconos" / f"{pid}.png"

        def trabajo():
            try:
                if cache.exists():
                    img = Image.open(cache)
                else:
                    req = urllib.request.Request(url, headers={"User-Agent": UA_MODRINTH})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        img = Image.open(io.BytesIO(r.read()))
                    img = img.convert("RGBA").resize((TarjetaMod.TAM_ICONO * 2,) * 2, Image.LANCZOS)
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    img.save(cache)
                img = img.convert("RGBA")
            except (urllib.error.URLError, OSError, ValueError) as e:
                log_error(f"Ícono de {pid} no disponible: {e!r}")
                return
            self.app.cola.put(("llamar", lambda: self._icono_listo(pid, img, tarjeta)))
        self._pool.submit(trabajo)

    def _icono_listo(self, pid, img, tarjeta):
        t = TarjetaMod.TAM_ICONO
        mascara = Image.new("L", (t * 4, t * 4), 0)
        ImageDraw.Draw(mascara).rounded_rectangle((0, 0, t * 4 - 1, t * 4 - 1), radius=44, fill=255)
        mascara = mascara.resize((t, t), Image.LANCZOS)
        color = img.resize((t, t), Image.LANCZOS)
        alfa = Image.composite(color.getchannel("A"), Image.new("L", (t, t), 0), mascara)
        color.putalpha(alfa)
        gris = ImageOps.grayscale(color.convert("RGB")).convert("RGBA")
        gris.putalpha(alfa)
        self.iconos[pid] = (ImageTk.PhotoImage(gris), ImageTk.PhotoImage(color))
        if tarjeta.winfo_exists():
            tarjeta.poner_icono(*self.iconos[pid])

    # ---- desplazamiento suave
    def _rueda(self, activo):
        if activo:
            self.bind_all("<MouseWheel>", lambda e: self._desplazar(-e.delta / 120))
            self.bind_all("<Button-4>", lambda e: self._desplazar(-1))
            self.bind_all("<Button-5>", lambda e: self._desplazar(1))
        else:
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.unbind_all(ev)

    def _alto(self):
        return max(1, self.interior.winfo_height() - self.lienzo.winfo_height())

    def _desplazar(self, pasos):
        self.scroll_obj = max(0.0, min(self._alto(), self.scroll_obj + pasos * 110))

    def tick(self, t, dt):
        for s in self.esqueletos:
            s.cv.itemconfig("b", fill=mezclar(G["card2"], "#2e2e34", (math.sin(t * 4) + 1) / 2))
        total = self.interior.winfo_height()
        if total <= 1:
            return
        if abs(self.scroll_obj - self.scroll_act) > 0.5:
            self.scroll_act += (self.scroll_obj - self.scroll_act) * (1 - math.exp(-dt * 14))
            self.lienzo.yview_moveto(self.scroll_act / total)
        else:  # la barra lateral pudo moverse a mano: sincronizar
            self.scroll_act = self.scroll_obj = self.lienzo.yview()[0] * total
        # carga infinita al acercarse al final
        if (self.vista == 0 and not self.cargando and self.tarjetas and self.offset < self.total
                and self.scroll_act > self._alto() - 300):
            self.buscar(False)


# --- Pantallas -------------------------------------------------------------------
class Pantalla(tk.Frame):
    def __init__(self, app):
        super().__init__(app.contenedor, bg=G["bg"])
        self.app = app

    def al_mostrar(self):
        pass

    def tick(self, t):
        pass


class PantallaCarga(Pantalla):
    def __init__(self, app):
        super().__init__(app)
        self.cv = tk.Canvas(self, bg=G["bg"], highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.part = Particulas(self.cv, 26)
        self.logo = LogoFlotante(self.cv, app.imgs, 200)
        self.titulo = self.cv.create_text(0, 0, text="", fill=G["texto"], font=fnt(28, "bold"))
        self.pista = self.cv.create_oval(0, 0, 0, 0, outline=G["linea"], width=3)
        self.arco = self.cv.create_arc(0, 0, 0, 0, start=0, extent=90, style="arc", outline=G["texto"], width=3)
        self.estado = self.cv.create_text(0, 0, text="Iniciando…", fill=G["tenue"], font=fnt(10))
        self.cv.bind("<Configure>", lambda e: self._layout())

    def _layout(self):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        x, y = w / 2, h * 0.38
        self.logo.mover(x, y)
        self.cv.coords(self.titulo, x, y + 150)
        r = 13
        for item in (self.pista, self.arco):
            self.cv.coords(item, x - r, y + 200 - r, x + r, y + 200 + r)
        self.cv.coords(self.estado, x, y + 238)

    def al_mostrar(self):
        self.logo.entrada(150)
        escribir_texto(self.cv, self.titulo, APP_NAME, 700, 500)

    def set_estado(self, texto):
        self.cv.itemconfig(self.estado, text=texto)

    def tick(self, t):
        self.part.tick(t)
        self.logo.tick(t)
        self.cv.itemconfig(self.arco, start=(-t * 300) % 360, extent=70 + 50 * math.sin(t * 3))


class PantallaLogin(Pantalla):
    ANCHO = 330

    def __init__(self, app):
        super().__init__(app)
        self.cv = tk.Canvas(self, bg=G["bg"], highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.part = Particulas(self.cv, 40)
        self.logo = LogoFlotante(self.cv, app.imgs, 250)
        self.nombre_app = self.cv.create_text(0, 0, text=APP_NAME, fill=G["texto"], font=fnt(32, "bold"))
        self.lema = self.cv.create_text(0, 0, text="", fill=G["tenue"], font=fnt(12))
        self.ver = self.cv.create_text(0, 0, text=f"v{APP_VERSION}", fill="#3d3d43", font=fnt(9), anchor="se")
        self.fondo = self.cv.create_polygon(0, 0, 0, 0, smooth=True, splinesteps=16, fill=G["card"],
                                            outline=G["linea"])
        self.tarjeta = tk.Frame(self.cv, bg=G["card"], padx=32, pady=24)
        self.win = self.cv.create_window(0, 0, window=self.tarjeta)
        self.off_y = self.off_x = 0.0
        self._contenido()
        self.cv.bind("<Configure>", lambda e: self._layout())
        self.tarjeta.bind("<Configure>", lambda e: self._layout())

    def _contenido(self):
        t, card = self.tarjeta, G["card"]
        tk.Label(t, text="Iniciar sesión", bg=card, fg=G["texto"], font=fnt(21, "bold")).pack(anchor="w")
        tk.Label(t, text="Elige cómo quieres entrar a jugar.", bg=card, fg=G["tenue"],
                 font=fnt(10)).pack(anchor="w", pady=(2, 18))
        self.btn_ms = Boton(t, "Continuar con Microsoft", self.app.login_microsoft, "primario", alto=48,
                            ancho=self.ANCHO, fuente=fnt(11, "semi"), icono=icono_microsoft, brillo=True)
        self.btn_ms.pack(fill="x")
        sep = tk.Frame(t, bg=card)
        sep.pack(fill="x", pady=14)
        sep.columnconfigure((0, 2), weight=1)
        tk.Frame(sep, bg=G["linea"], height=1).grid(row=0, column=0, sticky="ew")
        tk.Label(sep, text="  o juega sin conexión  ", bg=card, fg=G["tenue"], font=fnt(9)).grid(row=0, column=1)
        tk.Frame(sep, bg=G["linea"], height=1).grid(row=0, column=2, sticky="ew")
        self.campo = Entrada(t, "Nombre de jugador", al_enter=self.offline)
        self.campo.pack(fill="x")
        self.btn_off = Boton(t, "Jugar offline", self.offline, "secundario", alto=44, ancho=self.ANCHO,
                             fuente=fnt(11, "semi"))
        self.btn_off.pack(fill="x", pady=(10, 0))
        fila = tk.Frame(t, bg=card)
        fila.pack(fill="x", pady=(14, 0))
        self.sw = Interruptor(fila, bool(self.app.cfg.get("recordar", True)), self.app.set_recordar)
        self.sw.pack(side="left")
        tk.Label(fila, text="Mantener sesión iniciada", bg=card, fg=G["texto"], font=fnt(10)).pack(
            side="left", padx=10)
        self.zona = tk.Frame(t, bg=card)
        self.zona.pack(fill="x")
        pie = tk.Label(t, text="Configurar Client ID de Azure", bg=card, fg=G["tenue"],
                       font=fnt(9, "underline"), cursor="hand2")
        pie.pack(anchor="w", pady=(14, 0))
        pie.bind("<Button-1>", lambda e: self.app.configurar_client_id())
        pie.bind("<Enter>", lambda e: pie.configure(fg=G["texto"]))
        pie.bind("<Leave>", lambda e: pie.configure(fg=G["tenue"]))

    def refrescar_cuentas(self):
        for w in self.zona.winfo_children():
            w.destroy()
        recientes = list(reversed(self.app.cuentas.lista))[:2]
        if not recientes:
            return
        tk.Label(self.zona, text="CUENTAS GUARDADAS", bg=G["card"], fg=G["tenue"],
                 font=fnt(8, "bold")).pack(anchor="w", pady=(14, 6))
        for c in recientes:
            fila = tk.Frame(self.zona, bg=G["card"])
            fila.pack(fill="x", pady=2)
            tipo = "Microsoft" if c["type"] == "microsoft" else "Offline"
            Boton(fila, f"{c['name']}  ·  {tipo}", lambda c=c: self.app.entrar_con(c), "secundario",
                  alto=36, ancho=self.ANCHO - 44, fuente=fnt(10)).pack(side="left", fill="x", expand=True)
            Boton(fila, "✕", lambda c=c: self.app.quitar_cuenta(c), "fantasma", alto=36, ancho=38,
                  fuente=fnt(10)).pack(side="right", padx=(6, 0))

    def _layout(self):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        if w < 50:
            return
        lx, ly = w * 0.29, h * 0.40
        self.logo.mover(lx, ly)
        self.cv.coords(self.nombre_app, lx, ly + 175)
        self.cv.coords(self.lema, lx, ly + 215)
        self.cv.coords(self.ver, w - 16, h - 12)
        tw, th = self.tarjeta.winfo_reqwidth(), self.tarjeta.winfo_reqheight()
        cx = min(w * 0.71, w - tw / 2 - 40) + self.off_x
        cy = max(h / 2, th / 2 + 24) + self.off_y
        self.cv.coords(self.win, cx, cy)
        m = 10  # margen para que las esquinas del frame queden dentro del fondo redondeado
        self.cv.coords(self.fondo, *puntos_redondeados(cx - tw / 2 - m, cy - th / 2 - m,
                                                       cx + tw / 2 + m, cy + th / 2 + m, 22))

    def al_mostrar(self):
        self.ocupado(False)
        self.refrescar_cuentas()
        self.logo.entrada(120)
        escribir_texto(self.cv, self.lema, "Tu acceso directo a Minecraft.", 900, 650)

        def paso(t):
            self.off_y = 70 * (1 - t)
            self._layout()
        Tween(self, 750, paso, ease=out_cubic)

    def sacudir(self):
        def paso(t):
            self.off_x = math.sin(t * math.pi * 6) * (1 - t) * 14
            self._layout()
        Tween(self, 480, paso, ease=lambda t: t)

    def esperando_navegador(self):
        self.btn_ms.set_activo(True)
        self.btn_off.set_activo(False)
        self.btn_ms.set_texto("Esperando navegador · Cancelar")

    def ocupado(self, valor, texto=None):
        self.btn_ms.set_activo(not valor)
        self.btn_off.set_activo(not valor)
        self.btn_ms.set_texto(texto if (valor and texto) else "Continuar con Microsoft")

    def offline(self):
        nombre = self.campo.get()
        if not NOMBRE_OFFLINE_RE.match(nombre):
            self.campo.destello()
            self.sacudir()
            self.app.toast("El nombre debe tener de 3 a 16 caracteres: letras, números o _.", "error")
            return
        self.app.login_offline(nombre)

    def tick(self, t):
        self.part.tick(t)
        self.logo.tick(t)


OPCIONES_INICIO = {**CARGADORES, "modpack": "Modpacks"}


class PantallaPrincipal(Pantalla):
    ANCHO = 340

    def __init__(self, app):
        super().__init__(app)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.cuenta = None
        self._barra_superior()
        tk.Frame(self, bg=G["linea"], height=1).grid(row=1, column=0, sticky="ew")

        self.cuerpo = tk.Frame(self, bg=G["bg"])
        self.cuerpo.grid(row=2, column=0, sticky="nsew")
        self.cv = tk.Canvas(self.cuerpo, bg=G["bg"], highlightthickness=0)
        self.cv.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.part = Particulas(self.cv, 36)
        self.logo = LogoFlotante(self.cv, app.imgs, 230)
        self.saludo = self.cv.create_text(0, 0, text="", fill=G["texto"], font=fnt(24, "bold"))
        self.sub = self.cv.create_text(0, 0, text="", fill=G["tenue"], font=fnt(11))
        self.fondo = self.cv.create_polygon(0, 0, 0, 0, smooth=True, splinesteps=16, fill=G["card"],
                                            outline=G["linea"])
        self.tarjeta = tk.Frame(self.cv, bg=G["card"], padx=26, pady=24)
        self.win = self.cv.create_window(0, 0, window=self.tarjeta)
        self._controles()
        self._cajon_registro()
        self.mods = PanelMods(self.cuerpo, app, self)
        self._t_ant = None
        self.off_y = 0.0
        self.cv.bind("<Configure>", lambda e: self._layout())
        self.tarjeta.bind("<Configure>", lambda e: self._layout())

        inf = tk.Frame(self, bg=G["bg2"], padx=24, pady=12)
        inf.grid(row=3, column=0, sticky="ew")
        inf.columnconfigure(0, weight=1)
        self.estado = tk.Label(inf, text="Listo para jugar.", bg=G["bg2"], fg=G["tenue"], font=fnt(10),
                               anchor="w")
        self.estado.grid(row=0, column=0, sticky="ew")
        self.pct = tk.Label(inf, text="", bg=G["bg2"], fg=G["texto"], font=fnt(10, "semi"))
        self.pct.grid(row=0, column=1, sticky="e")
        self.barra = BarraProgreso(inf)
        self.barra.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 2))

    def _barra_superior(self):
        top = tk.Frame(self, bg=G["bg2"], height=64)
        top.grid(row=0, column=0, sticky="ew")
        top.pack_propagate(False)
        tk.Label(top, image=self.app.imgs.logo(36), bg=G["bg2"]).pack(side="left", padx=(18, 10))
        tk.Label(top, text=APP_NAME, bg=G["bg2"], fg=G["texto"], font=fnt(14, "bold")).pack(side="left")
        self.pestanas = Pestanas(top, ["Inicio", "Mods"], self._seccion)
        self.pestanas.pack(side="left", padx=(24, 0))
        self.btn_salir = Boton(top, "Cerrar sesión", self.app.cerrar_sesion, "fantasma", alto=36, ancho=124,
                               fuente=fnt(10))
        self.btn_salir.pack(side="right", padx=(4, 18), pady=(24, 4))
        chip = tk.Frame(top, bg=G["bg2"])
        chip.pack(side="right", padx=8)
        self.avatar = tk.Canvas(chip, width=36, height=36, bg=G["bg2"], highlightthickness=0)
        self.avatar.pack(side="left")
        datos = tk.Frame(chip, bg=G["bg2"])
        datos.pack(side="left", padx=(10, 0))
        self.lbl_nombre = tk.Label(datos, text="", bg=G["bg2"], fg=G["texto"], font=fnt(10, "semi"))
        self.lbl_nombre.pack(anchor="w")
        self.lbl_tipo = tk.Label(datos, text="", bg=G["bg2"], fg=G["tenue"], font=fnt(8))
        self.lbl_tipo.pack(anchor="w")

    def _controles(self):
        c, card = self.tarjeta, G["card"]
        guardado = self.app.cfg.get("cargador", "vanilla")
        self.tipo = guardado if guardado in OPCIONES_INICIO else "vanilla"
        self._loaders, self._packs = [], []
        self.sel = Selector(c, list(OPCIONES_INICIO.values()), OPCIONES_INICIO[self.tipo], self._tipo_cambiado)
        self.sel.configure(width=self.ANCHO)
        self.sel.pack(fill="x", pady=(0, 16))
        fila = tk.Frame(c, bg=card)
        fila.pack(fill="x")
        self.lbl_mc = tk.Label(fila, text="MINECRAFT", bg=card, fg=G["tenue"], font=fnt(8, "bold"))
        self.lbl_mc.pack(side="left")
        self.fila_snap = tk.Frame(fila, bg=card)
        self.sw_snap = Interruptor(self.fila_snap, bool(self.app.cfg.get("mostrar_snapshots")), self._snapshots)
        self.sw_snap.pack(side="right")
        tk.Label(self.fila_snap, text="Snapshots", bg=card, fg=G["tenue"], font=fnt(9)).pack(side="right", padx=8)
        self.cb = ttk.Combobox(c, state="readonly", font=fnt(11), style="Nex.TCombobox", height=14)
        self.cb.pack(fill="x", pady=(6, 12))
        self.cb.bind("<<ComboboxSelected>>", lambda e: self._version_cambiada())
        self.zona_loader = tk.Frame(c, bg=card)
        self.lbl_loader = tk.Label(self.zona_loader, text="", bg=card, fg=G["tenue"], font=fnt(8, "bold"))
        self.lbl_loader.pack(anchor="w")
        self.cb_loader = ttk.Combobox(self.zona_loader, state="readonly", font=fnt(11), style="Nex.TCombobox",
                                      height=14)
        self.cb_loader.pack(fill="x", pady=(6, 12))
        self.cb_loader.bind("<<ComboboxSelected>>", lambda e: self._loader_cambiado())
        self.fila_mem = tk.Frame(c, bg=card)
        self.fila_mem.pack(fill="x", pady=(4, 0))
        tk.Label(self.fila_mem, text="MEMORIA", bg=card, fg=G["tenue"], font=fnt(8, "bold")).pack(side="left")
        self.lbl_ram = tk.Label(self.fila_mem, text="", bg=card, fg=G["texto"], font=fnt(10, "semi"))
        self.lbl_ram.pack(side="right")
        maximo = max(2048, min(ram_total_mb() - 1024, 32768)) // 512 * 512
        self.ram = Deslizador(c, 1024, maximo, 512, int(self.app.cfg.get("ram_mb", 2048)), self._ram)
        self.ram.configure(width=self.ANCHO)
        self.ram.pack(fill="x", pady=(2, 18))
        self._ram(self.ram.valor)
        self.btn_jugar = Boton(c, "JUGAR", self.app.jugar, "primario", alto=58, ancho=self.ANCHO,
                               fuente=fnt(15, "bold"), radio=12, brillo=True)
        self.btn_jugar.pack(fill="x")
        fila = tk.Frame(c, bg=card)
        fila.pack(fill="x", pady=(10, 0))
        fila.columnconfigure((0, 1), weight=1)
        Boton(fila, "Carpeta del juego", self.app.abrir_carpeta, "fantasma", alto=36, ancho=140,
              fuente=fnt(10)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_reg = Boton(fila, "Ver registro", self.alternar_registro, "fantasma", alto=36, ancho=140,
                             fuente=fnt(10))
        self.btn_reg.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _cajon_registro(self):
        self.cajon = tk.Frame(self.cv, bg=G["card"], highlightthickness=1, highlightbackground=G["linea"])
        cab = tk.Frame(self.cajon, bg=G["card"], padx=14, pady=8)
        cab.pack(fill="x")
        tk.Label(cab, text="REGISTRO", bg=G["card"], fg=G["tenue"], font=fnt(8, "bold")).pack(side="left")
        Boton(cab, "✕", self.alternar_registro, "fantasma", alto=28, ancho=32, fuente=fnt(9)).pack(side="right")
        Boton(cab, "Limpiar", self.limpiar_registro, "fantasma", alto=28, ancho=72, fuente=fnt(9)).pack(
            side="right", padx=4)
        cuerpo = tk.Frame(self.cajon, bg=G["bg2"])
        cuerpo.pack(fill="both", expand=True)
        self.txt = tk.Text(cuerpo, bg=G["bg2"], fg="#a6a6ae", font=(MONO, 9), relief="flat", wrap="word",
                           state="disabled", highlightthickness=0, padx=12, pady=8, insertbackground=G["texto"],
                           selectbackground=G["sec_h"])
        sb = ttk.Scrollbar(cuerpo, command=self.txt.yview, style="Nex.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.tag_configure("warn", foreground="#d4d4d8")
        self.txt.tag_configure("error", foreground=G["blanco"], font=(MONO, 9, "bold"))
        self.win_cajon = self.cv.create_window(-3000, 0, window=self.cajon, anchor="nw")
        self.cajon_p, self.cajon_abierto, self._tw_cajon = 0.0, False, None

    def _layout(self):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        if w < 50:
            return
        tw, th = self.tarjeta.winfo_reqwidth(), self.tarjeta.winfo_reqheight()
        cx = w - 44 - tw / 2
        cy = max(h / 2, th / 2 + 20) + self.off_y
        self.cv.coords(self.win, cx, cy)
        m = 10
        self.cv.coords(self.fondo, *puntos_redondeados(cx - tw / 2 - m, cy - th / 2 - m,
                                                       cx + tw / 2 + m, cy + th / 2 + m, 20))
        izq = w - tw - 2 * m - 44  # espacio a la izquierda de la tarjeta
        lx, ly = izq / 2, h * 0.38
        self.logo.mover(lx, ly)
        self.cv.coords(self.saludo, lx, ly + 165)
        self.cv.coords(self.sub, lx, ly + 200)
        ancho_cajon = max(260, izq - 48)
        x = 24 - (ancho_cajon + 60) * (1 - self.cajon_p)
        self.cv.coords(self.win_cajon, x, 24)
        self.cv.itemconfig(self.win_cajon, width=ancho_cajon, height=max(120, h - 48))

    def al_mostrar(self):
        self.logo.entrada(150)

        def paso(t):
            self.off_y = 70 * (1 - t)
            self._layout()
        Tween(self, 750, paso)
        if self.cuenta:
            escribir_texto(self.cv, self.saludo, f"Hola, {self.cuenta['name']}", 650, 450)
        self._version_cambiada(guardar=False)

    def set_cuenta(self, cuenta):
        self.cuenta = cuenta
        self.lbl_nombre.configure(text=cuenta["name"])
        self.lbl_tipo.configure(text="Cuenta Microsoft" if cuenta["type"] == "microsoft" else "Cuenta offline")
        self.cv.itemconfig(self.saludo, text=f"Hola, {cuenta['name']}")
        self._avatar_letra()
        if cuenta["type"] == "microsoft":
            self.app.en_hilo(self._cargar_skin, cuenta["uuid"], silencioso=True)

    def _avatar_letra(self):
        self.avatar.delete("all")
        self.avatar.create_oval(1, 1, 35, 35, fill=G["prim"], outline="")
        self.avatar.create_text(18, 18, text=self.cuenta["name"][:1].upper(), fill=G["prim_txt"],
                                font=fnt(13, "bold"))

    def _cargar_skin(self, uuid_):
        """Cara de la skin (en gris). Si falla, se queda la inicial."""
        try:
            req = urllib.request.Request(f"https://mc-heads.net/avatar/{uuid_}/64", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                datos = r.read()
            img = Image.open(io.BytesIO(datos))
        except (urllib.error.URLError, OSError, ValueError) as e:
            log_error(f"No se pudo cargar la skin: {e!r}")
            return

        def poner():
            if self.cuenta and self.cuenta["uuid"] == uuid_:
                self._img_avatar = avatar_circular(img, 36)
                self.avatar.delete("all")
                self.avatar.create_image(18, 18, image=self._img_avatar)
        self.app.cola.put(("llamar", poner))

    def _tipo_cambiado(self, etiqueta):
        self.tipo = next(k for k, v in OPCIONES_INICIO.items() if v == etiqueta)
        self.app.cfg["cargador"] = self.tipo
        guardar_config(self.app.cfg)
        self.llenar_versiones()

    def _ultima(self):
        return self.app.cfg.setdefault("ultimas", {}).get(self.tipo, {})

    def _mostrar_extras(self):
        self.lbl_mc.configure(text="MODPACK" if self.tipo == "modpack" else "MINECRAFT")
        if self.tipo == "modpack":
            self.zona_loader.pack_forget()
            self.fila_snap.pack_forget()
        elif self.tipo == "vanilla":
            self.zona_loader.pack_forget()
            if not self.fila_snap.winfo_ismapped():
                self.fila_snap.pack(side="right")
        else:
            self.fila_snap.pack_forget()
            self.lbl_loader.configure(text=f"VERSIÓN DE {CARGADORES[self.tipo].upper()}")
            if not self.zona_loader.winfo_ismapped():
                self.zona_loader.pack(fill="x", before=self.fila_mem)

    def llenar_versiones(self):
        self._mostrar_extras()
        m = self.app.manifest
        ultima = self._ultima()
        if self.tipo == "vanilla":
            if m:
                tipos = {"release", "snapshot"} if self.sw_snap.valor else {"release"}
                ids = [v["id"] for v in m["versions"] if v["type"] in tipos]
            else:
                ids = sorted((d.name for d in VERSIONS_DIR.iterdir() if (d / f"{d.name}.json").exists()
                              and not (d / ".instalado").exists() and not d.name.startswith("fabric-")),
                             key=clave_num, reverse=True) if VERSIONS_DIR.exists() else []
            actual = ultima.get("mc") or self.app.cfg.get("ultima_version") or (m["latest"]["release"] if m else "")
        elif self.tipo == "modpack":
            self._packs = listar_modpacks()
            ids = [pk["nombre"] for pk in self._packs]
            actual = ultima.get("mc", "")
        else:
            datos = self.app.cargadores.get(self.tipo)
            if datos is None:
                self.cb.configure(values=[])
                self.cb.set("Cargando…")
                self.cb_loader.configure(values=[])
                self.cb_loader.set("Cargando…")
                self._loaders = []
                self.cv.itemconfig(self.sub, text=f"Cargando versiones de {CARGADORES[self.tipo]}…")
                self.app.cargar_cargador(self.tipo)
                return
            ids = datos["mcs"]
            actual = ultima.get("mc") or (m["latest"]["release"] if m else "")
        self.cb.configure(values=ids)
        if actual in ids:
            self.cb.set(actual)
        elif ids:
            self.cb.current(0)
        else:
            self.cb.set("")
        self._version_cambiada(guardar=False)

    def sin_lista(self, tipo):
        if self.tipo == tipo:
            self.cb.set("Sin conexión")
            self.cb_loader.set("")
            self.cv.itemconfig(self.sub, text=f"No se pudo cargar la lista de {CARGADORES[tipo]}")

    def pack_elegido(self):
        if self.tipo != "modpack":
            return None
        i = self.cb.current()
        return self._packs[i] if 0 <= i < len(self._packs) else None

    def loader_elegido(self):
        i = self.cb_loader.current()
        return self._loaders[i][1] if 0 <= i < len(self._loaders) else None

    def _texto_sub(self):
        mc = self.cb.get()
        if self.tipo == "modpack":
            pk = self.pack_elegido()
            if not pk:
                return "Instala un modpack en Mods → Modpacks"
            return f"{CARGADORES[pk['tipo']]}  ·  Minecraft {pk['mc']}  ·  v{pk.get('version', '')}"
        if not mc:
            return "Sin versiones disponibles"
        if self.tipo == "vanilla":
            tipo = "Snapshot" if self.app.manifest and any(
                x["id"] == mc and x["type"] == "snapshot" for x in self.app.manifest["versions"]) else "Release"
            return f"Minecraft {mc}  ·  {tipo}"
        i = self.cb_loader.current()
        if not 0 <= i < len(self._loaders):
            return f"Minecraft {mc}"
        etiqueta = self._loaders[i][0].replace("★", "").replace("(beta)", "beta").strip()
        return f"{CARGADORES[self.tipo]} {etiqueta}  ·  Minecraft {mc}"

    def _version_cambiada(self, guardar=True):
        mc = self.cb.get()
        if self.tipo not in ("vanilla", "modpack"):
            datos = self.app.cargadores.get(self.tipo) or {"por_mc": {}, "recomendado": {}}
            self._loaders = datos["por_mc"].get(mc, [])
            self.cb_loader.configure(values=[e for e, _ in self._loaders])
            ultima = self._ultima()
            elegido = (ultima.get("loader") if ultima.get("mc") == mc else None) or datos["recomendado"].get(mc)
            if self._loaders:
                self.cb_loader.current(next((k for k, (_, i) in enumerate(self._loaders) if i == elegido), 0))
            else:
                self.cb_loader.set("")
        self.cv.itemconfig(self.sub, text=self._texto_sub())
        if guardar:
            self._guardar_eleccion()

    def _loader_cambiado(self):
        self.cv.itemconfig(self.sub, text=self._texto_sub())
        self._guardar_eleccion()

    def _guardar_eleccion(self):
        mc = self.cb.get()
        if mc in tuple(self.cb["values"]):
            self.app.cfg.setdefault("ultimas", {})[self.tipo] = {"mc": mc, "loader": self.loader_elegido()}
            if self.tipo == "vanilla":
                self.app.cfg["ultima_version"] = mc
            guardar_config(self.app.cfg)

    def _snapshots(self, valor):
        self.app.cfg["mostrar_snapshots"] = valor
        guardar_config(self.app.cfg)
        self.llenar_versiones()

    def _ram(self, valor):
        self.lbl_ram.configure(text=f"{valor / 1024:.1f} GB")

    def alternar_registro(self):
        self.cajon_abierto = not self.cajon_abierto
        self.btn_reg.set_texto("Ocultar registro" if self.cajon_abierto else "Ver registro")
        if self._tw_cajon:
            self._tw_cajon.cancel()
        ini, fin = self.cajon_p, (1.0 if self.cajon_abierto else 0.0)

        def paso(t):
            self.cajon_p = ini + (fin - ini) * t
            self._layout()
        self._tw_cajon = Tween(self, 420, paso, ease=in_out_cubic)

    def agregar_log(self, texto, nivel="info"):
        self.txt.configure(state="normal")
        self.txt.insert("end", texto + "\n", nivel if nivel in ("warn", "error") else ())
        if int(self.txt.index("end-1c").split(".")[0]) > 3000:
            self.txt.delete("1.0", "500.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def limpiar_registro(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    # ---- estados del botón JUGAR
    def preparando(self):
        self.btn_jugar.set_activo(False)
        self.btn_jugar.set_texto("PREPARANDO…")
        self.btn_salir.set_activo(False)
        self.barra.indeterminado = True
        self.pct.configure(text="")

    def progreso(self, hechos, total, etiqueta):
        self.barra.brillo = True
        self.barra.set(hechos / max(total, 1))
        self.pct.configure(text=f"{int(100 * hechos / max(total, 1))}%")
        self.estado.configure(text=f"{etiqueta}  ·  {hechos} de {total}")
        self.btn_jugar.set_texto("DESCARGANDO…")

    def en_juego(self, version):
        self.barra.set(1.0)
        self.barra.brillo = False
        self.pct.configure(text="")
        self.btn_jugar.set_texto("EN EJECUCIÓN")
        self.estado.configure(text=f"{version} está abierto.")

    def listo(self, texto="Listo para jugar."):
        self.barra.set(0.0)
        self.barra.brillo = False
        self.barra.indeterminado = False
        self.pct.configure(text="")
        self.btn_jugar.set_activo(True)
        self.btn_jugar.set_texto("JUGAR")
        self.btn_salir.set_activo(True)
        self.estado.configure(text=texto)

    def _seccion(self, k):
        nuevo, viejo = (self.mods, self.cv) if k == 1 else (self.cv, self.mods)
        d = 1 if k == 1 else -1
        nuevo.place(relx=d, rely=0, relwidth=1, relheight=1)
        tk.Misc.tkraise(nuevo)  # Canvas.lift() es otra cosa en tkinter (sube items del canvas)
        if k == 1:
            self.mods.al_mostrar()

        def paso(t):
            nuevo.place_configure(relx=d * (1 - t))
            viejo.place_configure(relx=-d * 0.3 * t)

        def fin():
            if self.pestanas.i == k:
                viejo.place_forget()
        Tween(self, 480, paso, fin, ease=in_out_cubic)

    def tick(self, t):
        dt = 0.0 if self._t_ant is None else min(0.1, t - self._t_ant)
        self._t_ant = t
        if self.cv.winfo_ismapped():
            self.part.tick(t)
            self.logo.tick(t)
        if self.mods.winfo_ismapped():
            self.mods.tick(t, dt)
        self.barra.tick(t)


# --- Aplicación -------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1100, sw - 60), min(700, sh - 80)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(900, 600)
        self.configure(bg=G["bg"])
        self._iconos()
        migrar_ajustes()
        self.cfg = cargar_config()
        self.cuentas = Cuentas()
        self.cola = queue.Queue()
        self.manifest = None
        self.cuenta = None
        self.jugando = False
        self.toast_w = None
        self.dialogo_ms = None
        self.cargadores, self._cargando = {}, set()
        self.actualizacion, self.relanzar = None, False
        self.imgs = Imagenes()
        self._estilos()
        self.sin_marco, self._marco_listo, self._moviendo = os.name == "nt", False, False
        self.ctrl, self._ctrl_btns, self._arr = None, [], None
        self.contenedor = tk.Frame(self, bg=G["bg"])
        self.contenedor.pack(fill="both", expand=True)
        self.carga = PantallaCarga(self)
        self.login = PantallaLogin(self)
        self.principal = PantallaPrincipal(self)
        self.pantallas = (self.carga, self.login, self.principal)
        self.actual = self.carga
        self.carga.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.report_callback_exception = self._error_tk
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.deiconify()
        barra_oscura(self)
        if self.sin_marco:
            if quitar_barra_nativa(self) and not tiene_barra_nativa(self):
                self._marco_listo = True
                self._activar_arrastre()
                self.after(700, lambda: quitar_capa_transparente(self))  # cuando termina el fundido
            else:
                log_error("La barra de Windows no se pudo quitar; se deja la nativa.")
                self.sin_marco = False
        Tween(self, 500, self._alpha)
        self.carga.al_mostrar()
        self.t0 = self.inicio = time.perf_counter()
        self._fallo_tick = False
        self._login_ms = None
        self.after(16, self._tick)
        self.after(80, self._procesar_cola)
        self.en_hilo(self._arranque, al_error=lambda: self.ir_a(self.login))

    # ---- ventana sin barra de título: se arrastra desde la franja superior + botones discretos
    def _activar_arrastre(self):
        self.bind_all("<ButtonPress-1>", self._ini_arrastre, add="+")
        self.bind_all("<B1-Motion>", self._arrastrar, add="+")
        self.bind_all("<ButtonRelease-1>", self._fin_arrastre, add="+")
        self.bind_all("<Double-Button-1>", self._doble_clic, add="+")
        self.bind("<Map>", lambda e: self._asegurar_sin_barra(), add="+")
        self.bind("<Configure>", lambda e: self._asegurar_sin_barra(), add="+")
        self._crear_controles()

    def _crear_controles(self):
        """Minimizar / maximizar / cerrar: pequeños, sin fondo propio, arriba a la derecha."""
        iconos = "Segoe MDL2 Assets" in tkfont.families(self)
        glifos = {"min": "\ue921" if iconos else "—", "max": "\ue922" if iconos else "☐",
                  "rest": "\ue923" if iconos else "❐", "cerrar": "\ue8bb" if iconos else "✕"}
        fuente = ("Segoe MDL2 Assets", 8) if iconos else (FUENTE, 10)
        self._glifos = glifos
        self.ctrl = tk.Frame(self, bg=G["bg"])
        self.ctrl.place(relx=1.0, x=-4, y=2, anchor="ne")
        self._ctrl_btns = []

        def boton(clave, cmd, hover):
            b = tk.Label(self.ctrl, text=glifos[clave], font=fuente, fg=G["tenue"], bg=self._ctrl_bg(),
                         width=4, height=1, cursor="arrow")
            b.pack(side="left", ipady=3)
            b.bind("<Enter>", lambda e: b.configure(bg=hover, fg=G["blanco"]))
            b.bind("<Leave>", lambda e: b.configure(bg=self._ctrl_bg(), fg=G["tenue"]))
            b.bind("<Button-1>", lambda e: cmd())
            self._ctrl_btns.append(b)
            return b

        boton("min", self.iconify, G["card2"])
        self._btn_max = boton("max", self._alternar_maximizar, G["card2"])
        boton("cerrar", self.destroy, "#c42b1c")
        self.ctrl.lift()

    def _ctrl_bg(self):
        return G["bg2"] if getattr(self, "actual", None) is getattr(self, "principal", None) else G["bg"]

    def _pintar_controles(self):
        if getattr(self, "ctrl", None) is not None:
            self.ctrl.configure(bg=self._ctrl_bg())
            for b in self._ctrl_btns:
                b.configure(bg=self._ctrl_bg())
            self.ctrl.lift()

    def _alternar_maximizar(self):
        zoomed = self.state() == "zoomed"
        self.state("normal" if zoomed else "zoomed")
        self._btn_max.configure(text=self._glifos["max" if zoomed else "rest"])

    def _en_franja(self, e):
        try:
            return e.widget.winfo_toplevel() is self and 0 <= e.y_root - self.winfo_rooty() < 36
        except (tk.TclError, KeyError):
            return False

    def _ini_arrastre(self, e):
        self._arr = None
        if self.state() != "zoomed" and self._en_franja(e) and e.widget not in self._ctrl_btns:
            ox, oy = origen_ventana(self)
            self._arr = (e.x_root - ox, e.y_root - oy)

    def _doble_clic(self, e):
        if self._en_franja(e) and e.widget not in self._ctrl_btns:
            self._alternar_maximizar()

    def _arrastrar(self, e):
        if not getattr(self, "_arr", None):
            return
        self._moviendo = True  # las animaciones se pausan mientras se mueve la ventana
        self._t_mov = time.perf_counter()
        try:
            mover_ventana(self, e.x_root - self._arr[0], e.y_root - self._arr[1])
        except Exception as ex:
            self._arr = None
            self._moviendo = False
            log_error(f"No se pudo mover la ventana: {ex!r}")

    def _fin_arrastre(self, e):
        self._arr, self._moviendo = None, False

    def _asegurar_sin_barra(self):
        if self.sin_marco and self._marco_listo and tiene_barra_nativa(self):
            quitar_barra_nativa(self)

    # ---- base
    def _alpha(self, v):
        try:
            self.attributes("-alpha", v)
        except tk.TclError:
            pass

    def _iconos(self):
        """Ícono en la ventana, la barra de tareas y todos los diálogos."""
        if os.name == "nt":
            try:
                self.iconbitmap(default=str(ruta_recurso("icono.ico")))
            except tk.TclError as e:
                log_error(f"No se pudo aplicar icono.ico: {e!r}")
        try:
            base = Image.open(ruta_recurso("logo.png")).convert("RGBA")
            self._iconos_img = [ImageTk.PhotoImage(base.resize((s, s), Image.LANCZOS)) for s in (256, 64, 32, 16)]
            self.iconphoto(True, *self._iconos_img)
        except (OSError, tk.TclError) as e:
            log_error(f"No se pudo aplicar el ícono de ventana: {e!r}")

    def _estilos(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Nex.TCombobox", fieldbackground=G["card2"], background=G["card2"], foreground=G["texto"],
                    arrowcolor=G["texto"], bordercolor=G["linea"], lightcolor=G["card2"], darkcolor=G["card2"],
                    padding=8, arrowsize=14)
        s.map("Nex.TCombobox", fieldbackground=[("readonly", G["card2"])], foreground=[("readonly", G["texto"])],
              selectbackground=[("readonly", G["card2"])], selectforeground=[("readonly", G["texto"])],
              bordercolor=[("focus", G["tenue"]), ("hover", G["tenue"])],
              background=[("active", G["sec_h"])])
        s.configure("Nex.Vertical.TScrollbar", troughcolor=G["bg2"], background=G["sec"], arrowcolor=G["tenue"],
                    bordercolor=G["bg2"], lightcolor=G["sec"], darkcolor=G["sec"], gripcount=0)
        s.map("Nex.Vertical.TScrollbar", background=[("active", G["sec_h"])])
        self.option_add("*TCombobox*Listbox.background", G["card2"])
        self.option_add("*TCombobox*Listbox.foreground", G["texto"])
        self.option_add("*TCombobox*Listbox.selectBackground", G["prim"])
        self.option_add("*TCombobox*Listbox.selectForeground", G["prim_txt"])
        self.option_add("*TCombobox*Listbox.font", fnt(10))
        self.option_add("*TCombobox*Listbox.relief", "flat")

    def _tick(self):
        inicio = time.perf_counter()
        t = inicio - self.t0
        if self._moviendo and inicio - getattr(self, "_t_mov", 0) < 0.4:
            self.after(30, self._tick)
            return
        self._moviendo = False
        try:
            for p in self.pantallas:
                if p.winfo_ismapped():
                    p.tick(t)
        except Exception:
            if not self._fallo_tick:  # registrar una vez, sin inundar el log
                self._fallo_tick = True
                log_error("Error en la animación:\n" + traceback.format_exc())
        # 120 fps (30 mientras Minecraft está abierto, para no quitarle rendimiento al juego);
        # se descuenta lo que tardó este cuadro
        objetivo = 30 if self.jugando else FPS
        self.after(max(1, int(1000 / objetivo - (time.perf_counter() - inicio) * 1000)), self._tick)

    def _error_tk(self, tipo, valor, tb):
        log_error("".join(traceback.format_exception(tipo, valor, tb)))
        ahora = time.time()
        if ahora - getattr(self, "_ultimo_error_tk", 0) > 5:  # como mucho un aviso cada 5 s
            self._ultimo_error_tk = ahora
            self.toast(f"Ocurrió un error interno. Detalle en {LOG_FILE.name}", "error")

    def en_hilo(self, fn, *args, al_error=None, silencioso=False):
        def envoltura():
            try:
                fn(*args)
            except Cancelado:
                self.cola.put(("llamar", al_error or (lambda: None)))
            except LauncherError as e:
                self.cola.put(("error", str(e), al_error, silencioso))
            except Exception as e:
                log_error(traceback.format_exc())
                self.cola.put(("error", f"Error inesperado: {e}\nDetalle en {LOG_FILE}", al_error, silencioso))
        threading.Thread(target=envoltura, daemon=True).start()

    def log(self, msg, nivel="info"):
        self.cola.put(("log", msg, nivel))

    def progreso(self, hechos, total, etiqueta):
        self.cola.put(("progreso", hechos, total, etiqueta))

    def estado(self, texto):
        self.cola.put(("estado", texto))

    def _procesar_cola(self):
        try:
            for _ in range(400):  # por ciclo, para no congelar la UI con logs masivos
                try:
                    ev = self.cola.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._evento(ev)
                except Exception:
                    log_error(f"Error procesando evento {ev[0]}:\n" + traceback.format_exc())
        finally:
            self.after(50, self._procesar_cola)

    def _evento(self, ev):
        tipo = ev[0]
        if tipo == "log":
            self.principal.agregar_log(ev[1], ev[2])
        elif tipo == "progreso":
            self.principal.progreso(*ev[1:])
        elif tipo == "estado":
            (self.carga.set_estado if self.actual is self.carga else
             (lambda x: self.principal.estado.configure(text=x)))(ev[1])
        elif tipo == "error":
            _, msg, al_error, silencioso = ev
            if al_error:
                al_error()
            if not silencioso:
                self.principal.agregar_log(f"[ERROR] {msg}", "error")
                DialogoAviso(self, "Algo salió mal", msg)
        elif tipo == "llamar":
            ev[1]()

    def ir_a(self, pantalla, direccion=1):
        viejo = self.actual
        if pantalla is viejo:
            return
        self.actual = pantalla
        pantalla.place(relx=direccion, rely=0, relwidth=1, relheight=1)
        pantalla.lift()
        self._pintar_controles()
        pantalla.al_mostrar()

        def paso(t):
            pantalla.place_configure(relx=direccion * (1 - t))
            viejo.place_configure(relx=-direccion * 0.35 * t)

        def fin():
            if self.actual is not viejo:
                viejo.place_forget()
        Tween(self, 560, paso, fin, ease=in_out_cubic)

    def toast(self, texto, tipo="info"):
        if self.toast_w is not None and self.toast_w.winfo_exists():
            self.toast_w.destroy()
        f = tk.Frame(self, bg=G["card2"], highlightthickness=1, highlightbackground=G["linea"])
        simbolo = {"ok": "✓", "error": "!", "info": "i"}.get(tipo, "i")
        ico = tk.Canvas(f, width=26, height=26, bg=G["card2"], highlightthickness=0)
        ico.create_oval(1, 1, 25, 25, fill=G["prim"] if tipo != "error" else G["sec_h"], outline="")
        ico.create_text(13, 13, text=simbolo, fill=G["prim_txt"] if tipo != "error" else G["texto"],
                        font=fnt(10, "bold"))
        ico.pack(side="left", padx=(14, 10), pady=12)
        tk.Label(f, text=texto, bg=G["card2"], fg=G["texto"], font=fnt(10), wraplength=300,
                 justify="left").pack(side="left", padx=(0, 18), pady=12)
        self.toast_w = f
        # Abajo al centro (sobre la barra de estado) o bajo la columna del logo en el login
        rx = 0.29 if self.actual is self.login else 0.5
        f.place(relx=rx, rely=1, anchor="s", y=80)
        f.lift()
        Tween(self, 460, lambda t: f.winfo_exists() and f.place_configure(y=80 - 92 * t), ease=out_back)

        def salir():
            if f.winfo_exists() and self.toast_w is f:
                Tween(self, 300, lambda t: f.winfo_exists() and f.place_configure(y=-12 + 92 * t),
                      fin=lambda: f.winfo_exists() and f.destroy(), ease=in_cubic)
        self.after(6000 if tipo == "error" else 3600, salir)

    # ---- arranque e inicio de sesión
    def _buscar_actualizacion(self):
        if not IS_FROZEN or not REPO_ACTUALIZACIONES:
            return  # solo el .exe compilado se actualiza
        try:
            info = buscar_actualizacion()
        except (LauncherError, HttpError) as e:
            log_error(f"No se pudo revisar actualizaciones: {e}")
            return
        if not info:
            return
        self.log(f"Descargando NexLauncher {info['version']}…")
        nuevo = descargar_actualizacion(info)
        self.actualizacion = nuevo

        def avisar():
            DialogoActualizacion(self, info)
        self.cola.put(("llamar", avisar))

    def instalar_modpack(self, tarjeta):
        if self.jugando or not self.principal.btn_jugar.activo:
            self.toast("Espera a que termine lo que se está instalando (o cierra Minecraft).", "error")
            return
        hit = tarjeta.hit
        tarjeta.btn.set_activo(False)
        tarjeta.btn.set_texto("Instalando…")
        self.principal.preparando()

        def trabajo():
            meta = instalar_modpack(hit["project_id"], hit["title"], self.manifest, self.progreso, self.log,
                                    self.estado, hit.get("icon_url"))

            def listo():
                self.principal.listo(f"{meta['nombre']} está listo para jugar.")
                if tarjeta.winfo_exists():
                    tarjeta.set_instalado(True)
                self.toast(f"{meta['nombre']} instalado. Elígelo en Inicio → Modpacks.", "ok")
                if self.principal.tipo == "modpack":
                    self.principal.llenar_versiones()
            self.cola.put(("llamar", listo))

        def fallo():
            self.principal.listo()
            if tarjeta.winfo_exists():
                tarjeta.set_instalado(False)
        self.en_hilo(trabajo, al_error=fallo)

    def _arranque(self):
        self.estado("Preparando carpetas…")
        limpiar_actualizaciones()
        self.en_hilo(self._buscar_actualizacion, silencioso=True)
        migrar_juego(self.log)
        try:
            self.manifest = http_json(MANIFEST_URL)
            self.log(f"Lista de versiones cargada ({len(self.manifest['versions'])}).")
        except (LauncherError, HttpError) as e:
            self.manifest = None
            self.log(f"[AVISO] Sin acceso a la lista de versiones ({e}). Solo se muestran las instaladas.", "warn")
        self.cola.put(("llamar", self.principal.llenar_versiones))
        cuenta = self.cuentas.buscar(self.cfg.get("cuenta_activa", "")) if self.cfg.get("recordar") else None
        destino, aviso = "login", None
        if cuenta:
            if cuenta["type"] == "microsoft":
                self.estado("Restaurando tu sesión…")
                try:
                    if renovar_sesion(cuenta, self.client_id()):
                        self.cuentas.guardar()
                    destino = "principal"
                except LauncherError as e:
                    aviso = f"No se pudo restaurar tu sesión: {e}"
            else:
                destino = "principal"
        self.estado("¡Listo!")
        time.sleep(max(0.0, 1.2 - (time.perf_counter() - self.inicio)))  # lo justo para la animación

        def fin():
            if destino == "principal":
                self.entrar(cuenta, saludar=False)
            else:
                self.ir_a(self.login)
                if aviso:
                    self.toast(aviso, "error")
        self.cola.put(("llamar", fin))

    def entrar(self, cuenta, saludar=True):
        self.cuenta = cuenta
        self.cfg["cuenta_activa"] = Cuentas.clave(cuenta) if self.cfg.get("recordar", True) else ""
        guardar_config(self.cfg)
        self.principal.set_cuenta(cuenta)
        self.ir_a(self.principal)
        if saludar:
            self.toast(f"Bienvenido, {cuenta['name']}.", "ok")

    def set_recordar(self, valor):
        self.cfg["recordar"] = valor
        if not valor:
            self.cfg["cuenta_activa"] = ""
        elif self.cuenta:
            self.cfg["cuenta_activa"] = Cuentas.clave(self.cuenta)
        guardar_config(self.cfg)

    def login_offline(self, nombre):
        cuenta = self.cuentas.agregar({"type": "offline", "name": nombre, "uuid": uuid_offline(nombre)})
        self.entrar(cuenta)

    def entrar_con(self, cuenta):
        if cuenta["type"] != "microsoft":
            self.cuentas.agregar(cuenta)
            self.entrar(cuenta)
            return
        self.login.ocupado(True, "Restaurando sesión…")

        def trabajo():
            if renovar_sesion(cuenta, self.client_id()):
                self.cuentas.guardar()
            self.cuentas.agregar(cuenta)
            self.cola.put(("llamar", lambda: self.entrar(cuenta)))
        self.en_hilo(trabajo, al_error=lambda: self.login.ocupado(False))

    def quitar_cuenta(self, cuenta):
        self.cuentas.quitar(cuenta)
        if self.cfg.get("cuenta_activa") == Cuentas.clave(cuenta):
            self.cfg["cuenta_activa"] = ""
            guardar_config(self.cfg)
        self.login.refrescar_cuentas()
        self.toast(f"Se quitó la cuenta {cuenta['name']}.", "info")

    def configurar_client_id(self, despues=None):
        def validar(v):
            return None if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", v) else \
                "Debe ser un GUID, por ejemplo 12345678-abcd-1234-abcd-1234567890ab."

        def guardar(v):
            self.cfg["microsoft_client_id"] = v
            guardar_config(self.cfg)
            self.toast("Client ID guardado.", "ok")
            if despues:
                despues()
        DialogoTexto(self, "Client ID de Azure",
                     "Microsoft exige que cada launcher tenga su propia app registrada en Azure "
                     "(gratis, se hace una sola vez). Registra una app para cuentas personales; en "
                     "'Autenticación' agrega 'Aplicaciones móviles y de escritorio' con http://localhost "
                     "y activa 'Allow public client flows'; luego pide acceso a la API de Minecraft en "
                     "aka.ms/mce-reviewappid.\n\nConsejo: si guardas el ID en client_id.txt antes de "
                     "compilar, queda dentro del .exe y nadie más verá esta ventana.",
                     "Application (client) ID", self.cfg.get("microsoft_client_id", ""), guardar, validar,
                     enlace=("Abrir el portal de Azure",
                             "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"))

    def login_microsoft(self):
        cid = self.client_id()
        if not cid:
            self.configurar_client_id(despues=self.login_microsoft)
            return
        if self._login_ms is not None:  # el botón funciona como "Cancelar" mientras espera
            self._login_ms.set()
            return
        cancelado = threading.Event()
        self._login_ms = cancelado
        self.login.esperando_navegador()
        self.en_hilo(self._flujo_navegador, cid, cancelado, al_error=self._fin_login_ms)

    def _fin_login_ms(self):
        self._login_ms = None
        self._cerrar_dialogo_ms()
        self.login.ocupado(False)

    def _flujo_navegador(self, cid, cancelado):
        """Abre directo la página de Microsoft; si no se puede, cae al inicio con código."""
        try:
            nav = LoginNavegador(cid)
        except OSError as e:
            log_error(f"No se pudo abrir el puerto local para el login: {e!r}")
            return self._flujo_ms(MicrosoftAuth(cid), cancelado)
        if not webbrowser.open(nav.url):
            nav.cerrar()
            return self._flujo_ms(MicrosoftAuth(cid), cancelado)
        self.cola.put(("llamar", lambda: self.toast("Se abrió tu navegador. Inicia sesión con Microsoft "
                                                    "y vuelve aquí.", "info")))
        tok = nav.esperar(cancelado)
        self.cola.put(("llamar", self._traer_al_frente))
        self._completar_ms(MicrosoftAuth(cid), tok)

    def _traer_al_frente(self):
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except tk.TclError:
            pass

    def _flujo_ms(self, auth, cancelado):
        """Respaldo: inicio con código (device code)."""
        try:
            dc = auth.iniciar_device_code()
        except HttpError as e:
            raise LauncherError(f"Microsoft rechazó el Client ID: {e.json.get('error_description', e)}") from e

        def mostrar():
            self.dialogo_ms = DialogoMicrosoft(self, dc["verification_uri"], dc["user_code"], cancelado.set)
        self.cola.put(("llamar", mostrar))
        tok = auth.esperar_token(dc["device_code"], int(dc.get("interval", 5)), int(dc.get("expires_in", 900)),
                                 cancelado)
        self.cola.put(("llamar", self._cerrar_dialogo_ms))
        self._completar_ms(auth, tok)

    def _completar_ms(self, auth, tok):
        self.cola.put(("llamar", lambda: self.login.ocupado(True, "Verificando tu cuenta…")))
        try:
            datos = auth.login_minecraft(tok["access_token"])
        except (HttpError, KeyError) as e:
            raise LauncherError(f"Microsoft no respondió como se esperaba ({e}).") from e
        cuenta = self.cuentas.agregar({"type": "microsoft", "refresh_token": tok.get("refresh_token", ""), **datos})

        def listo():
            self._login_ms = None
            self.entrar(cuenta)
        self.cola.put(("llamar", listo))

    def _cerrar_dialogo_ms(self):
        if self.dialogo_ms is not None and self.dialogo_ms.winfo_exists():
            self.dialogo_ms.cerrar()
        self.dialogo_ms = None

    def cerrar_sesion(self):
        if self.jugando:
            self.toast("Cierra Minecraft antes de cambiar de cuenta.", "error")
            return
        self.cfg["cuenta_activa"] = ""
        guardar_config(self.cfg)
        self.cuenta = None
        self.ir_a(self.login, direccion=-1)

    # ---- jugar
    def client_id(self):
        return (self.cfg.get("microsoft_client_id") or "").strip() or client_id_integrado()

    def cargar_cargador(self, tipo):
        if tipo in self._cargando:
            return
        self._cargando.add(tipo)

        def trabajo():
            try:
                datos = listar_cargador(tipo)
            except HttpError as e:
                raise LauncherError(f"El servidor de {CARGADORES[tipo]} respondió {e.code}.") from e
            finally:
                self._cargando.discard(tipo)

            def listo():
                self.cargadores[tipo] = datos
                if self.principal.tipo == tipo:
                    self.principal.llenar_versiones()
            self.cola.put(("llamar", listo))

        def fallo():
            self.principal.sin_lista(tipo)
            self.toast(f"No se pudo cargar la lista de {CARGADORES[tipo]}. Revisa tu conexión y vuelve a "
                       "seleccionarlo.", "error")
        self.en_hilo(trabajo, al_error=fallo, silencioso=True)

    def abrir_carpeta(self):
        p = self.principal
        pk = p.pack_elegido()
        carpeta = Path(pk["dir"]) if pk else GAME_DIR
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(carpeta)
            else:
                subprocess.Popen(["xdg-open", str(carpeta)])
        except OSError as e:
            self.toast(f"No se pudo abrir la carpeta: {e}", "error")

    def jugar(self):
        if not self.cuenta:
            return
        p = self.principal
        mc, tipo = p.cb.get(), p.tipo
        if tipo == "modpack":
            pk = p.pack_elegido()
            if not pk:
                self.toast("Instala un modpack en Mods → Modpacks, o elige otra opción.", "error")
                return
            p._guardar_eleccion()
            p.preparando()
            self.cfg["ram_mb"] = p.ram.valor
            self.en_hilo(self._lanzar, self.cuenta, "modpack", pk["mc"], pk, p.ram.valor, al_error=p.listo)
            return
        if not mc or mc not in tuple(p.cb["values"]):
            self.toast("Elige una versión primero.", "error")
            return
        loader = p.loader_elegido() if tipo != "vanilla" else None
        if tipo != "vanilla" and not loader:
            self.toast(f"Elige la versión de {CARGADORES[tipo]}.", "error")
            return
        ram = p.ram.valor
        self.cfg["ram_mb"] = ram
        p._guardar_eleccion()
        p.preparando()
        self.en_hilo(self._lanzar, self.cuenta, tipo, mc, loader, ram, al_error=p.listo)

    def _lanzar(self, cuenta, tipo, mc, loader, ram):
        pack = loader if tipo == "modpack" else None
        if pack:
            nombre = pack["nombre"]
            tipo, loader = pack["tipo"], pack["loader"]
        elif tipo == "vanilla":
            nombre = f"Minecraft {mc}"
        else:
            nombre = f"{CARGADORES[tipo]} {loader.split('-', 1)[1] if tipo == 'forge' else loader} · {mc}"
        self.log(f"=== {nombre} · {cuenta['name']} ===")
        if renovar_sesion(cuenta, self.client_id()):
            self.cuentas.guardar()
        vid = instalar_cargador(tipo, mc, loader, self.manifest, self.progreso, self.log, self.estado)
        vjson, java, cp, natives_dir, game_assets, log_cfg = instalar_version(
            vid, self.manifest, self.progreso, self.log, self.estado)
        game_dir = Path(pack["dir"]) if pack else carpeta_juego(tipo, mc)
        game_dir.mkdir(parents=True, exist_ok=True)
        if tipo != "vanilla" and not pack:
            inst = Instancia(tipo, mc)
            otros = sorted({CARGADORES.get(e.get("cargador"), "?") for pid, e in inst.reg.items()
                            if e.get("tipo") == "mod" and e.get("cargador") not in (tipo, None) and inst.instalado(pid)})
            if otros:
                aviso = (f"En .minecraft\\mods hay mods de {', '.join(otros)}; {CARGADORES[tipo]} puede fallar. "
                         "Quítalos en Mods → Instalados.")
                self.log(f"[AVISO] {aviso}", "warn")
                self.cola.put(("llamar", lambda: self.toast(aviso, "error")))
        cmd = construir_comando(vjson, java, cuenta, ram, natives_dir, cp, game_assets, log_cfg,
                                self.client_id(), game_dir)
        # Diagnóstico: si el JSON trae una clase principal rara, se ve aquí en vez de un error críptico de Java.
        clase = vjson.get("mainClass", "?")
        self.log(f"Clase principal: {clase} · classpath: {len(cp)} jars")
        try:
            ruta_clase = clase.replace(".", "/") + ".class"
            hay = any(Path(j).suffix == ".jar" and Path(j).exists() and ruta_clase in zipfile.ZipFile(j).namelist()
                      for j in cp[-1:])
            if not hay:
                self.log(f"[AVISO] {clase} no está en el .jar del cliente ({Path(cp[-1]).name}); "
                         "el archivo de la versión puede estar dañado o ser de otro launcher. "
                         f"Borra la carpeta {VERSIONS_DIR / vjson['id']} y vuelve a darle a JUGAR.", "warn")
        except (OSError, zipfile.BadZipFile) as e:
            self.log(f"[AVISO] No se pudo revisar el .jar del cliente: {e}", "warn")
        self.estado("Iniciando Minecraft…")
        self.log(f"Iniciando el juego en {game_dir}…")
        salida = LAUNCHER_DIR / "logs" / "ultimo_juego.log"
        salida.parent.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # La salida va a un archivo: si cierras el launcher, el juego sigue sin bloquearse.
        try:
            with open(salida, "w", encoding="utf-8") as fsal:
                proc = subprocess.Popen(cmd, cwd=game_dir, stdout=fsal, stderr=subprocess.STDOUT,
                                        creationflags=flags)
        except OSError as e:
            raise LauncherError(f"No se pudo ejecutar Java ({java}): {e}") from e

        def iniciado():
            self.jugando = True
            self.principal.en_juego(nombre)
        self.cola.put(("llamar", iniciado))
        self._seguir_log(proc, salida, cuenta.get("mc_token"))
        codigo = proc.returncode
        self.log(f"=== El juego se cerró (código {codigo}) ===")

        def cerrado():
            self.jugando = False
            if codigo == 0:
                self.principal.listo("El juego se cerró. ¿Otra partida?")
            else:
                self.principal.listo(f"El juego terminó con código {codigo}.")
                self.toast("Minecraft se cerró con un error. Revisa el registro.", "error")
        self.cola.put(("llamar", cerrado))

    def _seguir_log(self, proc, ruta, token):
        parser, pendiente = ParserLog4j(), ""
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            while True:
                trozo = f.readline()
                if trozo:
                    pendiente += trozo
                    if not pendiente.endswith("\n"):
                        continue
                    linea, pendiente = pendiente.rstrip("\r\n"), ""
                    if token:
                        linea = linea.replace(token, "<token>")
                    for texto, nivel in parser.feed(linea):
                        self.log(texto, nivel)
                elif proc.poll() is not None:
                    resto = (pendiente + f.read()).splitlines()
                    for linea in resto:
                        for texto, nivel in parser.feed(linea.replace(token, "<token>") if token else linea):
                            self.log(texto, nivel)
                    break
                else:
                    time.sleep(0.2)


def main():
    # Windows redondea los temporizadores a ~15.6 ms; con 1 ms las animaciones llegan a 60 fps.
    precision = False
    if os.name == "nt":
        try:
            precision = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        except Exception as e:
            log_error(f"No se pudo ajustar la precisión del temporizador: {e!r}")
    app = None
    try:
        app = App()
        app.mainloop()
    except Exception as e:
        log_error(traceback.format_exc())
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, f"El launcher no pudo iniciar: {e}\nDetalle en {LOG_FILE}")
            r.destroy()
        except Exception:
            pass
    finally:
        if precision:
            ctypes.windll.winmm.timeEndPeriod(1)
        if app is not None and getattr(app, "actualizacion", None):
            try:
                aplicar_actualizacion(app.actualizacion, relanzar=getattr(app, "relanzar", False))
            except OSError as e:
                log_error(f"No se pudo aplicar la actualización: {e!r}")
        # Termina ya: sin esperar descargas/íconos en segundo plano (Minecraft sigue abierto si lo estaba)
        os._exit(0)


if __name__ == "__main__":
    main()
