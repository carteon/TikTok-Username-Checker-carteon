import random
import re
import json
import time
import unicodedata
import requests
import os
import sys
import glob
import shutil
import itertools
import threading
from collections import deque
from datetime import datetime
from queue import Queue

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from colorama import Fore, Style, init

try:
    import winsound
except ImportError:      # nicht unter Windows
    winsound = None

init(autoreset=True)

# Das Programm speichert alles im Ordner, in dem diese Datei liegt
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Einstellungen (z.B. Sound an/aus) bleiben nach dem Beenden gespeichert
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {"sound": bool(data.get("sound", True))}
    except Exception:
        return {"sound": True}


def save_settings():
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f)
    except Exception:
        pass


SETTINGS = load_settings()

BANNER = r"""
  ____     _     ____   _____  _____   ___   _   _
 / ___|   / \   |  _ \ |_   _|| ____| / _ \ | \ | |
| |      / _ \  | |_) |  | |  |  _|  | | | ||  \| |
| |___  / ___ \ |  _ <   | |  | |___ | |_| || |\  |
 \____|/_/   \_\|_| \_\  |_|  |_____| \___/ |_| \_|
"""

# TikTok-Usernames: Buchstaben, Zahlen, Unterstrich und Punkt
CHARSETS = {
    "C": "bcdfghjklmnpqrstvwxyz",
    "V": "aeiou",
    "D": "0123456789",
    "L": "abcdefghijklmnopqrstuvwxyz",
    "Q": "abcdefghijklmnopqrstuvwxyz0123456789",
    "_": "_",
    ".": ".",
}

MIN_LEN = 2
MAX_LEN = 24
# a-z, 0-9, Unterstrich, Punkt; darf nicht mit einem Punkt enden
HANDLE_RE = re.compile(r"^[a-z0-9_.]*[a-z0-9_]$")

# Ergebniscodes
VALID, TAKEN, INVALID, UNKNOWN = 0, 1, 2, 3

OUTPUT_FILES = {
    VALID: "valid.txt",
    TAKEN: "taken.txt",
    INVALID: "invalid.txt",
    UNKNOWN: "unknown.txt",
}

DELAY = 0.4          # Grundpause nach jeder Pruefung (Sekunden)
RETRY_PASSES = 2     # wie oft unklare Namen am Ende automatisch nochmal geprueft werden


def parse_pattern(fmt):
    tokens = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "[":
            end = fmt.index("]", i)
            inner = fmt[i + 1 : end]
            if len(inner) == 1:
                tokens.append(("lit", inner))
            else:
                tokens.append(("custom", inner))
            i = end + 1
        else:
            ch = fmt[i].upper()
            if ch in CHARSETS:
                tokens.append(("key", ch))
            else:
                tokens.append(("lit", fmt[i]))
            i += 1
    return tokens


def resolve_token(token):
    kind, val = token
    if kind == "key":
        return random.choice(CHARSETS[val])
    if kind == "custom":
        return random.choice(val)
    return val


def gen_from_pattern(fmt):
    return "".join(resolve_token(t) for t in parse_pattern(fmt))


def make_examples(fmt, n=2):
    return ", ".join(gen_from_pattern(fmt) for _ in range(n))


def all_combinations(fmt):
    tokens = parse_pattern(fmt)
    pools = []
    for kind, val in tokens:
        if kind == "key":
            pools.append(list(CHARSETS[val]))
        elif kind == "custom":
            pools.append(list(val))
        else:
            pools.append([val])
    for combo in itertools.product(*pools):
        yield "".join(combo)


def combo_count(fmt):
    tokens = parse_pattern(fmt)
    total = 1
    for kind, val in tokens:
        if kind == "key":
            total *= len(CHARSETS[val])
        elif kind == "custom":
            total *= len(val)
    return total


BUILTIN = [
    ("CVCVC",   "CVCVC",   "Consonant-vowel pattern"),
    ("LL_LL",   "LL_LL",   "Letters underscore letters"),
    ("LLLDD",   "LLLDD",   "3 letters + 2 digits"),
    ("DDLLL",   "DDLLL",   "2 digits + 3 letters"),
    ("LLDLL",   "LLDLL",   "Letters-digit-letters"),
    ("QQQQQ",   "QQQQQ",   "5 alphanumeric chars"),
    ("CVDCV",   "CVDCV",   "Vowel-consonant with digit"),
]

