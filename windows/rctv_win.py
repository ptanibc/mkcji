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

VERSION = "1.0"

# ---------------- config ----------------

DEF = {"API_ID": 14116809, "API_HASH": "", "BOT_TOKEN": "", "GROUP_ID": -1003904890252,
       "ADMIN_ID": 1345802943, "PORT": 8090}


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


HOME = rctv_home()


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
    if not CFG["API_HASH"] or not CFG["BOT_TOKEN"]:
        raise RuntimeError("Config missing — .env me API_ID, API_HASH, BOT_TOKEN, GROUP_ID bharo "
                           "(server/.env.example dekho)")
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


# ---------------- GUI (tkinter — Python ke saath hi aata hai) ----------------

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:  # headless / GUI-less par bhi core chale
    tk = None

LOOP = None  # core thread ka asyncio loop


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


class GUI:
    def __init__(self, root):
        self.root = root
        root.title(f"RC TV — Windows v{VERSION}")
        root.geometry("860x560")
        root.minsize(680, 420)
        try:
            ip = icon_path()
            if ip:
                root.iconbitmap(ip)
        except Exception:
            pass

        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        self.b_scan = ttk.Button(top, text="Rescan", command=self.do_scan)
        self.b_scan.pack(side="left")
        self.b_play = ttk.Button(top, text="▶ Play", command=self.do_play)
        self.b_play.pack(side="left", padx=4)
        ttk.Button(top, text="Copy link", command=self.do_copy).pack(side="left")
        ttk.Button(top, text="Web UI", command=lambda: webbrowser.open(base_url() + "/")).pack(side="left", padx=4)
        ttk.Button(top, text="Fix now", command=self.do_fix).pack(side="left")
        self.auto = tk.BooleanVar(value=bool(SETTINGS.get("auto_heal")))
        ttk.Checkbutton(top, text="Auto-fix door DC", variable=self.auto,
                        command=self.toggle_auto).pack(side="left", padx=8)
        ttk.Button(top, text="About", command=self.about).pack(side="right")

        self.status = tk.StringVar(value="Shuru ho raha hai…")
        ttk.Label(root, textvariable=self.status, padding=(8, 2), wraplength=840).pack(fill="x")

        cols = ("title", "size", "kind", "dc", "status")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="browse")
        for c, w, a in (("title", 380, "w"), ("size", 80, "e"), ("kind", 60, "center"),
                        ("dc", 50, "center"), ("status", 150, "w")):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", lambda e: self.do_play())
        self.menu = tk.Menu(root, tearoff=0)
        for lbl, fn in (("▶ Play", self.do_play), ("Copy link", self.do_copy),
                        ("Fix now (re-upload)", self.do_fix)):
            self.menu.add_command(label=lbl, command=fn)
        self.tree.bind("<Button-3>", self.on_right)

        self.logbox = tk.Text(root, height=7, state="disabled", font=("Consolas", 9))
        self.logbox.pack(fill="x", padx=6, pady=(2, 6))
        globals()["_LOG_UI"] = self.ui_log

        self.last_sig = None
        self.poll()

    # ---- log ----
    def ui_log(self, line):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", line + "\n")
        self.logbox.see("end")
        if float(self.logbox.index("end-1c").split(".")[0]) > 400:
            self.logbox.delete("1.0", "50.0")
        self.logbox.configure(state="disabled")

    # ---- actions ----
    def _sel(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def do_scan(self):
        post(scan())

    def do_play(self):
        mid = self._sel()
        if mid:
            play_url(f"{base_url()}/f/{mid}")

    def do_copy(self):
        mid = self._sel()
        if mid:
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{base_url()}/f/{mid}")
            self.ui_log("Link copy ho gaya: " + f"{base_url()}/f/{mid}")

    def do_fix(self):
        mid = self._sel()
        if not mid:
            return

        async def _fix():
            m = await get_msg(mid)
            if m and m.document:
                _heal_tried.discard(mid)
                queue_heal(mid, m.document, force=True)
        post(_fix())

    def toggle_auto(self):
        SETTINGS["auto_heal"] = bool(self.auto.get())
        save_settings()
        log("Auto-fix door DC: " + ("ON" if SETTINGS["auto_heal"] else "OFF"))

    def on_right(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(e.x_root, e.y_root)

    def about(self):
        from tkinter import messagebox
        messagebox.showinfo("RC TV", f"RC TV — Windows v{VERSION}\n\n"
                            f"Bot: @{(CFG['BOT_TOKEN'].split(':')[1][:1] + '…') if CFG['BOT_TOKEN'] else '—'} · "
                            f"Storage: {state['title'] or CFG['GROUP_ID']}\n"
                            f"Data folder: {HOME}\n\n"
                            f"Isi PC: {base_url()}/\nTV / phone (same WiFi): http://{state['lan']}:{CFG['PORT']}/\n\n"
                            f"TV app me Settings > Server me upar wala TV/phone URL dalo.\n"
                            f"Telegram DM me bot ko /status ya /scan bhejo.")

    # ---- 1 sec poll: status + list refresh ----
    def row_status(self, it):
        mid = it["id"]
        hs = heal_state.get(mid)
        if hs:
            st = hs.get("status")
            if st == "queued":
                return "Fix queued…"
            if st == "running":
                return f"Fixing… {hs.get('pct', 0)}%"
            if st == "done":
                return "Fixed ✓"
            if st == "failed":
                return "Fix fail ⚠ " + (hs.get("err") or "")[:24]
        if it.get("big"):
            return "2GB+ (bot limit)"
        dc = it.get("dc") or 0
        if dc in FAR_DCS:
            return "Door DC — fix hoga" if SETTINGS.get("auto_heal") else "Door DC ⚠"
        return "OK"

    def poll(self):
        try:
            if state["error"]:
                self.status.set("⚠ " + state["error"])
            else:
                bits = []
                bits.append("● Online" if state["online"] else "○ Connect ho raha hai…")
                if state["dc"]:
                    bits.append(f"dc{state['dc']}")
                if state["title"]:
                    bits.append(state["title"])
                bits.append(f"{len(index)} files")
                if state["speed"]:
                    bits.append(f"{state['speed'] / 1e6:.1f} MB/s")
                if state["scanning"]:
                    bits.append(f"scanning {state['scanned']}…")
                hh = [v for v in heal_state.values() if v.get("status") in ("queued", "running")]
                if hh:
                    bits.append(f"🛠 {hh[0].get('name') or 'file'} {hh[0].get('pct', 0)}%")
                self.status.set(" · ".join(bits) + f"   |   TV/phone: http://{state['lan'] or lan_ip()}:{CFG['PORT']}/")

            sig = (state["version"], json.dumps({str(k): v.get("status") for k, v in heal_state.items()}, sort_keys=True))
            if sig != self.last_sig:
                self.last_sig = sig
                sel = self._sel()
                self.tree.delete(*self.tree.get_children())
                for it in sorted(index.values(), key=lambda x: x["id"], reverse=True):
                    self.tree.insert("", "end", iid=str(it["id"]), values=(
                        it["title"][:70], human(it.get("size")), it["kind"],
                        f"dc{it['dc']}" if it.get("dc") else "—", self.row_status(it)))
                if sel and self.tree.exists(str(sel)):
                    self.tree.selection_set(str(sel))
        except Exception:
            pass
        self.root.after(1000, self.poll)


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
    ttk.Style(root).theme_use("clam")
    GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
