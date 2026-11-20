# Tel-tb — RC TV

Telegram storage ko TV + PC par chalne wala media app banata hai. Ek hi bot
(@tvrg_bot) aur ek hi storage group se sab kuch chalta hai.

## Andar kya hai

| Folder | Kya | Kahan chalega |
|---|---|---|
| `tv-app/` | RC TV Android app — Telegram library ka player, embedded Python server (`tgserver.py`) | Android TV / Mi TV Stick (APK releases me) |
| `windows/` | RC TV Windows app — wahi GUI list + play + self-heal, single exe | Windows 10/11 (`RC-TV-Windows.exe`) |
| `server/` | Bina GUI ka headless server — PC/VPS/Coolify par (TV app ko remote server jodne ke liye) | Koi bhi Linux/PC |

## Common HTTP API (teeno me same)

`/api/status` `/api/list` `/api/scan` `/t/{id}` (thumb) `/f/{id}` (Range streaming) `/tv.m3u`
+ Windows me extra: `/api/heal/{id}` (force fix), `/` (browser list page).

## Self-heal (door-DC auto fix)

Bot ka home DC India ke paas hai, par koi video kisi door DC (America/Europe, dc1/2/3)
par padi to wo atakti hai. App/bot **khud** us file ka data stream karke ghar wale DC pe
re-upload karta hai (bina disk, resume-safe), verify karta hai, purani copy delete —
file hamesha ke liye tez. Stream 2 baar fail ho ya scan par door-DC mile to khud shuru.

## Build

- **APK**: Actions → *Build TV APK* (secrets se config baked hota hai)
- **Windows exe**: Actions → *Build Windows App* → Release me `RC-TV-Windows.exe`
- Windows app source se chalana ho: `windows/README.md` dekho

## Limits

- Telegram bot **2GB+** file download nahi kar sakta (premium session chahiye) — wo files
  list me "2GB+" mark hoti hain, play nahi hongi.
- Bot storage group me admin hona chahiye. Admin DM commands: `/status` `/scan` `/id` `/help`.