print_lock = threading.Lock()
file_lock = threading.Lock()
beep_lock = threading.Lock()


def fmt_time(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def beep():
    """Kurzer Ton bei einem Treffer (laeuft im Hintergrund). Nur wenn der Sound an ist."""
    if not SETTINGS.get("sound", True):
        return

    def _beep():
        with beep_lock:
            try:
                if winsound:
                    winsound.Beep(1200, 150)
                    winsound.Beep(1700, 220)
                else:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
            except Exception:
                pass
    threading.Thread(target=_beep, daemon=True).start()


# ----------------------------------------------------------------------------
# Namen bereinigen und pruefen
# ----------------------------------------------------------------------------

def clean_name(raw):
    """Macht aus Eingaben wie '@Name' oder 'tiktok.com/@name' einen sauberen Username."""
    n = raw.strip().lower()
    n = n.replace("https://", "").replace("http://", "")
    for prefix in ("www.tiktok.com/", "tiktok.com/", "m.tiktok.com/"):
        if n.startswith(prefix):
            n = n[len(prefix):]
    if n.startswith("@"):
        n = n[1:]
    return n.split("?")[0].strip("/")


def is_valid_handle(name):
    return MIN_LEN <= len(name) <= MAX_LEN and bool(HANDLE_RE.match(name))


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NOT_FOUND_CODE = 10221   # TikTok-interner Code fuer "Profil nicht gefunden"


def parse_profile_page(name, text):
    """Liest die in der TikTok-Seite eingebetteten Daten.

    Gibt (Ergebnis, Info) zurueck. Ergebnis ist TAKEN, VALID oder UNKNOWN.
    """
    m = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        text, re.S)
    if not m:
        return UNKNOWN, "no profile data (bot check?)"
    try:
        data = json.loads(m.group(1))
        detail = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]
    except Exception:
        return UNKNOWN, "unreadable profile data"

    user = (detail.get("userInfo") or {}).get("user") or {}
    if str(user.get("uniqueId", "")).strip():
        return TAKEN, "profile exists"

    sc = detail.get("statusCode")
    msg = detail.get("statusMsg", "")
    tm = re.search(r'"shareMeta":\s*\{[^{}]*?"title":\s*"([^"]*)"', text)
    title = tm.group(1) if tm else ""
    info = f"statusCode={sc} statusMsg={msg!r} title={title!r}"
    if sc == NOT_FOUND_CODE:
        return VALID, info
    return UNKNOWN, info


def lookup_status(name):
    """Fragt das TikTok-Profil ab. Gibt (TAKEN/VALID/UNKNOWN, Info) zurueck."""
    url = f"https://www.tiktok.com/@{name}"
    last = "no answer"
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=False)
        except Exception as e:
            last = f"network error: {type(e).__name__}"
            time.sleep(1.5 * (attempt + 1))
            continue

        if r.status_code in (403, 429) or r.status_code >= 500:
            last = f"blocked or busy (HTTP {r.status_code})"
            time.sleep(3 * (attempt + 1))
            continue

        if r.status_code in (200, 404):
            return parse_profile_page(name, r.text)

        return UNKNOWN, f"unexpected HTTP {r.status_code}"

    return UNKNOWN, last


def check_username(username):
    """Prueft einen Namen. Gibt (sauberer Name, Ergebnis, Info) zurueck.

    Ein Treffer ('verfuegbar') wird zur Sicherheit ein zweites Mal geprueft.
    """
    name = clean_name(username)
    if not is_valid_handle(name):
        return name, INVALID, "violates TikTok rules"

    code, info = lookup_status(name)
    if code == VALID:
        time.sleep(1.5)
        code2, info2 = lookup_status(name)
        if code2 != VALID:
            return name, (TAKEN if code2 == TAKEN else UNKNOWN), "not confirmed: " + info2
    return name, code, info


