#!/usr/bin/env python3
"""One-shot installer + integration wiring for the Arr stack. Safe to re-run (idempotent).

    cp .env.example .env   # fill in paths, VPN, (optional) indexers / usenet
    python3 setup.py       # creates folders, starts the stack, wires every app together
    python3 setup.py sonarr radarr ...   # optional: only start these services

Stdlib only. Talks to the apps through their published localhost ports.
"""
import base64, hashlib, http.cookiejar, json, os, re, secrets, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
COMPOSE = ["docker", "compose", "--project-directory", str(HERE)]
SERVICES = sys.argv[1:]


# ---------------------------------------------------------------- env handling
def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*?)(\s+#.*)?$", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"')
    return env


def set_env(key, value):
    text = ENV_FILE.read_text()
    text = re.sub(rf"^({key}=)\s*(\s+#.*)?$", lambda m: f"{m.group(1)}{value}{m.group(2) or ''}",
                  text, count=1, flags=re.M)
    ENV_FILE.write_text(text)


def prepare_env():
    if not ENV_FILE.exists():
        ENV_FILE.write_text((HERE / ".env.example").read_text())
        sys.exit(f"Created {ENV_FILE}. Fill in paths + VPN settings, then re-run.")
    env = load_env()
    gen = {"SONARR_API_KEY": lambda: secrets.token_hex(16), "RADARR_API_KEY": lambda: secrets.token_hex(16),
           "PROWLARR_API_KEY": lambda: secrets.token_hex(16), "BAZARR_API_KEY": lambda: secrets.token_hex(16),
           "QBIT_PASSWORD": lambda: secrets.token_urlsafe(12), "ADMIN_PASSWORD": lambda: secrets.token_urlsafe(12)}
    for k, fn in gen.items():
        if not env.get(k):
            set_env(k, fn())
    env = load_env()
    need = ["DATA_ROOT", "CONFIG_ROOT", "HOST_IP"]
    if not SERVICES or "gluetun" in SERVICES:
        need += ["VPN_SERVICE_PROVIDER"] + (["WIREGUARD_PRIVATE_KEY"] if env.get("VPN_TYPE") == "wireguard"
                                            else ["VPN_USERNAME", "VPN_PASSWORD"])
    missing = [k for k in need if not env.get(k)]
    if missing:
        sys.exit(f"Fill these in {ENV_FILE} first: {', '.join(missing)}")
    return env


# ---------------------------------------------------------------- http helper
class Api:
    def __init__(self, base, headers=None):
        self.base, self.headers = base.rstrip("/"), headers or {}
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, headers=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        h = {"Content-Type": "application/json", **self.headers, **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=60) as r:
                out = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {self.base}{path} -> {e.code}: {e.read()[:300]!r}") from None
        if raw:
            return out.decode()
        return json.loads(out) if out.strip() else None

    get = lambda self, p, **k: self.call("GET", p, **k)
    post = lambda self, p, b=None, **k: self.call("POST", p, b if b is not None else {}, **k)


def wait_for(name, fn, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        try:
            fn()
            return True
        except Exception:
            time.sleep(3)
    print(f"  ! {name} not reachable after {timeout}s")
    return False


def step(msg):
    print(f"\n==> {msg}")


# ---------------------------------------------------------------- filesystem + pre-seeded configs
def prepare_files(env):
    data, conf = Path(env["DATA_ROOT"]), Path(env["CONFIG_ROOT"])
    for p in ["torrents/movies", "torrents/tv", "usenet/incomplete", "usenet/complete/movies",
              "usenet/complete/tv", "media/movies", "media/tv"]:
        (data / p).mkdir(parents=True, exist_ok=True)
    for app in ["gluetun", "qbittorrent", "sabnzbd", "prowlarr", "sonarr", "radarr", "bazarr",
                "recyclarr", "jellyfin", "seerr"]:
        (conf / app).mkdir(parents=True, exist_ok=True)

    # qBittorrent: known password + TRaSH save paths + accept Host header "gluetun"
    qb = conf / "qbittorrent/qBittorrent/qBittorrent.conf"
    if not qb.exists():
        qb.parent.mkdir(parents=True, exist_ok=True)
        salt = os.urandom(16)
        h = hashlib.pbkdf2_hmac("sha512", env["QBIT_PASSWORD"].encode(), salt, 100000, 64)
        pw = f'@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(h).decode()})'
        qb.write_text(
            "[LegalNotice]\nAccepted=true\n\n"
            "[BitTorrent]\nSession\\DefaultSavePath=/data/torrents\nSession\\TempPathEnabled=false\n\n"
            f'[Preferences]\nWebUI\\Username={env["QBIT_USERNAME"]}\nWebUI\\Password_PBKDF2="{pw}"\n'
            "WebUI\\HostHeaderValidation=false\nWebUI\\CSRFProtection=false\nDownloads\\SavePath=/data/torrents\n")

    # Bazarr: pre-link to Sonarr/Radarr
    bz = conf / "bazarr/config/config.yaml"
    if not bz.exists():
        bz.parent.mkdir(parents=True, exist_ok=True)
        bz.write_text(
            "analytics:\n  enabled: false\n"
            f"auth:\n  apikey: {env['BAZARR_API_KEY']}\n"
            "general:\n  use_sonarr: true\n  use_radarr: true\n"
            f"sonarr:\n  ip: sonarr\n  port: 8989\n  base_url: /\n  ssl: false\n  apikey: {env['SONARR_API_KEY']}\n"
            f"radarr:\n  ip: radarr\n  port: 7878\n  base_url: /\n  ssl: false\n  apikey: {env['RADARR_API_KEY']}\n")


# ---------------------------------------------------------------- SABnzbd
def prepare_sab(env):
    ini = Path(env["CONFIG_ROOT"]) / "sabnzbd/sabnzbd.ini"
    if not wait_for("sabnzbd.ini", lambda: ini.read_text().index("api_key"), 120):
        return None
    text = ini.read_text()
    if "gluetun" not in re.search(r"^host_whitelist.*$", text, re.M).group(0):
        # SAB rejects unknown Host headers; the arr apps call it as "gluetun"
        subprocess.run(COMPOSE + ["stop", "sabnzbd"], check=True, capture_output=True)
        text = re.sub(r"^host_whitelist.*$", "host_whitelist = gluetun, sabnzbd", text, flags=re.M)
        ini.write_text(text)
        subprocess.run(COMPOSE + ["start", "sabnzbd"], check=True, capture_output=True)
    key = re.search(r"^api_key\s*=\s*(\S+)", text, re.M).group(1)
    sab = Api(f"http://localhost:{env['SAB_WEBUI_PORT']}")
    q = lambda **kw: sab.get("/api?" + urllib.parse.urlencode({"apikey": key, "output": "json", **kw}))
    if wait_for("sabnzbd api", lambda: q(mode="version")):
        q(mode="set_config", section="misc", keyword="download_dir", value="/data/usenet/incomplete")
        q(mode="set_config", section="misc", keyword="complete_dir", value="/data/usenet/complete")
        for cat in ("movies", "tv"):
            q(mode="set_config", section="categories", name=cat, dir=cat)
        if env.get("USENET_HOST"):
            q(mode="set_config", section="servers", name="usenet", host=env["USENET_HOST"],
              port=env.get("USENET_PORT", "563"), ssl="1", username=env.get("USENET_USERNAME", ""),
              password=env.get("USENET_PASSWORD", ""), connections=env.get("USENET_CONNECTIONS", "20"),
              enable="1")
            print("  usenet server added")
    return key


# ---------------------------------------------------------------- *arr wiring
def add_from_schema(api, endpoint, implementation, name, values, extra=None):
    """Create an item (download client / app / proxy) from the app's own schema."""
    if any(i["name"] == name for i in api.get(endpoint)):
        print(f"  = {name} exists")
        return
    schema = next(s for s in api.get(f"{endpoint}/schema") if s["implementation"] == implementation)
    for f in schema["fields"]:
        if f["name"] in values:
            f["value"] = values[f["name"]]
    schema.update({"name": name, "enable": True, **(extra or {})})
    for attempt in range(3):  # target (e.g. qBittorrent behind the VPN) may still be starting
        try:
            api.post(f"{endpoint}?forceSave=true", schema)
            return print(f"  + {name}")
        except RuntimeError as e:
            err = e
            time.sleep(10)
    print(f"  ! {name} not added (re-run setup.py once its target is up): {err}")


def arr_api(port, key, ver="v3"):
    return Api(f"http://localhost:{port}/api/{ver}", {"X-Api-Key": key})


def wire_arr(env, sab_key):
    sonarr = arr_api(8989, env["SONARR_API_KEY"])
    radarr = arr_api(7878, env["RADARR_API_KEY"])
    prowlarr = arr_api(9696, env["PROWLARR_API_KEY"], "v1")
    apps = [("Sonarr", sonarr, "/data/media/tv", "tv", "http://sonarr:8989", env["SONARR_API_KEY"]),
            ("Radarr", radarr, "/data/media/movies", "movies", "http://radarr:7878", env["RADARR_API_KEY"])]
    qb = {"host": "gluetun", "port": int(env["QBIT_WEBUI_PORT"]), "username": env["QBIT_USERNAME"],
          "password": env["QBIT_PASSWORD"]}
    sab = {"host": "gluetun", "port": 8080, "apiKey": sab_key or ""}

    for label, api, root, cat, _, _ in apps:
        step(f"{label}: root folder + download clients")
        if not wait_for(label, lambda: api.get("/system/status")):
            continue
        if not any(r["path"] == root for r in api.get("/rootfolder")):
            api.post("/rootfolder", {"path": root})
            print(f"  + root folder {root}")
        cats = {"tvCategory": cat, "movieCategory": cat}
        add_from_schema(api, "/downloadclient", "QBittorrent", "qBittorrent", {**qb, **cats})
        if sab_key:
            add_from_schema(api, "/downloadclient", "Sabnzbd", "SABnzbd", {**sab, **cats})

    step("Prowlarr: FlareSolverr proxy, app sync, indexers")
    if not wait_for("Prowlarr", lambda: prowlarr.get("/system/status")):
        return
    tag = next((t for t in prowlarr.get("/tag") if t["label"] == "flaresolverr"), None) \
        or prowlarr.post("/tag", {"label": "flaresolverr"})
    add_from_schema(prowlarr, "/indexerproxy", "FlareSolverr", "FlareSolverr",
                    {"host": "http://flaresolverr:8191/", "requestTimeout": 60}, {"tags": [tag["id"]]})
    for label, _, _, _, url, key in apps:
        add_from_schema(prowlarr, "/applications", label, label,
                        {"prowlarrUrl": "http://prowlarr:9696", "baseUrl": url, "apiKey": key},
                        {"syncLevel": "fullSync"})
    wanted = [i.strip().lower() for i in env.get("INDEXERS", "").split(",") if i.strip()]
    if wanted:
        have = {i["name"].lower() for i in prowlarr.get("/indexer")}
        schemas = prowlarr.get("/indexer/schema")
        for w in wanted:
            s = next((x for x in schemas if w in (x["name"].lower(), x.get("definitionName", "").lower())), None)
            if not s:
                print(f"  ! indexer '{w}' not found in Prowlarr's catalogue")
            elif s["name"].lower() in have:
                print(f"  = {s['name']} exists")
            else:
                s.update({"enable": True, "appProfileId": 1, "tags": [tag["id"]]})
                try:
                    prowlarr.post("/indexer?forceSave=true", s)
                    print(f"  + indexer {s['name']}")
                except RuntimeError as e:
                    print(f"  ! {s['name']}: {e}")
    return sonarr, radarr


def sync_recyclarr():
    step("Recyclarr: TRaSH quality profiles + custom formats")
    r = subprocess.run(COMPOSE + ["exec", "-T", "recyclarr", "recyclarr", "sync"], capture_output=True, text=True)
    print((r.stdout + r.stderr).strip()[-800:] or "  done")


# ---------------------------------------------------------------- Jellyfin + Seerr
def setup_jellyfin(env):
    step("Jellyfin: admin user + libraries")
    jf = Api("http://localhost:8096", {"Authorization":
             'MediaBrowser Client="arr-setup", Device="setup", DeviceId="arr-setup", Version="1.0"'})
    if not wait_for("Jellyfin", lambda: jf.get("/System/Info/Public")):
        return False
    if not jf.get("/System/Info/Public").get("StartupWizardCompleted"):
        jf.post("/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US",
                                           "PreferredMetadataLanguage": "en"})
        jf.get("/Startup/User")
        jf.post("/Startup/User", {"Name": env["ADMIN_USER"], "Password": env["ADMIN_PASSWORD"]})
        jf.post("/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        jf.post("/Startup/Complete")
        print("  + admin user created")
    try:
        tok = jf.post("/Users/AuthenticateByName", {"Username": env["ADMIN_USER"], "Pw": env["ADMIN_PASSWORD"]})
    except RuntimeError as e:
        print(f"  ! can't log in as {env['ADMIN_USER']} (existing install with another password?): {e}")
        return False
    auth = {"Authorization": jf.headers["Authorization"] + f', Token="{tok["AccessToken"]}"'}
    have = {f["Name"] for f in jf.get("/Library/VirtualFolders", headers=auth)}
    for name, ctype, path in [("Movies", "movies", "/data/media/movies"), ("TV Shows", "tvshows", "/data/media/tv")]:
        if name not in have:
            q = urllib.parse.urlencode({"name": name, "collectionType": ctype, "refreshLibrary": "true"})
            jf.post(f"/Library/VirtualFolders?{q}", {"LibraryOptions": {
                "PathInfos": [{"Path": path}], "EnableRealtimeMonitor": True}}, headers=auth)
            print(f"  + library {name}")
    return True


def profile_id(api, prefer):
    profiles = api.get("/qualityprofile")
    p = next((x for x in profiles if x["name"] == prefer), profiles[0])
    return p["id"], p["name"]


def setup_seerr(env, sonarr, radarr):
    step("Seerr: Jellyfin login + Sonarr/Radarr")
    s = Api("http://localhost:5055")
    if not wait_for("Seerr", lambda: s.get("/api/v1/settings/public")):
        return
    if s.get("/api/v1/settings/public").get("initialized"):
        print("  = already initialised")
        return
    login = {"username": env["ADMIN_USER"], "password": env["ADMIN_PASSWORD"], "email": env["ADMIN_EMAIL"]}
    try:
        s.post("/api/v1/auth/jellyfin", {**login, "hostname": "jellyfin", "port": 8096, "useSsl": False,
                                         "urlBase": "", "serverType": 2})
    except RuntimeError:  # half-finished earlier run: server already stored, just sign in
        s.post("/api/v1/auth/jellyfin", login)
    libs = s.post("/api/v1/settings/jellyfin/library/sync")
    for lib in libs:
        s.call("PUT", f"/api/v1/settings/jellyfin/library/{lib['id']}", {"enabled": True})
    print(f"  + signed in with Jellyfin, {len(libs)} libraries enabled")
    host = env["HOST_IP"]
    if radarr:
        pid, pname = profile_id(radarr, "HD Bluray + WEB")
        s.post("/api/v1/settings/radarr", {
            "name": "Radarr", "hostname": "radarr", "port": 7878, "apiKey": env["RADARR_API_KEY"], "useSsl": False,
            "baseUrl": "", "activeProfileId": pid, "activeProfileName": pname,
            "activeDirectory": "/data/media/movies", "is4k": False, "minimumAvailability": "released",
            "isDefault": True, "externalUrl": f"http://{host}:7878", "syncEnabled": False, "preventSearch": False})
        print(f"  + Radarr ({pname})")
    if sonarr:
        pid, pname = profile_id(sonarr, "WEB-1080p")
        s.post("/api/v1/settings/sonarr", {
            "name": "Sonarr", "hostname": "sonarr", "port": 8989, "apiKey": env["SONARR_API_KEY"], "useSsl": False,
            "baseUrl": "", "activeProfileId": pid, "activeProfileName": pname,
            "activeDirectory": "/data/media/tv", "is4k": False, "enableSeasonFolders": True,
            "isDefault": True, "externalUrl": f"http://{host}:8989", "syncEnabled": False, "preventSearch": False})
        print(f"  + Sonarr ({pname})")
    s.post("/api/v1/settings/initialize")
    print("  + initialised")


# ---------------------------------------------------------------- main
def main():
    env = prepare_env()
    step("Folders + pre-seeded configs")
    prepare_files(env)
    step("Starting containers (first run pulls images)")
    subprocess.run(COMPOSE + ["up", "-d", *SERVICES], check=True)

    sab_key = prepare_sab(env) if not SERVICES or "sabnzbd" in SERVICES else None
    sonarr = radarr = None
    r = wire_arr(env, sab_key)
    if r:
        sonarr, radarr = r
        sync_recyclarr()
    jf_ok = setup_jellyfin(env)
    if jf_ok:
        setup_seerr(env, sonarr, radarr)

    h = env["HOST_IP"]
    print(f"""
==> Done. Open the dashboard: http://{h}:3000
    Jellyfin  http://{h}:8096   Seerr http://{h}:5055   Sonarr :8989  Radarr :7878  Prowlarr :9696
    Bazarr    http://{h}:6767   qBittorrent :{env['QBIT_WEBUI_PORT']}  SABnzbd :{env['SAB_WEBUI_PORT']}
    Logins (Jellyfin/Seerr): {env['ADMIN_USER']} / ADMIN_PASSWORD in {ENV_FILE}
    qBittorrent: {env['QBIT_USERNAME']} / QBIT_PASSWORD in {ENV_FILE}
    Still manual: add indexers/usenet provider if not set in .env, Bazarr languages + subtitle providers.""")


if __name__ == "__main__":
    main()
