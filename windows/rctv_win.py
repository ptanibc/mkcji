# RC TV — Windows desktop app (PC par wahi kaam jo TV app ka andar-wala server karta hai).
# Telegram storage group ki videos ki GUI list + player me play + door-DC wali files ka
# auto self-heal (bot KHUD file ka data stream karke usko ghar wale DC pe re-upload karta hai,
# verify karta hai, purani copy delete — bilkul TV app jaisa, bina kuch puche).
#
# Local HTTP API TV app wala hi hai: /api/status /api/list /api/scan /t/{id} /f/{id} /tv.m3u
# Is PC ko TV app ke Settings > Server URL me dalo to TV bhi isi PC se chalega.
#
# Data yahan save hota hai: %APPDATA%\RCTV\  (tvbot.session, index.json, heal.json, settings.json)
# RCTV_HOME env se jagah badal sakte ho (portable use ke liye).

import asyncio, base64, json, os, random, re, shutil, socket, subprocess, sys, threading, time, traceback, webbrowser
from pathlib import Path

VERSION = "1.4"

# ---------------- config ----------------

DEF = {"API_ID": 14116809, "API_HASH": "", "BOT_TOKEN": "", "SESSION": "",
       "GROUP_ID": -1003904890252, "ADMIN_ID": 1345802943, "PORT": 8090}


def rctv_home():
    h = os.environ.get("RCTV_HOME")
    if h:
        return Path(h)
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "RCTV"
    return Path.home() / ".rctv"


def app_dir():
    # PyInstaller exe ke bagal me .env dhoondo (portable), warna script ka folder
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _pick_home():
    # Pehla writable folder chuno — APPDATA ke permission/roaming issues par
    # exe ke bagal wala folder, phir temp. (v1.0 ka crash yahi tha: RCTV folder
    # banta hi nahi tha, isliye sqlite "unable to open database file" bol raha tha.)
    import tempfile
    for h in (rctv_home(), app_dir() / "RCTV-data", Path(tempfile.gettempdir()) / "RCTV"):
        try:
            h.mkdir(parents=True, exist_ok=True)
            t = h / ".w"
            t.write_text("ok")
            t.unlink()
            return h
        except Exception:
            continue
    raise RuntimeError("koi writable data folder nahi mila")


HOME = _pick_home()


def load_config():
    cfg = dict(DEF)
    # 1) build ke waqt inject hua config (GitHub Actions secrets se banta hai)
    try:
        import rctv_config as rc
        for k in cfg:
            if hasattr(rc, k):
                cfg[k] = getattr(rc, k)
    except Exception:
        pass
    # 2) .env (exe ke bagal me, phir data folder me)
    for f in (app_dir() / ".env", HOME / ".env"):
        try:
            if f.exists():
                for line in f.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip().upper()
                        if k in cfg and str(v).strip():
                            cfg[k] = str(v).strip()
        except Exception:
            pass
    # 3) seedha env vars
    for k in cfg:
        if os.environ.get(k, "").strip():
            cfg[k] = os.environ[k].strip()
    cfg["API_ID"] = int(cfg["API_ID"])
    cfg["GROUP_ID"] = int(cfg["GROUP_ID"])
    cfg["ADMIN_ID"] = int(cfg.get("ADMIN_ID") or 0)
    cfg["PORT"] = int(cfg["PORT"])
    return cfg


CFG = load_config()

# ---------------- log (GUI + console dono) ----------------

_LOG_UI = None


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _LOG_UI:
        try:
            _LOG_UI(line)
        except Exception:
            pass


# ---------------- settings ----------------

SETTINGS = {"auto_heal": True}
try:
    SETTINGS.update(json.loads((HOME / "settings.json").read_text()))
except Exception:
    pass


def save_settings():
    try:
        (HOME / "settings.json").write_text(json.dumps(SETTINGS))
    except Exception:
        pass


# ---------------- core (server/server.py jaisa) ----------------

try:
    from telethon import TelegramClient, events, functions, utils, types
    from telethon.errors import FloodWaitError
except Exception as e:  # source se chala rahe ho aur pip install nahi kiya
    print("pip install -r requirements.txt zaroori hai:", e)
    raise

CHUNK = 512 * 1024
PARALLEL = 4
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".webm", ".ts", ".mov", ".m4v", ".flv")
AUDIO_EXT = (".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac")
SUB_EXT = (".srt", ".vtt", ".ass", ".ssa")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
UPD = re.compile(r"^(?:TGTV|RGTV)-UPDATE\s+(\d+)\s*(\S*)")
# Telegram bots 2GB se badi file download nahi kar sakte (premium session chahiye)
BOT_DL_LIMIT = 2000 * 1024 * 1024
# Ye DC (America/Europe) India se door hain — inki video aksar atakti hai.
# Scan par inki video bot khud-fix (heal) kar deta hai (auto-heal on ho to).
FAR_DCS = {1, 2, 3}

state = {"ready": False, "error": "", "scanning": False, "scanned": 0, "version": 0,
         "title": "", "update": None, "speed": 0, "online": True, "channelError": "",
         "lan": "", "dc": 0}
BOOT_TS = 0.0
_speed = {"t": 0.0, "n": 0}
index = {}          # msg_id -> item dict (/api/list ka seedha jawab)
client: TelegramClient = None
group = None


def save_index():
    tmp = HOME / "index.json.tmp"
    tmp.write_text(json.dumps({"_v": 2, "items": index, "update": state["update"],
                               "title": state["title"]}, ensure_ascii=False))
    os.replace(tmp, HOME / "index.json")


def load_index():
    try:
        data = json.loads((HOME / "index.json").read_text())
        if data.get("_v") == 2:
            index.update({int(k): v for k, v in data["items"].items()})
            state["update"] = data.get("update")
            state["title"] = data.get("title", "")
            state["version"] += 1
    except Exception:
        pass


def kind(m):
    if not m or not m.media:
        return None
    if m.photo:
        return "photo"
    if not m.document or not m.file:
        return None
    mime = (m.file.mime_type or "").lower()
    name = (m.file.name or "").lower()
    ext = (m.file.ext or "").lower()
    if name.endswith(SUB_EXT) or mime in ("application/x-subrip", "text/vtt"):
        return "sub"
    if mime.startswith("video") or name.endswith(VIDEO_EXT) or ext in VIDEO_EXT:
        return "video"
    if mime.startswith("audio") or name.endswith(AUDIO_EXT) or ext in AUDIO_EXT:
        return "audio"
    if mime.startswith("image") or name.endswith(PHOTO_EXT):
        return "photo"
    return None


def _first_line(text):
    for line in (text or "").splitlines():
        line = re.sub(r"#[\w]+", "", line).strip()
        if line:
            return line[:140]
    return ""


def _clean(name):
    if not name:
        return ""
    base = name.rsplit(".", 1)[0] if "." in name else name
    base = base.replace("_", " ")
    base = re.sub(r"(?<=\S)\.(?=\S)", " ", base)
    return re.sub(r"\s+", " ", base).strip()


def _mini(m):
    try:
        thumbs = (m.photo.sizes if m.photo else (m.document.thumbs or [])) or []
        for t in thumbs:
            if isinstance(t, types.PhotoStrippedSize):
                return base64.b64encode(utils.stripped_photo_to_jpg(t.bytes)).decode()
    except Exception:
        pass
    return ""


def note_update(m):
    if not m.document or not m.message:
        return False
    mm = UPD.match(m.message.strip())
    if not mm:
        return False
    code = int(mm.group(1))
    cur = state["update"]
    if not cur or cur["code"] <= code:
        state["update"] = {"id": m.id, "code": code, "name": mm.group(2) or mm.group(1),
                           "size": m.file.size if m.file else 0}
    return True


def add(m):
    if m and note_update(m):
        return False
    k = kind(m)
    if not k:
        return False
    f = m.file
    cap = m.message or ""
    fname = (f.name or "") if f else ""
    if k == "photo" and not fname:
        fname = f"photo_{m.id}.jpg"
    title = _first_line(cap)
    perf = ""
    if k == "audio" and f:
        title = title or (f.title or "")
        perf = f.performer or ""
    title = title or _clean(fname) or f"{k.title()} {m.id}"
    reply = 0
    if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
        reply = m.reply_to.reply_to_msg_id
    index[m.id] = {
        "id": m.id, "kind": k, "title": title, "fileName": fname,
        "mime": (f.mime_type or "") if f else "image/jpeg",
        "size": (f.size or 0) if f else 0,
        "big": bool(f and k == "video" and (f.size or 0) > BOT_DL_LIMIT),
        "dc": getattr(m.document, "dc_id", 0) or 0,
        "duration": int(f.duration or 0) if f and k in ("video", "audio") else 0,
        "width": (f.width or 0) if f and k in ("video", "photo") else 0,
        "height": (f.height or 0) if f and k in ("video", "photo") else 0,
        "date": int(m.date.timestamp()) if m.date else 0,
        "performer": perf,
        "tags": sorted(set(t.lower() for t in re.findall(r"#(\w+)", cap))),
        "mini": _mini(m), "replyTo": reply, "caption": cap[:1000],
    }
    state["version"] += 1
    return True