def debug_name(raw):
    """Zeigt fuer einen Namen die rohe Antwort von TikTok (zur Fehlersuche)."""
    name = clean_name(raw)
    print(f"\n  Name: {name}")
    if not is_valid_handle(name):
        print("  -> violates TikTok rules (INVALID), no request sent.")
        return
    url = f"https://www.tiktok.com/@{name}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=False)
        print(f"\n  {url}")
        print(f"    status:        {r.status_code}")
        if r.headers.get("Location"):
            print(f"    redirect:      {r.headers.get('Location')}")
        print(f"    page size:     {len(r.text)} chars")
        has_json = "__UNIVERSAL_DATA_FOR_REHYDRATION__" in r.text
        print(f"    profile data:  {'found' if has_json else 'NOT found (bot check / blocked?)'}")
        code, info = parse_profile_page(name, r.text)
        print(f"    parsed:        {info}")
        m = re.search(r'"statusCode":\s*(\d+)', r.text)
        print(f"    statusCode:    {m.group(1) if m else 'n/a'}")
        print(f"    uniqueId seen: {'yes' if 'uniqueId' in r.text else 'no'}")
        print(f"    start of page: {' '.join(r.text[:150].split())}")
    except Exception as e:
        print(f"    error: {e}")
    labels = {VALID: "AVAILABLE", TAKEN: "TAKEN", INVALID: "INVALID", UNKNOWN: "UNKNOWN"}
    _, code, info = check_username(name)
    print(f"\n  FINAL RESULT: {labels[code]}   ({info})\n")


# ----------------------------------------------------------------------------
# Automatisch langsamer werden bei Blockaden
# ----------------------------------------------------------------------------

class Throttle:
    """Merkt sich die letzten Ergebnisse. Gibt es viele Fehler, wird pausiert und langsamer gearbeitet."""

    def __init__(self, base=DELAY):
        self.base = base
        self.delay = base
        self.window = deque(maxlen=20)
        self.pause_until = 0.0
        self.lock = threading.Lock()

    def wait(self):
        while True:
            left = self.pause_until - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 1.0))

    def record(self, bad):
        """Gibt eine Meldung zurueck, wenn sich das Tempo aendert."""
        with self.lock:
            self.window.append(1 if bad else 0)
            if len(self.window) < self.window.maxlen:
                return None
            ratio = sum(self.window) / len(self.window)
            if ratio >= 0.5:
                self.delay = min(max(self.delay * 2, 1.0), 20.0)
                pause = min(self.delay * 5, 60.0)
                self.pause_until = time.time() + pause
                self.window.clear()
                return (f"TikTok is blocking requests - pausing {pause:.0f}s, "
                        f"then continuing slower ({self.delay:.1f}s delay).")
            if ratio <= 0.05 and self.delay > self.base:
                self.delay = max(self.base, self.delay / 2)
                self.window.clear()
                return f"Looks fine again - speeding up ({self.delay:.1f}s delay)."
        return None


# ----------------------------------------------------------------------------
# Ein Lauf: Ordner, Zaehler, Anzeige
# ----------------------------------------------------------------------------

