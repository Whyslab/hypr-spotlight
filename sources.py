"""Откуда берутся результаты: программы, файлы, заметки, калькулятор, интернет.

Каждый источник отдаёт список Item. Окно ничего не знает о том, как
ищется, — только показывает Item и зовёт item.run(shift).
"""

import ast
import json
import operator
import os
import re
import shlex
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import Gdk, Gio, GLib

HOME = os.path.expanduser("~")
PINS_FILE = os.path.join(GLib.get_user_config_dir(), "spotlight", "pinned.json")

# Воркспейс, на котором всегда открыт браузер, и поисковик. Как в старой
# строке поиска (~/.local/share/websearch): Google — потому что он же выбран
# в самом браузере.
BROWSER_WORKSPACE = "1"
SEARCH_URL = "https://www.google.com/search?q={q}"

# Nerd Font, блок Font Awesome 4 — эти глифы в шрифте есть точно.
GLYPH = {
    "folder": "", "file": "", "code": "", "text": "",
    "pdf": "", "img": "", "audio": "", "video": "",
    "archive": "", "globe": "", "search": "", "calc": "",
    "note": "", "app": "",
}


@dataclass
class Item:
    kind: str                     # app / file / note / calc / web
    title: str
    sub: str = ""
    glyph: str = ""
    icon: Gio.Icon | None = None
    run: Callable[[bool], None] = field(default=lambda shift: None, repr=False)
    score: int = 0
    key: str = ""                 # для закрепления: app:<id> или file:<путь>; пусто — нельзя


def score(text: str, q: str, extra: str = "") -> int:
    """3 — начало названия, 2 — начало слова, 1 — где-то внутри."""
    t = text.lower()
    if t.startswith(q):
        return 3
    for sep in " -_./()":
        t = t.replace(sep, " ")
    if any(w.startswith(q) for w in t.split()):
        return 2
    if q in text.lower() or (len(q) >= 2 and q in extra):
        return 1
    return 0


def spawn(argv: list[str], cwd: str | None = None) -> None:
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    # Программа живёт долго: без этого каждый запуск оставлял бы зомби.
    # Ждёт сам Popen, в отдельном потоке: GLib.child_watch_add спорил бы с
    # питоном, кто заберёт процесс, и сыпал ECHILD в журнал.
    threading.Thread(target=proc.wait, daemon=True).start()


def complain(title: str, why: str) -> None:
    """Не открылось — сказать об этом уведомлением, а не молча в журнал."""
    try:
        spawn(["notify-send", "-a", "Поиск", f"Не открылось: {title}", why])
    except OSError:
        pass       # нет notify-send — сказать некому, но и падать незачем