async def periodic_rescan():
    # Har 15 min light rescan: live event miss ho jaye (net gaya tha) to bhi pakad lega
    while True:
        await asyncio.sleep(900)
        try:
            if not state["scanning"]:
                await scan()
        except Exception:
            pass


async def scan():
    if state["scanning"]:
        return
    state["scanning"] = True
    try:
        start, empty = 1, 0
        found = {}
        while empty < 5:
            msgs = await client.get_messages(group, ids=list(range(start, start + 100)))
            got = [m for m in msgs if m]
            empty = 0 if got else empty + 1
            for m in got:
                if add(m):
                    found[m.id] = index[m.id]
            start += 100
            state["scanned"] = start - 1
        # channel se hat chuki files list se bhi hatao
        for mid in [k for k in index if k not in found]:
            index.pop(mid, None)
        state["version"] += 1
        state["channelError"] = ""
        save_index()
        far = sum(1 for v in index.values() if v.get("kind") == "video" and not v.get("big")
                  and (v.get("dc") or 0) in FAR_DCS)
        log(f"Scan ho gaya: {len(index)} files" + (f" · door-DC videos: {far}" if far else ""))
        await auto_heal_scan()
    except Exception as e:
        state["channelError"] = f"{type(e).__name__}: {e}"
        log("Scan error: " + state["channelError"])
    finally:
        state["scanning"] = False


msg_cache = {}


async def get_msg(mid):
    c = msg_cache.get(mid)
    if c and time.time() - c[0] < 1800:
        return c[1]
    m = await client.get_messages(group, ids=mid)
    if m:
        msg_cache[mid] = (time.time(), m)
        if len(msg_cache) > 500:
            msg_cache.clear()
    return m


async def _purge_dc_sender(doc):
    """Doosre DC ka cached (mar chuka) sender hatao — agla attempt fresh auth banayega."""
    try:
        dc = getattr(doc, "dc_id", 0) or 0
        if dc and client and dc != client.session.dc_id:
            ent = client._borrowed_senders.pop(dc, None)
            if ent:
                try:
                    await ent[1].disconnect()
                except Exception:
                    pass
    except Exception:
        pass


async def _get_chunk(doc, off, size, mid=0):
    """Ek CHUNK download karo, retry + reconnect ke saath (FloodWait wait, backoff,
    2 fail par door-DC sender purge)."""
    last = None
    for attempt in range(5):
        try:
            async for c in client.iter_download(doc, offset=off, request_size=CHUNK,
                                                limit=1, file_size=size):
                return bytes(c)
            return b""
        except FloodWaitError as e:
            last = e
            await asyncio.sleep(min(float(e.seconds + 1), 60.0))
        except Exception as e:
            last = e
            await asyncio.sleep(min(2.0 ** attempt, 10.0))
            try:
                if not client.is_connected():
                    await client.connect()
            except Exception:
                pass
            if attempt >= 1:
                await _purge_dc_sender(doc)
    if mid:
        note_stream_fail(mid, doc)
    raise RuntimeError(f"chunk@{off} 5 baar fail: {last}")


async def parallel_chunks(doc, offset, size, mid=0):
    total = -(-(size - offset) // CHUNK)
    dc = getattr(doc, "dc_id", 0) or 0
    far = bool(dc and client and dc != client.session.dc_id)
    # door DC me latency zyada — extra parallel streams se speed aati hai
    n = max(1, min(6 if far else PARALLEL, total))
    start_idx = offset // CHUNK
    queues = [asyncio.Queue(maxsize=3) for _ in range(n)]

    async def worker(i):
        try:
            idx = start_idx + i
            end_idx = start_idx + total
            while idx < end_idx:
                c = await _get_chunk(doc, idx * CHUNK, size, mid)
                await queues[i].put(c)
                idx += n
        except Exception as e:
            log("chunk error: " + str(e))
        finally:
            await queues[i].put(None)

    tasks = [asyncio.create_task(worker(i)) for i in range(n)]
    try:
        k = 0
        while True:
            c = await queues[k % n].get()
            if c is None:
                break
            yield c
            k += 1
    finally:
        for t in tasks:
            t.cancel()


# ---------------- door-DC file self-heal (auto re-upload, bina disk ke) ----------------
# 1MB chunk download -> 2x512KB part upload (ek saath, disk par kuch nahi likha jata).
# Nayi copy ghar wale DC pe banti hai, verify hoti hai, purani delete. Resume-safe:
# beech me app band ho jaye to wahi se shuru (heal.json me parts ka record).

UP_PART = 512 * 1024
HEAL_WORKERS = 3
heal_state = {}          # mid(int) -> {status,pct,name,new_id,err}
_heal_queue = []
_heal_running = False
_fail_count = {}
_heal_tried = set()


def queue_heal(mid, doc, force=False):
    if not client or not doc:
        return
    if not force and mid in _heal_tried:
        return
    _heal_tried.add(mid)
    size = getattr(doc, "size", 0) or 0
    if size <= 0 or size > BOT_DL_LIMIT:
        return  # 2GB+ file bot upload hi nahi kar sakta
    dc = getattr(doc, "dc_id", 0) or 0
    if not dc or dc == client.session.dc_id:
        return  # ghar wale DC pe hai, heal ki zaroorat nahi
    hs = heal_state.get(mid)
    if hs and hs.get("status") in ("queued", "running"):
        return
    heal_state[mid] = {"status": "queued", "pct": 0, "name": "", "new_id": 0, "err": ""}
    _heal_queue.append(mid)
    asyncio.ensure_future(_heal_pump())


def note_stream_fail(mid, doc):
    """Stream mara to gino — 2 baar mar jaye to self-heal shuru (chahe DC jo bhi ho)."""
    try:
        _fail_count[mid] = _fail_count.get(mid, 0) + 1
        if _fail_count[mid] >= 2:
            queue_heal(mid, doc)
    except Exception:
        pass


async def auto_heal_scan():
    """Scan ke baad: door-DC (America/Europe) ki videos auto-fix queue me."""
    if not SETTINGS.get("auto_heal") or not client:
        return
    n = 0
    for it in list(index.values()):
        if it.get("kind") != "video" or it.get("big"):
            continue
        if (it.get("dc") or 0) in FAR_DCS and it["id"] not in _heal_tried:
            try:
                m = await get_msg(it["id"])
                if m and m.document:
                    queue_heal(it["id"], m.document)
                    n += 1
            except Exception:
                pass
    if n:
        log(f"Auto-fix: {n} door-DC video(s) queue me daal di — bot khud theek karega")


def resume_heals():
    """Boot par: pichle adhoore heal wapas shuru karo."""
    try:
        for mid, rs in heal_load().items():
            mid = int(mid)
            _heal_tried.add(mid)
            heal_state[mid] = {"status": "queued", "pct": 0, "name": rs.get("name", ""),
                               "new_id": 0, "err": ""}
            _heal_queue.append(mid)
        if _heal_queue:
            asyncio.ensure_future(_heal_pump())
    except Exception:
        pass


def heal_load():
    try:
        return json.loads((HOME / "heal.json").read_text())
    except Exception:
        return {}


def heal_save(d):
    try:
        tmp = str(HOME / "heal.json") + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, HOME / "heal.json")
    except Exception:
        pass


async def _heal_pump():
    global _heal_running
    if _heal_running:
        return
    _heal_running = True
    try:
        while _heal_queue:
            mid = _heal_queue.pop(0)
            try:
                await _heal_run(mid)
            except Exception as e:
                hs = heal_state.get(mid) or {"pct": 0, "name": "", "new_id": 0}
                hs.update(status="failed", err=f"{type(e).__name__}: {str(e)[:80]}")
                heal_state[mid] = hs
                log(f"Heal fail ({hs.get('name') or mid}): {hs['err']}")
    finally:
        _heal_running = False