class Run:
    def __init__(self, folder, total, valid=0, taken=0, invalid=0):
        self.folder = folder
        self.total = total
        self.valid = valid
        self.taken = taken
        self.invalid = invalid
        self.unknown = []
        self.start = time.time()
        self.session_done = 0
        self.stop = threading.Event()
        self.throttle = Throttle()
        self.lock = threading.Lock()
        self.status_visible = False

    # --- Dateien ---
    def file(self, name):
        return os.path.join(self.folder, name)

    def append(self, filename, line):
        with file_lock:
            with open(self.file(filename), "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def write_all(self, filename, lines):
        with file_lock:
            with open(self.file(filename), "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")

    # --- Anzeige ---
    def status_text(self):
        done = self.valid + self.taken + self.invalid
        checked = done + len(self.unknown)
        pct = checked * 100 // max(1, self.total)
        elapsed = max(1e-6, time.time() - self.start)
        rate = self.session_done / elapsed
        remaining = max(0, self.total - checked)
        eta = fmt_time(remaining / rate) if rate > 0 and remaining > 0 else "-"
        text = (f"  {checked:,}/{self.total:,} ({pct}%) | available: {self.valid} | "
                f"taken: {self.taken:,} | unclear: {len(self.unknown)} | "
                f"{rate:.1f}/s | ETA {eta}")
        if self.throttle.delay > self.throttle.base + 0.01:
            text += f" | slow {self.throttle.delay:.1f}s"
        return text

    def _width(self):
        return max(40, shutil.get_terminal_size((110, 20)).columns - 1)

    def _draw(self):
        w = self._width()
        sys.stdout.write("\r" + Fore.CYAN + self.status_text()[:w].ljust(w) + Style.RESET_ALL)
        sys.stdout.flush()
        self.status_visible = True

    def draw_status(self):
        with print_lock:
            self._draw()

    def clear_status(self):
        with print_lock:
            if self.status_visible:
                sys.stdout.write("\r" + " " * self._width() + "\r")
                sys.stdout.flush()
                self.status_visible = False

    def print_above(self, message):
        """Schreibt eine Zeile ueber die Statuszeile."""
        with print_lock:
            if self.status_visible:
                sys.stdout.write("\r" + " " * self._width() + "\r")
            print(message)
            self._draw()


def display_loop(run, stop_evt):
    while not stop_evt.wait(0.5):
        run.draw_status()


def handle_result(run, name, code, info):
    if code == UNKNOWN:
        with run.lock:
            run.unknown.append(name)
        run.append("details.log", f"UNCLEAR  {name} | {info}")
        return

    with run.lock:
        if code == VALID:
            run.valid += 1
        elif code == TAKEN:
            run.taken += 1
        else:
            run.invalid += 1
        run.session_done += 1

    run.append(OUTPUT_FILES[code], name)

    if code == VALID:
        run.append("details.log", f"AVAILABLE {name} | {info}")
        run.print_above(Fore.GREEN + f"  [AVAILABLE] {name}" + Fore.YELLOW + "   <- check in app (could be a banned account)")
        beep()


def worker(q, run):
    while True:
        name = q.get()
        try:
            if name is None:
                return
            if run.stop.is_set():
                continue
            run.throttle.wait()
            try:
                clean, code, info = check_username(name)
            except Exception as e:
                clean, code, info = clean_name(name), UNKNOWN, f"error: {e}"
            handle_result(run, clean if clean else name, code, info)
            msg = run.throttle.record(code == UNKNOWN)
            if msg:
                run.print_above(Fore.YELLOW + "  " + msg)
            time.sleep(run.throttle.delay)
        finally:
            q.task_done()


def drain(q):
    try:
        while True:
            q.get_nowait()
            q.task_done()
    except Exception:
        pass


def process(run, names, threads):
    """Prueft die Namen. Gibt True zurueck, wenn der Lauf abgebrochen wurde (Strg+C)."""
    q = Queue(maxsize=threads * 4)
    workers = []
    for _ in range(threads):
        t = threading.Thread(target=worker, args=(q, run), daemon=True)
        t.start()
        workers.append(t)

    disp_stop = threading.Event()
    disp = threading.Thread(target=display_loop, args=(run, disp_stop), daemon=True)
    disp.start()

    try:
        for name in names:
            if run.stop.is_set():
                break
            q.put(name)
        q.join()
    except KeyboardInterrupt:
        run.stop.set()
        drain(q)
    finally:
        drain(q)
        for _ in workers:
            q.put(None)
        for t in workers:
            t.join(timeout=15)
        disp_stop.set()
        disp.join(timeout=2)
        run.clear_status()

    return run.stop.is_set()


def self_test():
    """Prueft kurz, ob TikTok gerade Antworten liefert."""
    print("Running quick self-test...")
    taken, info_t = lookup_status("tiktok")
    gibberish = "zq" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(18))
    free, info_f = lookup_status(gibberish)
    if taken == TAKEN and free == VALID:
        print(Fore.GREEN + "  Self-test OK.\n")
        return True
    print(Fore.YELLOW + "  WARNING: Self-test failed. Results may be unreliable!")
    print(Fore.YELLOW + f"  (known account -> {info_t}; random name -> {info_f})")
    print(Fore.YELLOW + "  TikTok may be blocking automatic requests right now.\n")
    return False


# ----------------------------------------------------------------------------
# Ordner pro Lauf, Fortsetzen nach Abbruch
# ----------------------------------------------------------------------------