def readable(text: str) -> str:
    """Имя файла не в UTF-8 хранится с «суррогатами» (так его можно открыть),
    но показать такое GTK не может — для подписи заменяем их на «�»."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# ---------- программы ----------

class Apps:
    def __init__(self):
        self.apps: list[Gio.AppInfo] = []
        self.reload()
        # Поставил или удалил программу — список обновится сам.
        self._monitor = Gio.AppInfoMonitor.get()
        self._monitor.connect("changed", lambda *_: self.reload())

    def reload(self):
        # Не isinstance(Gio.DesktopAppInfo): это имя в GLib устарело (теперь
        # GioUnix), и после обновления список программ молча опустел бы.
        self.apps = [a for a in Gio.AppInfo.get_all()
                     if a.should_show() and hasattr(a, "get_boolean")]

    def item(self, a: Gio.AppInfo, s: int = 0) -> Item:
        app_id = a.get_id()
        return Item("app", a.get_display_name(), subtitle(a), GLYPH["app"], a.get_icon(),
                    run=lambda shift, a=a: launch_app(a), score=s, key=f"app:{app_id}" if app_id else "")

    def search(self, q: str) -> list[Item]:
        out = []
        for a in self.apps:
            extra = " ".join([a.get_generic_name() or "", a.get_executable() or "",
                              *(a.get_keywords() or [])]).lower()
            s = score(a.get_display_name(), q, extra)
            if s:
                # Программу ищут чаще, чем файл с похожим именем: «fire» — это
                # Firefox, а не песня «Fire Man».
                out.append(self.item(a, s + 1))
        return out

    def by_id(self, desktop_id: str) -> Item | None:
        for a in self.apps:
            if a.get_id() == desktop_id:
                return self.item(a)
        return None


def subtitle(a: Gio.AppInfo) -> str:
    for text in (a.get_generic_name(), a.get_description()):
        if text and text.lower() != a.get_display_name().lower():
            return text
    return "Программа"


def launch_app(a: Gio.AppInfo) -> None:
    # gio не знает kitty как терминал и для Terminal=true молча ничего не
    # запускает — собираем kitty -e сами.
    if a.get_boolean("Terminal"):
        try:
            words = shlex.split(a.get_commandline() or "")
        except ValueError:
            complain(a.get_display_name(), "не разобрал строку запуска")
            return
        # Коды вроде %f, %U и --file=%f подставлять нечем — выкидываем; %% — это «%».
        cmd = [w.replace("%%", "%") for w in words if not re.search(r"%[a-zA-Z]", w)]
        if cmd:
            spawn(["kitty", "-e", *cmd], cwd=a.get_string("Path") or None)
        return
    try:
        a.launch([], Gdk.Display.get_default().get_app_launch_context())
    except GLib.Error as e:
        complain(a.get_display_name(), e.message)


# ---------- файлы и заметки ----------

# Что в домашней папке не ищем: кэши, зависимости, сборки.
SKIP_DIRS = ("node_modules", "__pycache__", "site-packages", "venv", "target", "dist", "build")

EXT_GLYPH = {
    **dict.fromkeys(("py", "js", "ts", "lua", "sh", "conf", "json", "toml", "yaml", "yml",
                     "css", "scss", "html", "c", "cpp", "rs", "go"), "code"),
    **dict.fromkeys(("md", "txt", "log", "csv"), "text"),
    "pdf": "pdf",
    **dict.fromkeys(("png", "jpg", "jpeg", "webp", "gif", "svg", "bmp"), "img"),
    **dict.fromkeys(("mp3", "flac", "opus", "ogg", "m4a", "wav"), "audio"),
    **dict.fromkeys(("mp4", "mkv", "webm", "mov", "avi"), "video"),
    **dict.fromkeys(("zip", "tar", "gz", "xz", "zst", "7z", "rar"), "archive"),
}
OBSIDIAN = os.path.join(HOME, "Documents", "obsidian") + "/"
FILES_SHOWN = 30     # больше в окно всё равно не влезает


def short(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


class Files:
    """Список всех файлов домашней папки в памяти.

    Не plocate: тот обновляется раз в сутки (сегодняшний файл не найти), а на
    коротком запросе вроде «fire» тонет в сотнях совпадений из /usr — 200 мс
    на букву. fd обходит домашнюю папку за ~0,07 с, поэтому список
    пересобирается при каждом открытии окна, в фоне. Скрытые папки и то, что
    в .gitignore, fd пропускает сам.
    """

    FD = ["fd", "--type", "f", "--type", "d", "--absolute-path",
          *(a for d in SKIP_DIRS for a in ("-E", d)), ".", HOME]

    def __init__(self):
        # (путь, имя в нижнем регистре, папка ли). Папку fd помечает «/» на
        # конце — так не нужен лишний stat на каждое совпадение.
        self.index: list[tuple[str, str, bool]] = []
        self._busy = False
        self.refresh()

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        try:
            # surrogateescape: одно имя не в UTF-8 (архив из Windows) иначе
            # роняло бы каждую пересборку, и поиск файлов умер бы навсегда.
            # Не "replace": тогда путь испорчен и файл не открыть.
            res = subprocess.run(self.FD, capture_output=True, text=True, errors="surrogateescape",
                                 timeout=10, check=False)
            index = []
            for line in res.stdout.splitlines():
                is_dir = line.endswith("/")
                path = line.rstrip("/")
                index.append((path, os.path.basename(path).lower(), is_dir))
            self.index = index
        except Exception:      # noqa: BLE001 — поток не должен умирать молча и навсегда
            pass
        finally:
            self._busy = False

    def search(self, q: str) -> list[Item]:
        if len(q) < 2:
            return []
        # Сначала только оценки, Item — для лучших: на двухбуквенном запросе
        # совпадений тысячи, а показываем пять.
        hits = [(score(name, q), path, is_dir) for path, name, is_dir in self.index if q in name]
        hits.sort(key=lambda h: (-h[0], len(h[1])))
        return [file_item(path, is_dir, s) for s, path, is_dir in hits[:FILES_SHOWN]]


def file_item(path: str, is_dir: bool, s: int = 0) -> Item:
    name = os.path.basename(path)
    note = path.startswith(OBSIDIAN) and name.endswith(".md")
    if is_dir:
        g = "folder"
    elif note:
        g = "note"
    else:
        g = EXT_GLYPH.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", "file")
    where = short(os.path.dirname(path))
    if note:
        folder = os.path.dirname(path)[len(OBSIDIAN):]     # пусто для корня хранилища
        where = f"Obsidian · {folder}" if folder else "Obsidian"
        name = name[:-3]
    return Item("note" if note else "file", readable(name), readable(where), GLYPH[g],
                run=lambda shift, p=path, n=note: open_path(p, shift, n), score=s, key="file:" + path)


def open_path(path: str, shift: bool, note: bool = False) -> None:
    if not os.path.exists(path):
        complain(os.path.basename(path), "файла больше нет")
        return
    if note and not shift:
        # Заметку — в Obsidian: для .md в системе нет программы по умолчанию.
        spawn(["xdg-open", "obsidian://open?path=" + urllib.parse.quote(path)])
        return
    if shift:
        # «Показать в папке»: открываем саму папку, где лежит файл.
        path = os.path.dirname(path)
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(),
                                           Gdk.Display.get_default().get_app_launch_context())
    except GLib.Error as e:
        complain(os.path.basename(path), e.message)


# ---------- закреплённое ----------

DEFAULT_PINS = ["app:firefox.desktop", "app:obsidian.desktop", "app:kitty.desktop"]


class Pins:
    """Что показывать при пустом запросе. Порядок — как закрепил."""

    def __init__(self, apps: Apps):
        self.apps = apps
        try:
            with open(PINS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError     # 5, null, {} — испорченный файл
            self.keys = [k for k in data if isinstance(k, str)]
        except (OSError, ValueError):
            # Испорченный файл не должен ронять программу при каждом запуске.
            self.keys = list(DEFAULT_PINS)

    def has(self, key: str) -> bool:
        return key in self.keys

    def toggle(self, key: str) -> None:
        if not key:
            return
        old = list(self.keys)
        if key in self.keys:
            self.keys.remove(key)
        else:
            self.keys.append(key)
        self._save(old)

    def move(self, key: str, d: int, shown: list[str]) -> None:
        """Сдвиг среди показанных: удалённые программы в списке остаются,
        но через них не перескакиваем — иначе шаг выглядел бы пустым."""
        if key not in shown or key not in self.keys:
            return
        j = shown.index(key) + d
        if not 0 <= j < len(shown):
            return
        old = list(self.keys)
        a, b = self.keys.index(key), self.keys.index(shown[j])
        self.keys[a], self.keys[b] = self.keys[b], self.keys[a]
        self._save(old)

    def items(self) -> list[Item]:
        out = []
        for key in self.keys:
            kind, _, rest = key.partition(":")
            if kind == "app":
                it = self.apps.by_id(rest)
            elif kind == "file" and os.path.exists(rest):
                it = file_item(rest, os.path.isdir(rest))
            else:
                it = None
            if it:          # удалённую программу или файл просто не показываем
                out.append(it)
        return out

    def _save(self, old: list[str]):
        tmp = PINS_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(PINS_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.keys, f, ensure_ascii=False, indent=1)
            os.replace(tmp, PINS_FILE)
        except (OSError, ValueError) as e:
            # Не записалось (диск полон, имя не в UTF-8) — на экране должно
            # остаться то же, что в файле.
            self.keys = old
            try:
                os.remove(tmp)
            except OSError:
                pass
            complain("закрепление", str(e))


# ---------- калькулятор ----------

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
DATE = re.compile(r"^\d{1,4}[-./]\d{1,2}[-./]\d{1,4}$")


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        a, b = _eval(node.left), _eval(node.right)
        # 9**9**9 повесил бы окно на минуты.
        if isinstance(node.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
            raise ValueError
        return OPS[type(node.op)](a, b)
    raise ValueError


def fmt_number(v) -> str:
    if isinstance(v, int) and len(str(abs(v))) > 20:
        v = float(v)          # длиннее 20 цифр — в научную запись
    if isinstance(v, float):
        if abs(v) >= 1e15 or (v != 0 and abs(v) < 1e-9):
            return f"{v:.10g}".replace(".", ",")
        v = round(v, 10)
        if v.is_integer():
            v = int(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return f"{v:,.10f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def calc(q: str) -> Item | None:
    s = q.replace(",", ".").replace("×", "*").replace("÷", "/").replace("^", "**").strip()
    if not s or DATE.match(s):      # 2026-10-17 — дата, а не 1999
        return None
    if not any(ch.isdigit() for ch in s) or not any(op in s for op in "+-*/%"):
        return None
    if any(ch not in "0123456789.+-*/%() " for ch in s):
        return None
    try:
        v = _eval(ast.parse(s, mode="eval").body)
        if isinstance(v, complex) or (isinstance(v, float) and (v != v or abs(v) == float("inf"))):
            return None       # (-8)**0.5 даёт комплексное — не наш случай
        text = fmt_number(v)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError):
        return None
    return Item("calc", text, q.strip() + " =", GLYPH["calc"],
                run=lambda shift, t=text: copy(t.replace(" ", "")))


def copy(text: str) -> None:
    # Буфер на Wayland живёт, пока жив владелец, — а программа живёт всегда.
    Gdk.Display.get_default().get_clipboard().set(text)


# ---------- интернет ----------

def looks_like_url(text: str) -> bool:
    """Адрес это или запрос — правило из старой строки поиска: нет пробелов,
    есть точка, после последней точки хотя бы две не-цифры."""
    if any(ch.isspace() for ch in text):
        return False
    low = text.lower()
    if low.startswith(("http://", "https://", "file://", "about:")):
        return True
    if low == "localhost" or low.startswith(("localhost:", "127.0.0.1")):
        return True
    if "." not in text:
        return False
    tail = text.rsplit(".", 1)[1]
    for cut in ("/", "?", ":", "#"):
        tail = tail.split(cut, 1)[0]
    return len(tail) >= 2 and not tail.isdigit()


def url_for(text: str) -> str:
    low = text.lower()
    if low.startswith(("http://", "https://", "file://", "about:")):
        return text
    if low == "localhost" or low.startswith(("localhost:", "127.0.0.1")):
        return "http://" + text
    return "https://" + text


def open_url(url: str) -> None:
    # Сначала воркспейс, потом ссылка — иначе браузер успевает утащить фокус.
    subprocess.run(["hyprctl", "dispatch", "workspace", BROWSER_WORKSPACE],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    spawn(["firefox", "--new-tab", url])


def web(q: str, is_file: bool = False) -> Item:
    """is_file: запрос совпал с именем файла — «spotlight.py» не адрес сайта."""
    q = q.strip()
    if looks_like_url(q) and not is_file:
        url = url_for(q)
        return Item("web", f"Открыть {q}", "в Firefox", GLYPH["globe"], run=lambda shift: open_url(url))
    url = SEARCH_URL.format(q=urllib.parse.quote_plus(q))
    return Item("web", f"Искать «{q}»", "Google", GLYPH["search"], run=lambda shift: open_url(url))


# ---------- всё вместе ----------

GROUPS = [("app", "Программы", 4), ("note", "Заметки", 3), ("file", "Файлы", 5)]


class Search:
    def __init__(self):
        self.apps = Apps()
        self.files = Files()
        self.pins = Pins(self.apps)

    def run(self, text: str) -> list[tuple[str, list[Item], bool]]:
        """Список групп: (заголовок, результаты, крупная ли строка)."""
        q = text.strip().lower()
        if not q:
            items = self.pins.items()
            return [("Закреплённые", items, False)] if items else []

        groups = []
        c = calc(text)
        found = self.apps.search(q) + self.files.search(q)
        # Программы выше файлов при равной оценке, короткие имена выше длинных.
        order = {"app": 0, "note": 1, "file": 2}
        found.sort(key=lambda i: (-i.score, order[i.kind], len(i.title)))
        exact_file = any(i.kind != "app" and i.title.lower() == q for i in found)
        if c:
            groups.append(("Калькулятор", [c], True))
        elif found and found[0].score >= 2:
            groups.append(("Лучшее совпадение", [found.pop(0)], True))
        for kind, title, limit in GROUPS:
            items = [i for i in found if i.kind == kind][:limit]
            if items:
                groups.append((title, items, False))
        groups.append(("Интернет", [web(text, exact_file)], False))
        return groups
