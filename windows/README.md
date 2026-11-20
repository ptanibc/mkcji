# RC TV — Windows App

PC/laptop ke liye RC TV: Telegram storage ki poori library ek app me — list, play,
aur **door-DC wali videos ka automatic self-heal** (bot khud file ko doosre DC se
ghar wale DC pe re-upload karke theek karta hai — TV app wala hi fix).

## Ek baar me chalu

1. `RC-TV-Windows.exe` download karo (GitHub Releases — ya build artifact).
2. Double-click karo. Bas. Config exe ke andar baked hai.
3. List khud bharti jayegi. Kisi video par **double-click** = play
   (VLC/mpv mile to wahan, warna browser me).

> Pehli baar Windows Defender Smartcard/SmartScreen poochh sakta hai:
> "More info" → "Run anyway". (App unsigned hai.)

## GUI

- **Rescan** — storage group dobara padho
- **▶ Play** — selected video player me
- **Copy link** — `http://127.0.0.1:8090/f/{id}` clipboard me
- **Web UI** — browser me list (wahan se bhi sab chalega)
- **Fix now** — selected video ko haath se heal queue me daalo
- **Auto-fix door DC** — ON rakhna: America/Europe DC (dc1/2/3) ki video
  khud-fix ho jayegi (bot usko re-upload karke ghar wale DC pe le aayega,
  purani copy delete). Progress list me "Fixing… 43%" dikhta hai.
- Status bar: online/dc/videos/speed + heal progress + TV/phone ka URL

## TV ke saath jodo (optional)

Yahi PC TV app ka "remote server" ban sakta hai:
TV app → Settings → Server URL → `http://<PC-ka-IP>:8090/`
(status bar me wahi URL likha rehta hai; dono same WiFi par hone chahiye).
Tab TV pe apna Python server nahi chalega, ye PC serve karega.

## Telegram DM commands

Bot (@tvrg_bot) ko DM bhejo (sirf admin ke liye):
- `/status` — kitni files, online haal, heal progress
- `/scan` — storage dobara padho
- `/id` — chat id batao

## Data kahan

`%APPDATA%\RCTV\` — session (`tvbot.session`), `index.json`, `heal.json`, `settings.json`.
`RCTV_HOME` env se badal sakte ho (portable USB use ke liye).

## Khud build karna (ya dobara)

GitHub Actions → **Build Windows App** → version_name bharti hui "Run workflow".
Exe Release me `win-vX.Y` tag ke saath aayega.

Source se chalana ho to: `pip install -r requirements.txt`, `.env.example` ko
`.env` bana ke values bharo, phir `python rctv_win.py`.

## Limits (Telegram ke rules)

- Bot **2GB+** ki file download nahi kar sakta — wo list me "2GB+ (bot limit)" dikhti hai.
- Heal ke waqt net band/app band hua to agle chalu hone par wahi se resume (heal.json).