async def _heal_run(mid):
    m = await client.get_messages(group, ids=mid)
    if not m or not m.document:
        heal_state.pop(mid, None)
        return
    doc = m.document
    size = doc.size
    heal_state[mid] = {"status": "running", "pct": 0, "name": (m.file.name or "")[:60],
                       "new_id": 0, "err": ""}
    log(f"Heal shuru: {heal_state[mid]['name']} ({size // 1048576} MB, dc{doc.dc_id} -> dc{client.session.dc_id})")
    rs = heal_load().get(str(mid), {})
    fid = rs.get("file_id") or random.randrange(1, 2 ** 63)
    parts_total = -(-size // UP_PART)
    ul_done = set(rs.get("ul_done", []))
    if rs.get("size") != size:
        ul_done = set()
    CH = 2 * UP_PART  # 1MB download -> 2 part upload
    nchunks = -(-size // CH)
    todo_c = [c for c in range(nchunks)
              if any(p not in ul_done for p in range(2 * c, min(2 * c + 2, parts_total)))]
    lock = asyncio.Lock()
    stop = asyncio.Event()

    async def work(wid):
        while True:
            async with lock:
                if stop.is_set() or not todo_c:
                    return
                cidx = todo_c.pop(0)
            off = cidx * CH
            data = None
            for att in range(6):
                try:
                    buf = bytearray()
                    async for c in client.iter_download(doc, offset=off, request_size=CH,
                                                        limit=1, file_size=size):
                        buf += c
                    data = bytes(buf)
                    break
                except FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds + 1, 120))
                except Exception:
                    if att >= 1:
                        await _purge_dc_sender(doc)
                    await asyncio.sleep(min(2.0 ** att, 10.0))
            if not data:
                stop.set()
                heal_state[mid]["err"] = f"download chunk {cidx} fail"
                return
            for p in range(2 * cidx, min(2 * cidx + 2, parts_total)):
                if p in ul_done:
                    continue
                seg = data[p * UP_PART - off: p * UP_PART - off + UP_PART]
                for att in range(4):
                    try:
                        await client(functions.upload.SaveBigFilePartRequest(fid, p, parts_total, seg))
                        break
                    except FloodWaitError as e:
                        await asyncio.sleep(min(e.seconds + 1, 120))
                    except Exception:
                        await asyncio.sleep(min(2.0 ** att, 8.0))
                else:
                    stop.set()
                    heal_state[mid]["err"] = f"upload part {p} fail"
                    return
                async with lock:
                    ul_done.add(p)
                    heal_state[mid]["pct"] = int(len(ul_done) * 100 / parts_total)
                    if len(ul_done) % 20 == 0 or len(ul_done) == parts_total:
                        heal_save({str(mid): {"file_id": fid, "size": size,
                                              "ul_done": sorted(ul_done)}})

    await asyncio.gather(*[work(i) for i in range(HEAL_WORKERS)])
    if stop.is_set():
        heal_state[mid]["status"] = "failed"
        log(f"Heal fail: {heal_state[mid].get('name') or mid} — {heal_state[mid]['err']}")
        return
    # sab parts upload ho gaye — ab asli caption/attributes ke saath message bhejo
    m = await client.get_messages(group, ids=mid)
    if not m or not m.document:
        heal_state[mid]["status"] = "failed"
        heal_state[mid]["err"] = "source message gayab"
        return
    d2 = m.document
    fname = next((a.file_name for a in d2.attributes
                  if isinstance(a, types.DocumentAttributeFilename)), "video.mkv")
    attrs = list(d2.attributes)  # filename attr bhi saath jaye
    r = await client(functions.messages.SendMediaRequest(
        group, types.InputMediaUploadedDocument(
            file=types.InputFileBig(fid, parts_total, fname),
            mime_type=d2.mime_type or "video/mp4",
            attributes=attrs, force_file=True),
        message=m.message or "", random_id=random.randrange(1, 2 ** 63)))
    new_id = 0
    for u in (getattr(r, "updates", None) or []):
        if isinstance(u, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
            new_id = u.message.id
    if not new_id:
        raise RuntimeError("nayi message id nahi mili")
    ok = False
    m2 = await client.get_messages(group, ids=new_id)
    if m2 and m2.document:
        try:
            async for _c in client.iter_download(m2.document, offset=0, request_size=UP_PART,
                                                 limit=1, file_size=m2.document.size):
                pass
            ok = True
        except Exception:
            ok = False
    if ok:
        try:
            await client.delete_messages(group, [mid])
        except Exception:
            pass
        heal_state[mid] = {"status": "done", "pct": 100, "name": heal_state[mid].get("name", ""),
                           "new_id": new_id, "err": ""}
        d = heal_load()
        d.pop(str(mid), None)
        heal_save(d)
        log(f"Heal ho gaya: {heal_state[mid]['name']} — nayi copy dc{client.session.dc_id} pe, purani delete")
    else:
        heal_state[mid]["status"] = "failed"
        heal_state[mid]["err"] = "nayi copy verify fail"


def _count(n):
    now = time.time()
    if now - _speed["t"] >= 1.0:
        if _speed["t"]:
            inst = _speed["n"] / (now - _speed["t"])
            state["speed"] = int(inst if not state["speed"] else state["speed"] * 0.6 + inst * 0.4)
        _speed["t"], _speed["n"] = now, 0
    _speed["n"] += n
    _speed["last"] = now


thumb_cache = {}


async def thumb(m):
    if m.id in thumb_cache:
        return thumb_cache[m.id]
    data = None
    try:
        if m.photo:
            sizes = [s for s in m.photo.sizes if hasattr(s, "w") and not isinstance(s, types.PhotoStrippedSize)]
            pick = sorted([s for s in sizes if s.w >= 300], key=lambda s: s.w)
            ts = pick[0] if pick else (max(sizes, key=lambda s: s.w) if sizes else None)
            if ts is not None:
                data = await client.download_media(m, file=bytes, thumb=ts)
        elif m.document and m.document.thumbs:
            real = [t for t in m.document.thumbs if not isinstance(t, types.PhotoStrippedSize)]
            if real:
                data = await client.download_media(m, file=bytes, thumb=real[-1])
    except Exception as e:
        log("thumb error " + str(e))
    if len(thumb_cache) > 300:
        thumb_cache.clear()
    if data:
        thumb_cache[m.id] = data
    return data


# ---------------- HTTP (aiohttp) — TV app wale endpoints ----------------

from aiohttp import web


async def api_status(request):
    if time.time() - _speed.get("last", 0) > 3:
        state["speed"] = 0
    state["online"] = bool(client and client.is_connected())
    state["heal"] = {str(k): v for k, v in heal_state.items()}
    return web.json_response({**state, "count": len(index)})


async def api_list(request):
    items = sorted(index.values(), key=lambda x: x["id"], reverse=True)
    return web.json_response(items)


async def api_scan(request):
    asyncio.ensure_future(scan())
    return web.json_response({})


async def api_heal(request):
    """Fix now: kisi bhi video ko haath se heal queue me daalo."""
    mid = int(request.match_info["id"])
    m = await get_msg(mid)
    if not m or not m.document:
        raise web.HTTPNotFound()
    _heal_tried.discard(mid)
    queue_heal(mid, m.document, force=True)
    return web.json_response({"ok": True})


async def thumb_h(request):
    try:
        m = await get_msg(int(request.match_info["id"]))
    except Exception:
        m = None
    if not m or not m.media:
        raise web.HTTPNotFound()
    data = await thumb(m)
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg")


async def stream_h(request):
    try:
        m = await get_msg(int(request.match_info["id"]))
    except Exception:
        m = None
    if not m or not m.media:
        raise web.HTTPNotFound()

    if m.photo:
        data = await client.download_media(m, file=bytes)
        return web.Response(body=data, content_type="image/jpeg")

    size = m.file.size
    start, end = 0, size - 1
    rng = request.headers.get("Range")
    if rng:
        r = re.match(r"bytes=(\d*)-(\d*)", rng)
        if r and r.group(1):
            start = int(r.group(1))
            if r.group(2):
                end = min(int(r.group(2)), size - 1)
        elif r and r.group(2):
            start = max(size - int(r.group(2)), 0)
    if start >= size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1

    headers = {
        "Content-Type": m.file.mime_type or "application/octet-stream",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp = web.StreamResponse(status=206 if rng else 200, headers=headers)
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp

    aligned = start - (start % CHUNK)
    skip = start - aligned
    remaining = length
    try:
        async for chunk in parallel_chunks(m.document, aligned, size, mid=m.id):
            if skip:
                chunk, skip = chunk[skip:], 0
            chunk = chunk[:remaining]
            await resp.write(chunk)
            _count(len(chunk))
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # player ne seek / stop kiya
    return resp


async def playlist(request):
    lines = ["#EXTM3U"]
    for it in sorted(index.values(), key=lambda x: x["id"], reverse=True):
        if it["kind"] in ("video", "audio"):
            lines += [f"#EXTINF:-1 {it['duration']},{it['title']}",
                      f"{request.scheme}://{request.host}/f/{it['id']}"]
    return web.Response(text="\n".join(lines), content_type="audio/x-mpegurl")


async def home(request):
    import html
    rows = []
    for it in sorted(index.values(), key=lambda x: x["id"], reverse=True):
        t = html.escape(it["title"])
        if it["kind"] == "photo":
            rows.append(f'<a href="/f/{it["id"]}"><img src="/t/{it["id"]}" width="280" loading="lazy"></a>')
        else:
            rows.append(f'<p><a href="/f/{it["id"]}">[{it["kind"]}] {t}</a></p>')
    hh = [v for v in heal_state.values() if v.get("status") in ("queued", "running")]
    heal_line = f'<p>🛠 Self-heal: {html.escape(hh[0].get("name") or "")} — {hh[0].get("pct", 0)}%</p>' if hh else ""
    body = ("<meta name=viewport content='width=device-width'><h2>RC TV (Windows) — " +
            html.escape(state["title"] or "Telegram storage") + "</h2>"
            "<p><a href='/tv.m3u'>VLC playlist</a></p>" + heal_line + "".join(rows))
    return web.Response(text=body, content_type="text/html")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------- main (bot connect + events + server) ----------------

async def main_async():
    global client, group, BOOT_TS
    BOOT_TS = time.time()
    if not CFG["API_HASH"] or not (CFG["BOT_TOKEN"] or CFG["SESSION"]):
        raise RuntimeError("Config missing — .env me API_ID, API_HASH, BOT_TOKEN (ya SESSION), "
                           "GROUP_ID bharo (server/.env.example dekho)")
    if CFG["SESSION"]:
        # bot_token + api pair invalid ho sakta hai (ApiIdInvalidError) — seedha authed session
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(CFG["SESSION"]), CFG["API_ID"], CFG["API_HASH"])
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("SESSION string expire ho gaya — naya session chahiye")
        log("Session se login (bot_token skip)")
    else:
        client = TelegramClient(str(HOME / "tvbot.session"), CFG["API_ID"], CFG["API_HASH"])
        await client.start(bot_token=CFG["BOT_TOKEN"])
    state["dc"] = client.session.dc_id

    real_id, peer_type = utils.resolve_id(CFG["GROUP_ID"])
    try:
        group = await client.get_input_entity(peer_type(real_id))
    except ValueError:
        # bots group ko access_hash 0 se khol sakte hain
        group = types.InputPeerChannel(real_id, 0)

    load_index()
    resume_heals()
    try:
        ent = await client.get_entity(group)
        state["title"] = getattr(ent, "title", "") or state["title"]
    except Exception:
        pass
    log(f"Bot online (dc{client.session.dc_id}) · storage: {state['title'] or CFG['GROUP_ID']}")

    @client.on(events.NewMessage(chats=CFG["GROUP_ID"]))
    async def on_new(event):
        add(event.message)
        save_index()
        log("Naya: " + index[event.message.id]["title"])

    @client.on(events.MessageEdited(chats=CFG["GROUP_ID"]))
    async def on_edit(event):
        msg_cache.pop(event.message.id, None)
        if not add(event.message):
            index.pop(event.message.id, None)
        state["version"] += 1
        save_index()

    @client.on(events.MessageDeleted(chats=CFG["GROUP_ID"]))
    async def on_del(event):
        for mid in event.deleted_ids:
            index.pop(mid, None)
            if state["update"] and state["update"]["id"] == mid:
                state["update"] = None
        state["version"] += 1
        save_index()

    # ---- bot admin commands (Telegram DM se hi TV app jaisa control) ----
    @client.on(events.NewMessage(pattern=r"^/(status|scan|id|help)\b"))
    async def on_cmd(event):
        if event.is_channel and not event.is_group:
            return  # broadcast channel me bot ki reply spam karegi
        if not CFG["ADMIN_ID"] or event.sender_id != CFG["ADMIN_ID"]:
            if event.is_private:
                await event.reply("Ye commands sirf bot admin ke liye hain.")
            return
        cmd = event.pattern_match.group(1)
        if cmd == "id":
            await event.reply(f"chat_id: {event.chat_id}\naapki id: {event.sender_id}")
        elif cmd == "status":
            up = int(time.time() - BOOT_TS) if BOOT_TS else 0
            c = {}
            for v in index.values():
                c[v["kind"]] = c.get(v["kind"], 0) + 1
            lines = [f"RC TV Windows — status",
                     f"Videos: {c.get('video', 0)} · Gaane: {c.get('audio', 0)} · Photos: {c.get('photo', 0)} · Subs: {c.get('sub', 0)}",
                     f"Connection: {'online' if client and client.is_connected() else 'offline'} · Uptime: {up // 3600}h {(up % 3600) // 60}m",
                     f"Server: http://{state['lan'] or lan_ip()}:{CFG['PORT']}/"]
            hh = [v for v in heal_state.values() if v.get("status") in ("queued", "running")]
            if hh:
                lines.append(f"🛠 Self-heal: {hh[0].get('name') or 'file'} — {hh[0].get('pct', 0)}%")
            await event.reply("\n".join(lines))
        elif cmd == "scan":
            await event.reply("Storage dobara padh raha hoon…")
            await scan()
            await event.reply(f"Ho gaya: {len(index)} files")
        elif cmd == "help":
            await event.reply("Commands (sirf admin):\n/status — abhi ka haal\n/scan — storage dobara padho\n/id — chat id batao")

    app = web.Application()
    app.add_routes([
        web.get("/", home),
        web.get("/api/status", api_status),
        web.get("/api/list", api_list),
        web.get("/api/scan", api_scan),
        web.get("/api/heal/{id}", api_heal),
        web.get("/tv.m3u", playlist),
        web.get("/t/{id}", thumb_h),
        web.get("/f/{id}", stream_h),
        web.get("/live/{id}", live_page),
        web.get("/m3u/{id}", m3u_h),
        web.get("/seg/{id}/{u:.+}", seg_h),
        web.get("/file/{id}", file_h),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CFG["PORT"]).start()
    state["lan"] = lan_ip()
    state["ready"] = True
    state["error"] = ""
    log(f"Ready! Isi PC par: http://127.0.0.1:{CFG['PORT']}/  ·  TV/phone par: http://{state['lan']}:{CFG['PORT']}/")

    if not index:
        await scan()
    else:
        asyncio.ensure_future(scan())
    asyncio.ensure_future(periodic_rescan())
    await client.run_until_disconnected()


def _core_loop():
    while True:
        try:
            asyncio.run(main_async())
        except Exception as e:
            state["ready"] = False
            state["error"] = f"{type(e).__name__}: {e}"
            log("Error: " + state["error"])
            log(traceback.format_exc(limit=3))
        time.sleep(5)  # net gaya to dobara try


# ---------------- GUI (TV app jaisa — dark theme + poster grid) ----------------

try:
    import tkinter as tk
except Exception:  # GUI-less par bhi core chale
    tk = None

try:
    import io
    from PIL import Image, ImageTk, ImageDraw
except Exception:
    Image = ImageTk = ImageDraw = None

import urllib.request
from concurrent.futures import ThreadPoolExecutor

LOOP = None  # core thread ka asyncio loop

# TV app ki palette (app/src/main/res/values/colors.xml jaisi hi)
BG = "#050507"
CARD = "#141419"
GLASS = "#1B1B22"
GLASS2 = "#26262E"
ACC = "#FA2D48"
ACC2 = "#FF6B80"
SUB = "#A1A1A8"
TXT = "#FFFFFF"

IMG_W, IMG_H = 160, 90
CARD_W = IMG_W + 14
FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_S = ("Segoe UI", 8)
FONT_H = ("Segoe UI", 17, "bold")
KIND_TILE = {"video": ("#1B2434", "VIDEO"), "audio": ("#241B2E", "MUSIC"),
             "photo": ("#1B2E24", "PHOTO"), "sub": ("#222226", "SUBS")}


def post(fn):
    """GUI thread se core thread me coroutine bhejo."""
    if LOOP is None:
        log("Abhi ready nahi hai — thodi der baad try karo")
        return

    def _spawn():
        asyncio.ensure_future(fn())
    try:
        LOOP.call_soon_threadsafe(_spawn)
    except Exception:
        pass


def _players():
    out = []
    for name in ("vlc", "mpv"):
        p = shutil.which(name)
        if p:
            out.append(p)
    bases = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
             os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")]
    rels = [r"VideoLAN\VLC\vlc.exe", r"mpv\mpv.exe", r"MPV\mpv.exe"]
    for rp in rels:
        for b in bases:
            if b:
                p = os.path.join(b, rp)
                if os.path.exists(p):
                    out.append(p)
    return out


PLAYERS = _players()


def play_url(url):
    for exe in PLAYERS:
        try:
            subprocess.Popen([exe, url])
            return True
        except Exception:
            continue
    try:
        os.startfile(url)  # Windows default app (browser)
        return True
    except Exception:
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False


def base_url():
    return f"http://127.0.0.1:{CFG['PORT']}"


def human(n):
    n = float(n or 0)
    if n <= 0:
        return "—"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "—"


def icon_path():
    cands = [Path(getattr(sys, "_MEIPASS", "")) / "rctv.ico", app_dir() / "rctv.ico"]
    for p in cands:
        try:
            if p.exists():
                return str(p)
        except Exception:
            pass
    return None


def ext_of(it):
    fn = it.get("fileName") or ""
    if "." in fn:
        return fn.rsplit(".", 1)[-1].upper()[:6]
    return (it.get("mime") or "").split("/")[-1].upper()[:6] or "?"


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"RC TV — Windows v{VERSION}")
        root.configure(bg=BG)
        try:
            root.state("zoomed")  # TV app jaisa full window
        except Exception:
            root.geometry("1020x660")
        root.minsize(720, 460)
        ip = icon_path()
        if ip:
            try:
                root.iconbitmap(ip)
            except Exception:
                pass

        self.filter = "all"
        self.qvar = tk.StringVar()
        self.thumbs = {}      # mid -> PhotoImage (ref rakhna zaroori)
        self.thumb_real = set()  # jinke asli thumb aa chuke (placeholder nahi)
        self.pending = set()  # thumb fetch chal raha hai
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.cards = {}       # mid -> img Label (thumb baad me update karne ke liye)
        self.sig = None
        self.cols = 1

        self._topbar(root)
        self.banner = tk.Label(root, text="", bg="#3A0D14", fg=ACC2, font=FONT_S,
                               anchor="w", padx=10, pady=3)
        self.banner_visible = False
        self._chipsbar(root)   # chips grid se PEHLE pack ho (warna neeche chale jate hain)
        self._grid(root)
        self._statusbar(root)

        globals()["_LOG_UI"] = self.ui_log
        root.after(700, self.poll)

    # ---- log: app.log me (GUI par logbox nahi — TV app jaisa saaf look) ----
    def ui_log(self, line):
        try:
            f = HOME / "app.log"
            if f.exists() and f.stat().st_size > 1_000_000:
                data = f.read_text(errors="ignore")[-200_000:]
                f.write_text(data)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    # ---------- layout ----------
    def _topbar(self, root):
        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=16, pady=(10, 2))
        tk.Label(bar, text="RC TV", bg=BG, fg=ACC, font=FONT_H).pack(side="left")
        self.h_title = tk.Label(bar, text="", bg=BG, fg=TXT, font=("Segoe UI", 11, "bold"))
        self.h_title.pack(side="left", padx=(12, 0))
        self.h_sub = tk.Label(bar, text="", bg=BG, fg=SUB, font=FONT_S)
        self.h_sub.pack(side="left", padx=(12, 0))
        self._chip(bar, "Rescan", self.do_scan).pack(side="right", padx=(8, 0))
        self._chip(bar, "📺 Live TV", self.open_live).pack(side="right", padx=(8, 0))
        self._chip(bar, "About", self.about).pack(side="right")

    def _chipsbar(self, root):
        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", padx=16, pady=(4, 6))
        self.chips = {}
        for key, label in (("all", "Sab"), ("video", "Videos"), ("audio", "Music"), ("photo", "Photos")):
            c = self._chip(row, label, lambda k=key: self.set_filter(k), key=key, small=True)
            c.pack(side="left", padx=(0, 6))
            self.chips[key] = c
        tk.Label(row, text="Search", bg=BG, fg=SUB, font=FONT_S).pack(side="right", padx=(8, 4))
        ent = tk.Entry(row, textvariable=self.qvar, bg=GLASS, fg=TXT, insertbackground=TXT,
                       relief="flat", width=26, font=FONT)
        ent.pack(side="right")
        ent.bind("<KeyRelease>", lambda e: self.refresh())

    def _chip(self, parent, text, cmd, key=None, small=False):
        active = key is not None and key == self.filter
        c = tk.Label(parent, text=text, bg=(ACC if active else GLASS), fg=TXT,
                     font=FONT_S if small else FONT, padx=12, pady=4, cursor="hand2")
        c.bind("<Button-1>", lambda e: cmd())
        c.bind("<Enter>", lambda e: c.configure(bg=GLASS2))
        c.bind("<Leave>", lambda e: c.configure(bg=(ACC if (key is not None and key == self.filter) else GLASS)))
        return c

    def set_filter(self, k):
        self.filter = k
        for key, w in self.chips.items():
            w.configure(bg=(ACC if key == k else GLASS))
        self.refresh()

    def _grid(self, root):
        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.wrap = wrap
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        self.sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                               bg=GLASS, troughcolor=BG, activebackground=GLASS2,
                               width=12)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.gridfr = tk.Frame(self.canvas, bg=BG)
        self._gwin = self.canvas.create_window((12, 8), window=self.gridfr, anchor="nw")
        self.gridfr.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind_all("<MouseWheel>", self.on_wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.empty = tk.Label(root, text="", bg=BG, fg=SUB, font=("Segoe UI", 12), anchor="center")
        self.menu = tk.Menu(self.canvas, tearoff=0, bg=GLASS, fg=TXT, bd=0,
                            activebackground=ACC, activeforeground=TXT,
                            font=FONT_S)
        self.menu.add_command(label="Play / Open", command=lambda: self.open_item(self.menu_mid, self.menu_kind))
        self.menu.add_command(label="Copy link", command=lambda: self.copy_link(self.menu_mid))
        self.menu.add_command(label="Fix now (re-upload)", command=lambda: self.fix_now(self.menu_mid))
        self.menu_mid = 0
        self.menu_kind = "video"

    def _statusbar(self, root):
        self.status = tk.Label(root, text="Shuru ho raha hai…", bg=GLASS, fg=SUB,
                               font=FONT_S, anchor="w", padx=10, pady=4)
        self.status.pack(fill="x", side="bottom")

    def on_resize(self, e):
        try:
            self.canvas.itemconfigure(self._gwin, width=max(200, e.width - 24))
        except Exception:
            pass
        cols = max(1, (e.width - 24) // (CARD_W + 10))
        if cols != self.cols:
            self.cols = cols
            self.rebuild()

    def on_wheel(self, e):
        try:
            self.canvas.yview_scroll(-1 * (int(e.delta) // 120) * 4, "units")
        except Exception:
            pass

    # ---------- data ----------
    def visible(self):
        its = [i for i in index.values() if i.get("kind") != "sub"]
        if self.filter != "all":
            its = [i for i in its if i["kind"] == self.filter]
        q = self.qvar.get().strip().lower()
        if q:
            its = [i for i in its if q in (i["title"] or "").lower()
                   or q in (i.get("fileName") or "").lower()]
        return sorted(its, key=lambda x: x["id"], reverse=True)

    def row_status(self, it):
        mid = it["id"]
        hs = heal_state.get(mid)
        if hs:
            st = hs.get("status")
            if st == "queued":
                return "Fix queued…"
            if st == "running":
                return f"Fixing {hs.get('pct', 0)}%"
            if st == "done":
                return "Fixed"
            if st == "failed":
                return "Fix fail"
        if it.get("big"):
            return "2GB+ (bot limit)"
        if (it.get("dc") or 0) in FAR_DCS:
            return "Door DC — fix hoga" if SETTINGS.get("auto_heal") else "Door DC"
        return ""

    def card(self, parent, it):
        mid = it["id"]
        f = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=GLASS, cursor="hand2")
        img = tk.Label(f, bg=CARD, image=self.thumbs.get(mid) or self._tile(mid, it))
        img.pack(padx=2, pady=2)
        self.cards[mid] = img
        tk.Label(f, text=it["title"], bg=CARD, fg=TXT, font=FONT_B, wraplength=IMG_W,
                 justify="left").pack(padx=5, pady=(2, 0))
        st = self.row_status(it)
        meta = f"{human(it.get('size'))} · {ext_of(it)}"
        if st:
            meta += " · " + st
        hot = ("Fix" in st or "Door" in st or "2GB" in st) if st else False
        tk.Label(f, text=meta, bg=CARD, fg=(ACC2 if hot else SUB), font=FONT_S,
                 wraplength=IMG_W, justify="left").pack(padx=5, pady=(1, 5))
        f.bind("<Button-1>", lambda e, m=mid, k=it["kind"]: self.open_item(m, k))
        f.bind("<Button-3>", lambda e, m=mid, k=it["kind"]: self.popup(e, m, k))
        f.bind("<Enter>", lambda e: f.configure(highlightthickness=2, highlightbackground=ACC))
        f.bind("<Leave>", lambda e: f.configure(highlightthickness=1, highlightbackground=GLASS))
        return f

    # ---------- thumbnails (apne hi server ke /t/{id} se, disk cache) ----------
    def _tile(self, mid, it):
        col, word = KIND_TILE.get(it["kind"], ("#1C1C22", "FILE"))
        if Image and ImageTk and ImageDraw:
            im = Image.new("RGB", (IMG_W, IMG_H), col)
            d = ImageDraw.Draw(im)
            d.text((IMG_W // 2 - len(word) * 3, IMG_H // 2 - 5), word, fill="#5A5A64")
            ph = ImageTk.PhotoImage(im)
        else:
            ph = tk.PhotoImage(width=IMG_W, height=IMG_H)
            ph.put(col, to=(0, 0, IMG_W, IMG_H))
        self.thumbs[mid] = ph
        return ph

    def want_thumb(self, mid):
        if Image is None or ImageTk is None:
            return
        if mid in self.pending or mid in self.thumb_real:
            return
        self.pending.add(mid)
        self.pool.submit(self._fetch_thumb, mid)

    def _fetch_thumb(self, mid):
        img = None
        try:
            cache = HOME / "thumbs"
            cache.mkdir(parents=True, exist_ok=True)
            cf = cache / f"{mid}.jpg"
            if cf.exists() and cf.stat().st_size > 100:
                img = Image.open(cf)
            else:
                data = urllib.request.urlopen(f"{base_url()}/t/{mid}", timeout=25).read()
                cf.write_bytes(data)
                img = Image.open(io.BytesIO(data))
        except Exception:
            img = None
        try:
            if img is not None:
                im = img.convert("RGB")
                im.thumbnail((IMG_W, IMG_H))
                tile = Image.new("RGB", (IMG_W, IMG_H), CARD)
                tile.paste(im, ((IMG_W - im.width) // 2, (IMG_H - im.height) // 2))
                self.root.after(0, self._thumb_done, mid, tile)
                return
        except Exception:
            pass
        self.root.after(0, self._thumb_done, mid, None)

    def _thumb_done(self, mid, tile):
        self.pending.discard(mid)
        if tile is None:
            return  # thumb nahi mili — placeholder tile chalega
        try:
            ph = ImageTk.PhotoImage(tile)
        except Exception:
            return
        self.thumbs[mid] = ph
        self.thumb_real.add(mid)
        lab = self.cards.get(mid)
        if lab:
            try:
                if lab.winfo_exists():
                    lab.configure(image=ph)
            except Exception:
                pass

    # ---------- actions ----------
    def open_item(self, mid, kind):
        # v1.1 wala playback: default browser (TV app jaisa full-screen player)
        url = f"{base_url()}/f/{mid}"
        try:
            os.startfile(url)
        except Exception:
            webbrowser.open(url)

    def open_live(self):
        LiveWin(self.root)

    def copy_link(self, mid):
        if mid:
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{base_url()}/f/{mid}")

    def fix_now(self, mid):
        if not mid:
            return

        async def _fix():
            m = await get_msg(mid)
            if m and m.document:
                _heal_tried.discard(mid)
                queue_heal(mid, m.document, force=True)
        post(_fix())

    def do_scan(self):
        post(scan())

    def popup(self, e, mid, kind):
        self.menu_mid, self.menu_kind = mid, kind
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def about(self):
        from tkinter import messagebox
        messagebox.showinfo(
            "RC TV", f"RC TV — Windows v{VERSION}\n"
            f"Storage: {state['title'] or CFG['GROUP_ID']} · Data: {HOME}\n\n"
            f"• Video/Music/Photo — sab APP KE ANDAR chalte hain (built-in mpv player)\n"
            f"• 📺 Live TV: 2000+ channels (JioStar/+PlusTV/InPlex+)\n"
            f"• TV/phone (same WiFi): http://{state['lan'] or lan_ip()}:{CFG['PORT']}/\n\n"
            f"Player keys: Esc/q band · Space pause · ←→ seek · Up/Down volume · F fullscreen\n"
            f"Right-click = menu · door-DC video khud fix hoti hai.")

    def refresh(self):
        self.sig = None  # force rebuild
        self.poll()

    # ---------- 1-sec poll ----------
    def update_header(self):
        tot = sum(i.get("size") or 0 for i in index.values())
        vids = sum(1 for i in index.values() if i["kind"] == "video")
        self.h_title.configure(text=state["title"] or "")
        self.h_sub.configure(text=f"{len(index)} files · {human(tot)} · {vids} videos")

    def update_status(self):
        bits = []
        bits.append("Online" if state["online"] else "Connect ho raha hai…")
        if state["dc"]:
            bits.append(f"dc{state['dc']}")
        if state["speed"]:
            bits.append(f"{state['speed'] / 1e6:.1f} MB/s")
        if state["scanning"]:
            bits.append("scanning…")
        hh = [v for v in heal_state.values() if v.get("status") in ("queued", "running")]
        if hh:
            bits.append(f"Heal: {(hh[0].get('name') or 'file')[:28]} {hh[0].get('pct', 0)}%")
        self.status.configure(text="  ·  ".join(bits) +
                              f"   |   TV/phone: http://{state['lan'] or lan_ip()}:{CFG['PORT']}/")

    def poll(self):
        try:
            err = state["error"]
            if err and not self.banner_visible:
                self.banner.configure(text="⚠ " + err + "   —   app khud dobara connect karega…")
                self.banner.pack(fill="x", padx=16, pady=(0, 4), before=self.wrap)
                self.banner_visible = True
            elif not err and self.banner_visible:
                self.banner.pack_forget()
                self.banner_visible = False

            self.update_header()
            self.update_status()

            heal_sig = tuple(sorted((k, v.get("status"), (v.get("pct") or 0) // 5)
                                    for k, v in heal_state.items()))
            sig = (state["version"], self.filter, self.qvar.get().strip().lower(),
                   heal_sig, len(index), self.cols, state["error"])
            if sig != self.sig:
                self.sig = sig
                self.rebuild()
        except Exception:
            pass
        self.root.after(700, self.poll)

    def rebuild(self):
        try:
            frac = self.canvas.yview()[0]
            for w in self.gridfr.winfo_children():
                w.destroy()
            self.cards.clear()
            items = self.visible()
            for i, it in enumerate(items):
                r, c = divmod(i, self.cols)
                f = self.card(self.gridfr, it)
                f.grid(row=r, column=c, padx=5, pady=5, sticky="n")
                self.want_thumb(it["id"])
            if items:
                self.empty.pack_forget()
            else:
                self.empty.configure(text="Scan ho raha hai…" if state["scanning"] else "Yahan abhi kuch nahi hai")
                try:
                    self.empty.pack(fill="x", padx=16, pady=14, before=self.wrap)
                except Exception:
                    pass
            self.canvas.yview_moveto(frac)
        except Exception:
            pass


# ================= playback: v1.1 style — browser me chalega =================
# Video/audio/photo: tile click -> default browser (os.startfile/webbrowser) — v1.1 wala
# Live TV: local player page (hls.js) + server-side HLS proxy (headers/CORS server solve karta hai)
from urllib.parse import urljoin

LIVE_CHANS = []  # LiveWin parse ke waqt bhar jata hai ({name,url,backup,hdrs,drm,logo,_i})


def b64e(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def b64d(s):
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode(errors="ignore")
    except Exception:
        return ""


def _live_ch(idx):
    try:
        return LIVE_CHANS[int(idx)]
    except Exception:
        return None


def _up_headers(url, hdrs):
    h = {"User-Agent": UA_LIVE}
    for k, v in (hdrs or {}).items():
        try:
            h[str(k).lower()] = str(v)
        except Exception:
            pass
    return h


# ---- live: HLS playlist proxy (headers server-side, segments localhost par) ----
async def m3u_h(request):
    ch = _live_ch(request.match_info["id"])
    if not ch:
        raise web.HTTPNotFound()
    url = ch["backup"] if request.query.get("b") and ch.get("backup") else ch["url"]
    hdrs = ch["hdrs"]
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=_up_headers(url, hdrs),
                         timeout=aiohttp.ClientTimeout(total=25)) as r:
            txt = await r.text()
            base = str(r.url)
    out = []
    cid = request.match_info["id"]
    qs = "?b=1" if request.query.get("b") else ""
    for line in txt.splitlines():
        ls = line.strip()
        if not ls:
            continue
        if ls.startswith("#"):
            ls = re.sub(r'URI="([^"]+)"',
                        lambda m: 'URI="/seg/%s/%s%s"' % (cid, b64e(urljoin(base, m.group(1))), qs),
                        ls)
            out.append(ls)
        else:
            out.append("/seg/%s/%s%s" % (cid, b64e(urljoin(base, ls)), qs))
    return web.Response(text="\n".join(out),
                        content_type="application/vnd.apple.mpegurl")


async def seg_h(request):
    ch = _live_ch(request.match_info["id"])
    u = b64d(request.match_info["u"])
    if not ch or not u:
        raise web.HTTPNotFound()
    hdrs = _up_headers(u, ch["hdrs"])
    rng = request.headers.get("Range")
    if rng:
        hdrs["Range"] = rng
    async with aiohttp.ClientSession() as s:
        async with s.get(u, headers=hdrs) as r:
            data = await r.read()
            ct = r.headers.get("Content-Type", "video/mp2t")
    return web.Response(body=data, content_type=ct)


async def file_h(request):
    ch = _live_ch(request.match_info["id"])
    if not ch:
        raise web.HTTPNotFound()
    url = ch["backup"] if request.query.get("b") and ch.get("backup") else ch["url"]
    hdrs = _up_headers(url, ch["hdrs"])
    rng = request.headers.get("Range")
    if rng:
        hdrs["Range"] = rng
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=hdrs) as r:
            out_h = {k: v for k, v in r.headers.items()
                     if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
            resp = web.StreamResponse(status=r.status, headers=out_h)
            await resp.prepare(request)
            try:
                async for c in r.content.iter_chunked(65536):
                    await resp.write(c)
            except (ConnectionResetError, asyncio.CancelledError):
                pass
    return resp


LIVE_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — RC TV</title>
<style>html,body{{margin:0;height:100%;background:#000;overflow:hidden}}
video{{width:100vw;height:100vh;object-fit:contain;background:#000}}</style></head>
<body><video id="v" autoplay controls playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
var src = "{src}";
var v = document.getElementById("v");
function direct() {{ v.src = src; }}
if (src.indexOf(".m3u8") !== -1 && typeof Hls !== "undefined" && Hls.isSupported()) {{
  var h = new Hls({{ maxBufferLength: 30, liveSyncDurationCount: 3 }});
  h.loadSource(src); h.attachMedia(v);
  h.on(Hls.Events.ERROR, function (e, d) {{
    if (d.fatal) {{ h.destroy(); direct(); }}
  }});
}} else {{ direct(); }}
</script></body></html>"""


async def live_page(request):
    ch = _live_ch(request.match_info["id"])
    if not ch:
        raise web.HTTPNotFound()
    qs = "?b=1" if request.query.get("b") else ""
    is_m3u = ".m3u8" in (ch.get("url") or "") or ".m3u8" in (ch.get("backup") or "")
    src = f"/m3u/{request.match_info['id']}{qs}" if is_m3u else f"/file/{request.match_info['id']}{qs}"
    return web.Response(text=LIVE_PAGE.format(name=ch["name"], src=src),
                        content_type="text/html")


# ================= Live TV (TV app wale 3 servers — 2000+ channels) =================
UA_LIVE = ("Mozilla/5.0 (Linux; Android 9; Mi TV) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LIVE_SERVERS = [
    ("JioStar", ["https://lemajekaat.streamxlive.workers.dev/",
                 "https://endless.livenoww.workers.dev/",
                 "https://api.npoint.io/5122554ee81206923fa7"]),
    ("+PlusTV", ["https://api.npoint.io/9db02feba76eb297fc65"]),
    ("InPlex+", ["https://api.npoint.io/6dd4e6633c5c07b76d47"]),
]

_CAT_RE = [
    (r"\b(SPORTS?|CRICKET|FOOTBALL|ESPN|STARZPLAY|TENNIS|HOCKEY|KABADDI|TEN\s?SPORTS?)\b", "Sports"),
    (r"\b(NEWS|AAJ TAK|NDTV|CNN|BBC|REPUBLIC|ABP|INDIA TV|TIMES NOW|NEWS18)\b", "News"),
    (r"\b(KIDS?|CARTOON|DISNEY|POGO|NICK|NICKELODEON|CHUTTI|SONY YAY|KUSHI|KOCHU|CHINTU|BABYTV)\b", "Kids"),
    (r"\b(MOVIES?|CINEMA|MAX|HBO|PICTURES|PIX|MINIPLEX|STAR GOLD|GOLDMINES)\b", "Movies"),
    (r"\b(MUSIC|MTV|SANGEET|9XM|B4U)\b", "Music"),
]


def _live_map(o):
    name = (o.get("name") or o.get("channel_name") or "").strip()
    if not name or name.lower() == "null":
        return None
    url = ""
    for f in ("mpd_url", "m3u8_url", "url", "channel_url", "link", "stream_url"):
        v = (o.get(f) or "").strip()
        if v and v.lower() != "null":
            url = v
            break
    if not url:
        return None
    hdrs = {}
    if "|" in url:  # url|Cookie=..|User-Agent=..
        parts = url.split("|")
        url = parts[0].strip()
        for p in parts[1:]:
            i = p.find("=")
            if i > 0:
                hdrs[p[:i].strip().lower()] = p[i + 1:].strip()
    def gv(*fs):
        for f in fs:
            v = (o.get(f) or "").strip()
            if v and v.lower() != "null":
                return v
        return ""
    ck = gv("cookie") or ((o.get("headers") or {}).get("cookie")
                          if isinstance(o.get("headers"), dict) else "")
    if ck:
        hdrs["Cookie"] = ck
    for f in ("referer", "referrer"):
        v = gv(f)
        if v:
            hdrs["Referer"] = v
    v = gv("origin")
    if v:
        hdrs["Origin"] = v
    hd = o.get("headers")
    if isinstance(hd, dict):
        for k, v in hd.items():
            if isinstance(v, str) and v and v.lower() != "null":
                hdrs[str(k).strip().lower()] = v
    cat = ""
    for f in ("group", "category", "cat", "channel_genre", "genre", "type", "language"):
        v = (o.get(f) or "").strip()
        if v and v.lower() != "null":
            cat = v
            break
    if not cat:
        for rx, c in _CAT_RE:
            if re.search(rx, name, re.I):
                cat = c
                break
        cat = cat or "Entertainment"
    lic = gv("all_keys", "allKeys", "allkeys")
    if not lic:
        lic = gv("license_url", "licenseUrl", "drmLicense", "drm_license")
    if not lic:
        kid = gv("keyId", "kid", "key_id")
        key = gv("key", "KID", "drm_key")
        if kid and key:
            lic = kid + ":" + key
    return {"name": name, "url": url,
            "backup": gv("url_backup"), "logo": gv("logo", "channel_logo"),
            "cat": cat.title()[:20], "hdrs": hdrs, "drm": bool(lic)}


def _live_score(c):
    s = 2 if c["url"] else 0
    s += 2 if ".m3u8" in c["url"] else 0
    s += 1 if c["backup"] else 0
    s += 1 if c["hdrs"] else 0
    return s


def _live_parse(text):
    try:
        d = json.loads(text)
    except Exception:
        return []
    arr = d if isinstance(d, list) else None
    if arr is None and isinstance(d, dict):
        arr = d.get("channels") or d.get("data")
    if not isinstance(arr, list):
        return []
    out = []
    for o in arr:
        if isinstance(o, dict):
            c = _live_map(o)
            if c:
                out.append(c)
    return out


def _live_fetch_server(si):
    name, sources = LIVE_SERVERS[si]
    raw = []
    cf = HOME / f"live_cache_{si}.json"
    for s in sources:
        try:
            req = urllib.request.Request(s, headers={"User-Agent": UA_LIVE})
            text = urllib.request.urlopen(req, timeout=14).read().decode(errors="ignore")
            got = _live_parse(text)
            if got:
                raw.extend(got)
                try:
                    cf.write_text(json.dumps(got[:3000], ensure_ascii=False))
                except Exception:
                    pass
        except Exception as e:
            log(f"live[{name}] {s} fail: {type(e).__name__}")
    if not raw and cf.exists():
        try:
            raw = json.loads(cf.read_text())
            log(f"live[{name}] cache se aaya ({len(raw)})")
        except Exception:
            pass
    by = {}
    for c in sorted(raw, key=_live_score, reverse=True):
        by.setdefault(c["name"].lower().strip(), c)
    chans = list(by.values())
    chans.sort(key=lambda c: c["name"].lower())
    return chans


class LiveWin:
    """Live TV: server chips + category chips + search + logo grid. Sab app ke andar."""

    def __init__(self, master):
        self.win = tk.Toplevel(master)
        self.win.title("RC TV — Live TV")
        self.win.configure(bg=BG)
        try:
            self.win.state("zoomed")
        except Exception:
            self.win.geometry("1020x680")
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.chans = []
        self.server = 0
        self.cat = "All"
        self.logos = {}
        self.sig = None

        top = tk.Frame(self.win, bg=BG)
        top.pack(fill="x", padx=16, pady=(10, 4))
        tk.Label(top, text="📺 Live TV", bg=BG, fg=ACC, font=FONT_H).pack(side="left")
        self.sub = tk.Label(top, text="", bg=BG, fg=SUB, font=FONT_S)
        self.sub.pack(side="left", padx=14)
        self.q = tk.StringVar()
        e = tk.Entry(top, textvariable=self.q, bg="#14141A", fg=TXT, insertbackground=TXT,
                     relief="flat", width=24, font=FONT_S)
        e.pack(side="right", ipady=4)
        e.bind("<KeyRelease>", lambda ev: self._invalidate())

        self.chips = tk.Frame(self.win, bg=BG)
        self.chips.pack(fill="x", padx=16)
        self._chip("All", lambda: self._setcat("All")).pack(side="left", padx=(0, 6))
        self.wrap = tk.Frame(self.win, bg=BG)
        self.wrap.pack(fill="both", expand=True, padx=16, pady=8)
        self.canvas = tk.Canvas(self.wrap, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(self.wrap, orient="vertical", command=self.canvas.yview,
                          bg=BG, troughcolor="#14141A", width=10)
        self.gridfr = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((0, 0), window=self.gridfr, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.gridfr.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        self.cols = 5
        self.cards = {}
        self.load()
        self.win.after(500, self._tick)

    def _chip(self, text, cmd=None):
        ch = tk.Label(self.chips, text=text, bg="#14141A", fg=SUB, font=FONT_S,
                      padx=12, pady=5, cursor="hand2")
        if cmd:
            ch.bind("<Button-1>", lambda e: cmd())
        return ch

    def _invalidate(self):
        self.sig = None

    def load(self):
        self.sub.configure(text="channels load ho rahi hain…")
        self.pool.submit(self._load_work)

    def _load_work(self):
        chans = _live_fetch_server(self.server)
        def done():
            global LIVE_CHANS
            for i, c in enumerate(chans):
                c["_i"] = i
            LIVE_CHANS = chans
            self.chans = chans
            self.sub.configure(text=f"{len(chans)} channels")
            self._cats()
            self.sig = None
        try:
            self.win.after(0, done)
        except Exception:
            pass

    def _cats(self):
        for w in self.chips.winfo_children():
            if w.cget("text") != "All":
                w.destroy()
        cats = {}
        for c in self.chans:
            cats[c["cat"]] = cats.get(c["cat"], 0) + 1
        for name, n in sorted(cats.items(), key=lambda kv: -kv[1])[:9]:
            ch = self._chip(f"{name} ({n})", lambda nm=name: self._setcat(nm))
            ch.pack(side="left", padx=(0, 6))

    def _setcat(self, nm):
        self.cat = nm
        self.sig = None

    def _server_switch(self, si):
        self.server = si
        self.chans = []
        self.sig = None
        self.load()

    def visible(self):
        q = self.q.get().strip().lower()
        out = []
        for c in self.chans:
            if self.cat != "All" and c["cat"] != self.cat:
                continue
            if q and q not in c["name"].lower():
                continue
            out.append(c)
        return out

    def _tick(self):
        try:
            items = self.visible()
            sig = (len(items), self.cat, self.q.get().strip().lower(), len(self.chans))
            if sig != self.sig:
                self.sig = sig
                self._rebuild(items)
        except Exception:
            pass
        self.win.after(500, self._tick)

    def _rebuild(self, items):
        try:
            frac = self.canvas.yview()[0]
            for w in self.gridfr.winfo_children():
                w.destroy()
            self.cards = {}
            if not self.chans:
                tk.Label(self.gridfr, text="channels load ho rahi hain… (net chahiye, 15-20 sec)",
                         bg=BG, fg=SUB, font=FONT_S).grid(row=0, column=0, padx=10, pady=30)
                return
            if not items:
                tk.Label(self.gridfr, text="is filter me kuch nahi mila", bg=BG, fg=SUB,
                         font=FONT_S).grid(row=0, column=0, padx=10, pady=30)
                return
            for i, ch in enumerate(items[:400]):
                r, c = divmod(i, self.cols)
                self.gridfr.columnconfigure(c, weight=1)
                self.gridfr.rowconfigure(r, weight=0)
                f = self._card(ch)
                f.grid(row=r, column=c, padx=5, pady=5, sticky="n")
        except Exception:
            pass
        try:
            self.canvas.yview_moveto(frac)
        except Exception:
            pass

    def _card(self, ch):
        f = tk.Frame(self.gridfr, bg="#101014", bd=0, highlightthickness=1,
                     highlightbackground="#23232B", cursor="hand2")
        f.configure(width=170, height=118)
        f.pack_propagate(False)
        img_l = tk.Label(f, bg="#0A0A0C", width=23, height=4)
        img_l.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        nm = ch["name"][:26]
        tag = "  ⛔DRM" if ch["drm"] else ""
        tk.Label(f, text=nm + tag, bg="#101014", fg=(SUB if ch["drm"] else TXT),
                 font=("Segoe UI", 9, "bold"), wraplength=160).pack(fill="x", padx=6, pady=(2, 5))
        def go(e):
            if ch["drm"]:
                self._play_fail_drm(ch)
            else:
                url = f"{base_url()}/live/{ch.get('_i', 0)}"
                try:
                    os.startfile(url)
                except Exception:
                    webbrowser.open(url)
        def backup(e):
            if ch["backup"]:
                url = f"{base_url()}/live/{ch.get('_i', 0)}?b=1"
                try:
                    os.startfile(url)
                except Exception:
                    webbrowser.open(url)
        def link(e):
            self.win.clipboard_clear()
            self.win.clipboard_append(ch["url"])
        f.bind("<Button-1>", go)
        img_l.bind("<Button-1>", go)
        f.bind("<Double-Button-1>", backup)
        f.bind("<Button-3>", link)
        if ch["logo"]:
            self.pool.submit(self._logo_work, ch["logo"], id(f))
            self.cards[id(f)] = img_l
            f._img_key = id(f)
        return f

    def _play_fail_drm(self, ch):
        from tkinter import messagebox
        messagebox.showinfo("RC TV", f"\"{ch['name']}\" DRM-protected hai — PC player me nahi chalta.\n"
                                     "Baaki channels chalega (DRM wale 12% hi hain).", parent=self.win)

    def _logo_work(self, url, key):
        try:
            from PIL import Image, ImageTk
            import io
            req = urllib.request.Request(url, headers={"User-Agent": UA_LIVE})
            data = urllib.request.urlopen(req, timeout=10).read()
            im = Image.open(io.BytesIO(data)).convert("RGBA")
            im.thumbnail((110, 64))
            def done():
                lbl = self.cards.get(key)
                if lbl and lbl.winfo_exists():
                    ph = ImageTk.PhotoImage(im)
                    self.logos[key] = ph
                    lbl.configure(image=ph, width=110, height=64)
            self.win.after(0, done)
        except Exception:
            pass


def main():
    global LOOP
    if "--headless" in sys.argv or tk is None:
        _core_loop()  # bina GUI, server-only (debug / autostart)
        return

    def _run_core():
        global LOOP
        LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(LOOP)
        while True:
            try:
                LOOP.run_until_complete(main_async())
            except Exception as e:
                state["ready"] = False
                state["error"] = f"{type(e).__name__}: {e}"
                log("Error: " + state["error"])
                log(traceback.format_exc(limit=3))
            time.sleep(5)

    threading.Thread(target=_run_core, daemon=True).start()
    root = tk.Tk()
    app = App(root)

    # --autotest: 18s baad index+livewin khol ke log likhwa do (wine/CI verify)
    if "--autotest" in sys.argv:
        def _at():
            try:
                log(f"AUTOTEST: index {len(index)} items, /f/ aur /live routes ready")
                lw = LiveWin(root)
                log("AUTOTEST: livewin open ho gaya")
                root.after(40000, lambda: log("AUTOTEST-DONE"))
            except Exception as e:
                log("AUTOTEST-ERR: " + repr(e))
                root.after(1000, lambda: log("AUTOTEST-DONE"))
        root.after(18000, _at)

    root.mainloop()


if __name__ == "__main__":
    main()