def read_set(path):
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def read_list(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def write_meta(folder, meta):
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def read_meta(folder):
    try:
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def new_run_folder():
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    folder = os.path.join(BASE_DIR, f"Results_{stamp}")
    n = 2
    while os.path.exists(folder):
        folder = os.path.join(BASE_DIR, f"Results_{stamp}_{n}")
        n += 1
    os.makedirs(folder)
    return folder


def migrate_old_files(folder):
    """Runs from the previous version saved hits as candidates.txt - rename them back to valid.txt."""
    old = os.path.join(folder, "candidates.txt")
    new = os.path.join(folder, OUTPUT_FILES[VALID])
    if os.path.isfile(old) and not os.path.isfile(new):
        os.rename(old, new)


def find_unfinished_run():
    """Finds the newest unfinished run. Returns (folder, meta, done_count) or None."""
    candidates = []
    # "Ergebnisse_*" = folders from an earlier version, still supported for resuming
    for pattern in ("Results_*", "Ergebnisse_*"):
        for folder in glob.glob(os.path.join(BASE_DIR, pattern)):
            meta = read_meta(folder)
            if not meta or meta.get("finished") or meta.get("dismissed"):
                continue
            migrate_old_files(folder)
            candidates.append((meta.get("created", ""), folder, meta))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, folder, meta = candidates[0]
    done = sum(len(read_set(os.path.join(folder, OUTPUT_FILES[c])))
               for c in (VALID, TAKEN, INVALID))
    return folder, meta, done


def start_run(usernames, threads, folder=None):
    """Startet einen neuen Lauf (folder=None) oder setzt einen abgebrochenen fort."""
    resumed = folder is not None

    if resumed:
        migrate_old_files(folder)
        names_all = read_list(os.path.join(folder, "queue.txt"))
        done_valid = read_set(os.path.join(folder, OUTPUT_FILES[VALID]))
        done_taken = read_set(os.path.join(folder, OUTPUT_FILES[TAKEN]))
        done_invalid = read_set(os.path.join(folder, OUTPUT_FILES[INVALID]))
        done = done_valid | done_taken | done_invalid
        todo = [n for n in names_all if n not in done]
        meta = read_meta(folder) or {}
        run = Run(folder, len(names_all), len(done_valid), len(done_taken), len(done_invalid))
        print(f"\nResuming: {folder}")
        print(f"  {len(done):,} of {len(names_all):,} names already done, {len(todo):,} left.\n")
    else:
        names_all = [n for n in dict.fromkeys(clean_name(u) for u in usernames) if n]
        folder = new_run_folder()
        with open(os.path.join(folder, "queue.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(names_all) + "\n")
        for fname in OUTPUT_FILES.values():
            open(os.path.join(folder, fname), "a").close()
        meta = {"created": datetime.now().isoformat(timespec="seconds"),
                "total": len(names_all), "threads": threads,
                "finished": False, "dismissed": False}
        write_meta(folder, meta)
        todo = names_all
        run = Run(folder, len(names_all))
        print(f"\nResults folder: {folder}\n")

    if not self_test():
        ans = input("Self-test failed. Continue anyway? (y/n): ").strip().lower()
        if ans != "y":
            print("Cancelled. Your list is saved - start again later and choose 'y' to resume.")
            return

    print(f"Checking {len(todo):,} usernames with {threads} thread(s). Press Ctrl+C to pause (progress is saved).\n")
    print(Fore.YELLOW + "NOTE: 'AVAILABLE' = TikTok shows no public profile for this name.")
    print(Fore.YELLOW + "Why check it yourself: banned, deleted or recently changed accounts look exactly")
    print(Fore.YELLOW + "the same to TikTok's public pages, so a few hits may still be unavailable.")
    print(Fore.YELLOW + "Please verify each hit in the TikTok app (Edit profile > Username).\n")
    interrupted = process(run, todo, threads)

    # Unklare Namen am Ende automatisch nochmal pruefen
    for attempt in range(1, RETRY_PASSES + 1):
        if interrupted or not run.unknown:
            break
        retry = run.unknown
        run.unknown = []
        run.print_above(Fore.YELLOW + f"  Retrying {len(retry)} unclear names "
                                      f"(pass {attempt}/{RETRY_PASSES}) in 15 seconds...")
        try:
            time.sleep(15)
        except KeyboardInterrupt:
            run.unknown = retry
            interrupted = True
            break
        run.throttle.delay = max(run.throttle.delay, 1.0)
        interrupted = process(run, retry, min(threads, 2))

    # Ergebnisdateien abschliessen
    run.write_all(OUTPUT_FILES[UNKNOWN], sorted(set(run.unknown)))
    elapsed = fmt_time(time.time() - run.start)
    summary = [
        f"Checked this session: {run.session_done:,}",
        f"Available: {run.valid}",
        f"Taken:     {run.taken:,}",
        f"Invalid:   {run.invalid}",
        f"Unclear:   {len(run.unknown)}",
        f"Time:      {elapsed}",
    ]
    run.write_all("summary.txt", summary)

    print()
    if interrupted:
        print(Fore.YELLOW + "Stopped. Progress is saved.")
        print("Start the program again and choose 'y' to resume this run.")
    else:
        meta["finished"] = True
        write_meta(folder, meta)
        print(Fore.GREEN + "Done!")
    for line in summary:
        print("  " + line)
    print(f"\nResults folder: {folder}")
    print("  valid.txt / taken.txt / invalid.txt / unknown.txt / details.log")
    if run.valid:
        print(Fore.YELLOW + "\nREMINDER: 'AVAILABLE' means TikTok shows no public profile for the name.")
        print(Fore.YELLOW + "Banned, deleted or recently changed accounts look exactly the same, so a few")
        print(Fore.YELLOW + "hits may still be unavailable. Verify each name in valid.txt in the TikTok app")
        print(Fore.YELLOW + "(Edit profile > Username) before relying on it.")


def read_lines(path):
    """Liest eine TXT-Datei, egal ob UTF-8, UTF-16 (alter Editor) oder Windows-Kodierung."""
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            with open(path, encoding=enc) as f:
                return [line.strip() for line in f if line.strip()]
        except (UnicodeDecodeError, UnicodeError):
            continue
    return []


def load_from_file():
    """Fragt so lange nach dem Dateipfad, bis eine Datei gefunden wurde."""
    while True:
        path = input("File path (or 'b' to go back/quit): ").strip().strip('"').strip("'")
        if path.lower() == "b":
            sys.exit(0)
        if not path:
            continue
        # Nur Dateiname eingegeben? Dann auch im Programmordner suchen
        candidates = [path, os.path.join(BASE_DIR, path)]
        found = next((c for c in candidates if os.path.isfile(c)), None)
        if not found:
            print("  File not found. Tip: drag the .txt file into this window, or put it")
            print(f"  into the program folder ({BASE_DIR}) and type only its name.\n")
            continue
        try:
            return read_lines(found)
        except Exception as e:
            print(f"  Could not read file: {e}\n")


def ask_generation_mode(fmt):
    print("\nGeneration mode:")
    print("  1. Random sample (pick N names)")
    print("  2. All combinations (exhaustive)\n")
    mode = input("Mode: ").strip()

    if mode == "2":
        total = combo_count(fmt)
        print(f"\n  This pattern has {total:,} possible combinations.")
        confirm = input("  Proceed? (y/n): ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            sys.exit(0)
        usernames = list(all_combinations(fmt))
    else:
        count = int(input("\nHow many names? "))
        usernames = [gen_from_pattern(fmt) for _ in range(count)]

    return apply_ordering(usernames)


def apply_ordering(usernames):
    print("\nOrdering:")
    print("  1. Totally random")
    print("  2. Random, no duplicates")
    print("  3. Alphabetical order\n")
    order = input("Order: ").strip()

    if order == "2":
        usernames = list(dict.fromkeys(usernames))
        random.shuffle(usernames)
    elif order == "3":
        usernames = sorted(set(usernames))
    else:
        random.shuffle(usernames)

    return usernames


def ask_threads():
    print("\nThreads (1 or 2 is safest - the program slows down by itself if TikTok blocks):")
    try:
        n = int(input("  How many threads? [1-5]: ").strip())
        return max(1, min(n, 5))
    except ValueError:
        return 1


WORDLIST_DIR = os.path.join(BASE_DIR, "wordlists")

# Quellen fuer die Sprachlisten (werden beim ersten Gebrauch heruntergeladen)
LANG_URLS = {
    "french":  "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/fr/fr_50k.txt",
    "spanish": "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/es/es_50k.txt",
    "german":  "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/de/de_50k.txt",
}


def normalize_word(w):
    """Kleinbuchstaben, Akzente weg (e mit Akzent -> e, ae-Umlaut -> a, ss fuer scharfes s). Nur a-z bleibt."""
    w = w.strip().lower().replace("\u00df", "ss")
    w = unicodedata.normalize("NFKD", w)
    w = "".join(c for c in w if not unicodedata.combining(c))
    return w if (w.isascii() and w.isalpha() and len(w) >= MIN_LEN) else ""


def read_wordfile(path):
    """Liest eine Wortliste (ein Wort pro Zeile; Zeilen wie 'wort 123' sind ok)."""
    out, seen = [], set()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            with open(path, encoding=enc) as f:
                for line in f:
                    parts = line.split()
                    if not parts:
                        continue
                    w = normalize_word(parts[0])
                    if w and w not in seen:
                        seen.add(w)
                        out.append(w)
            return out
        except (UnicodeDecodeError, UnicodeError):
            out, seen = [], set()
    return out


def load_language(lang):
    """Laedt eine Sprache: erst aus dem Ordner wordlists, sonst Download beim ersten Mal."""
    os.makedirs(WORDLIST_DIR, exist_ok=True)
    path = os.path.join(WORDLIST_DIR, f"{lang}.txt")
    if os.path.isfile(path):
        return read_wordfile(path)
    url = LANG_URLS.get(lang)
    if not url:
        return []
    print(f"  Downloading {lang} word list (first time only)...")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        tmp = os.path.join(WORDLIST_DIR, f"{lang}.download")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(r.text)
        words = read_wordfile(tmp)
        os.remove(tmp)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(words) + "\n")
        return words
    except Exception as e:
        print(Fore.YELLOW + f"  Could not download {lang}: {e}")
        print(Fore.YELLOW + f"  Tip: put your own {lang}.txt (one word per line) into:")
        print(Fore.YELLOW + f"  {WORDLIST_DIR}")
        return []


def parse_length_range(text):
    """'4-6' -> (4, 6), '5' -> (5, 5), leer -> (MIN_LEN, MAX_LEN)."""
    text = text.strip()
    if not text:
        return MIN_LEN, MAX_LEN
    try:
        if "-" in text:
            lo, hi = text.split("-", 1)
            return max(MIN_LEN, int(lo)), min(MAX_LEN, int(hi))
        n = int(text)
        return n, n
    except ValueError:
        return MIN_LEN, MAX_LEN


def words_example():
    path = os.path.join(WORDLIST_DIR, "english.txt")
    try:
        ws = read_wordfile(path)
        return ", ".join(random.sample([w for w in ws if 4 <= len(w) <= 6], 2))
    except Exception:
        return "table, river"


def ask_word_mode():
    print("\nWord source:")
    print("  1. English (built-in dictionary, ~60,000 words)")
    print("  2. Mixed: English + French + Spanish + German")
    src = input("\nSource: ").strip()

    lo, hi = parse_length_range(input("Word length, e.g. 4-6 (Enter = any): "))

    raw = input("How many words? (number, or 'all'): ").strip().lower()
    want_all = raw == "all"
    try:
        count = None if want_all else int(raw)
    except ValueError:
        count = 1000

    def pick(words, n):
        words = [w for w in words if lo <= len(w) <= hi]
        if n is None or n >= len(words):
            return words
        return random.sample(words, n)

    if src == "2":
        langs = ["english", "french", "spanish", "german"]
        quota = None if count is None else max(1, count // len(langs))
        result = []
        for lang in langs:
            words = load_language(lang)
            if not words:
                print(Fore.YELLOW + f"  Skipping {lang} (no words available).")
                continue
            chosen = pick(words, quota)
            print(f"  {lang:<8} {len(chosen):>6} words")
            result.extend(chosen)
    else:
        result = pick(load_language("english"), count)
        print(f"  english  {len(result):>6} words")

    result = list(dict.fromkeys(result))   # Duplikate entfernen (z.B. gleiche Woerter in 2 Sprachen)
    if not result:
        print("No words found.")
        input("\nPress Enter to exit...")
        sys.exit(1)
    print(f"\n  Total: {len(result)} words.")
    return apply_ordering(result)


def ask_bigword_mode():
    """Option 9: the big English word list (400k+ words), with a letter-count range."""
    path = os.path.join(WORDLIST_DIR, "english_big.txt")
    if not os.path.isfile(path):
        print(Fore.YELLOW + f"\n  Word list not found: {path}")
        print(Fore.YELLOW + "  Put 'english_big.txt' (one word per line) into the wordlists folder.")
        input("\nPress Enter to exit...")
        sys.exit(1)

    print("\nBig English word list")
    words = read_wordfile(path)
    print(f"  Loaded {len(words):,} words.")

    raw = input("\nHow many letters? e.g. 4-10 (Enter = 4-10): ").strip()
    lo, hi = (4, 10) if not raw else parse_length_range(raw)
    pool = [w for w in words if lo <= len(w) <= hi]
    print(f"  {len(pool):,} words with {lo}-{hi} letters.")
    if not pool:
        input("\nNo words found. Press Enter to exit...")
        sys.exit(1)

    raw = input("\nHow many words? (number, or 'all'): ").strip().lower()
    if raw == "all":
        result = pool
    else:
        try:
            n = int(raw)
        except ValueError:
            n = 1000
        result = random.sample(pool, min(n, len(pool)))

    print(f"\n  Total: {len(result):,} words.")
    return apply_ordering(result)


def show_menu():
    print(BANNER)
    print("TikTok Username Checker")
    print("Credits to carteon on Github \u2726\ufe0e\n")
    print("Built-in patterns:\n")
    for i, (label, fmt, desc) in enumerate(BUILTIN, 1):
        print(f"  {i}. {label:<10} {desc:<30} e.g. {make_examples(fmt)}")
    print(f"  {len(BUILTIN) + 1}. {'WORDS':<10} {'Dictionary words (EN / mixed)':<30} e.g. {words_example()}")
    print(f"  {len(BUILTIN) + 2}. {'BIG WORDS':<10} {'Big English list (400k words)':<30} choose 4-10 letters")
    state = "ON" if SETTINGS.get("sound", True) else "OFF"
    print(f"  {len(BUILTIN) + 3}. {'SOUND':<10} {'Beep on available name':<30} currently: {state}")
    print()
    print("  c. Custom pattern  (e.g. [M][S][PQ]DD)")
    print("  f. Load from .txt file")
    print("  t. Test a single name (shows details)")
    print("  q. Quit\n")


def run():
    # Start ueber die Kommandozeile: python tiktok_checker.py 100 CVCVC
    if len(sys.argv) == 3:
        try:
            fmt = sys.argv[2]
            count = int(sys.argv[1])
            usernames = apply_ordering([gen_from_pattern(fmt) for _ in range(count)])
        except Exception as e:
            print(f"Bad args: {e}")
            sys.exit(1)
        start_run(usernames, ask_threads())
        return

    # Abgebrochenen Lauf fortsetzen?
    found = find_unfinished_run()
    if found:
        folder, meta, done = found
        print(BANNER)
        print(f"Unfinished run found: {os.path.basename(folder)}")
        print(f"  {done:,} of {int(meta.get('total', 0)):,} names are already done.\n")
        ans = input("Resume it? (y/n): ").strip().lower()
        if ans == "y":
            start_run(None, ask_threads(), folder)
            return
        meta["dismissed"] = True
        write_meta(folder, meta)
        print()

    while True:
        show_menu()
        choice = input("Choice: ").strip().lower()
        if choice == str(len(BUILTIN) + 3):          # Option 10: Sound an/aus
            SETTINGS["sound"] = not SETTINGS.get("sound", True)
            save_settings()
            if SETTINGS["sound"]:
                print(Fore.GREEN + "\n  Sound is now ON (you will hear a beep for every available name).")
                beep()
                time.sleep(0.6)
            else:
                print(Fore.YELLOW + "\n  Sound is now OFF.")
            print()
            continue
        break

    usernames = []

    if choice == "q":
        sys.exit(0)

    elif choice == "t":
        print("\nType a name to see exactly what TikTok answers (empty line = back).")
        while True:
            n = input("\nName: ").strip()
            if not n:
                sys.exit(0)
            debug_name(n)

    elif choice == "f":
        usernames = load_from_file()
        if not usernames:
            print("No usernames found in file.")
            input("\nPress Enter to exit...")
            sys.exit(1)
        print(f"\n  Loaded {len(usernames)} usernames.")
        usernames = apply_ordering(usernames)

    elif choice == str(len(BUILTIN) + 1):
        usernames = ask_word_mode()

    elif choice == str(len(BUILTIN) + 2):
        usernames = ask_bigword_mode()

    elif choice == "c":
        print("\nPattern key:")
        print("  C = consonant  V = vowel  D = digit  L = letter  Q = alphanumeric")
        print("  _ = underscore   . = period   (typed directly, e.g. LL_LL)")
        print("  [X]   = literal  (e.g. [M]  always outputs M)")
        print("  [XY\u2026] = custom table - picks one of the listed chars  (e.g. [PQ] outputs P or Q)")
        print(f"  TikTok usernames: {MIN_LEN}-{MAX_LEN} chars, a-z 0-9 _ and ., must not end with a period")
        fmt = input("\nPattern (e.g. [M][S][PQ]DD): ").strip()
        usernames = ask_generation_mode(fmt)

    elif choice.isdigit() and 1 <= int(choice) <= len(BUILTIN):
        _, fmt, _ = BUILTIN[int(choice) - 1]
        usernames = ask_generation_mode(fmt)

    else:
        print("Invalid choice.")
        sys.exit(1)

    threads = ask_threads()
    start_run(usernames, threads)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"\nError: {e}")
        input("\nPress Enter to exit...")
