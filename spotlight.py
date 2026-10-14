#!/usr/bin/env python3
"""Поиск-остров: кнопка на HyprPanel раздувается в строку поиска.

Программа живёт в фоне всё время (exec-once с --daemon), а окно показывается
по действию toggle через D-Bus. Поэтому открытие мгновенное: питон и GTK уже
загружены. Кнопка на панели и SUPER+SHIFT+F зовут ./toggle.

Как устроено окно:

1. Это слой поверх всего (layer-shell, слой overlay) постоянного размера и
   прозрачный. Сам остров рисуется в Stage.do_snapshot: на каждом кадре
   меняется только картинка, разметка не пересчитывается — иначе T460 не
   успевал бы за частотой экрана.
2. В начале анимации остров стоит ровно на месте кнопки панели, её цветом и
   размером, с той же лупой. Кнопку не двигаем и не прячем: остров её просто
   накрывает. Где кнопка — задано в BUTTON и измерено по скриншоту.
3. Анимация — пружина, а не кривая с длительностью: если передумал на
   полпути, остров разворачивается с той же скоростью, без рывка.
4. Esc и Enter срабатывают на отпускании. Если закрыться на нажатии, зажатая
   клавиша уйдёт в окно под нами (Wayland передаёт список зажатых клавиш
   новому владельцу фокуса) — так Esc уже выкидывал YouTube из фуллскрина.
"""

import ctypes
import os

# GTK 4.24 сам сообщает композитору, где размывать фон (протокол
# ext-background-effect), и считает это по CSS-свойству backdrop-filter.
# Остров рисуется вручную, CSS о нём не знает — GTK шлёт «размывать нигде», и
# Hyprland слушается его, а не layerrule. Отключаем протокол для себя, и
# размытие задаёт правило в hyprland.conf.
os.environ["GDK_WAYLAND_DISABLE"] = "ext_background_effect_manager_v1"

# gtk4-layer-shell должна загрузиться раньше libwayland-client — иначе окно
# молча окажется обычным. Поэтому до импорта Gtk.
ctypes.CDLL("libgtk4-layer-shell.so.0")

import math  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cairo  # noqa: E402
from sources import Item, Search, complain  # noqa: E402

APP_ID = "dev.bogdan.Spotlight"
DEBUG = bool(os.environ.get("SPOTLIGHT_DEBUG"))

W = 600          # ширина острова
PAD = 56         # запас под тень сбоку и снизу
SURF_W = W + 2 * PAD
SURF_H = 600     # экран 1280×720 в логических пикселях
# Верх острова в открытом виде: под панелью (её кнопки кончаются на 28),
# с зазором — остров выплывает из кнопки вниз, а не накрывает панель.
TOP = 36
LIST_MAX = 380   # выше — список прокручивается

# Кнопка на панели в логических пикселях: dx — от центра экрана до центра
# кнопки, y — от верха экрана. Окно центрируется по экрану, поэтому от ширины
# монитора ничего не зависит.
BUTTON = {"dx": -61.67, "y": 6, "w": 28.67, "h": 22, "radius": 4}   # замер 07.10.2026
# Своя лупа рисуется по центру кнопки чуть иначе, чем у HyprPanel, —
# сдвиг подобран по скриншоту (см. README, «Если кнопка съехала»).
GHOST_NUDGE = (0.0, 0.0)

OPEN = (240, 23)    # жёсткость и трение: лёгкий перелёт при открытии
CLOSE = (420, 42)   # закрытие — быстрее и без перелёта

CSS = """
window.spotlight { background: transparent; }
.content, .ghost { font-family: "JetBrainsMono Nerd Font"; color: #ffffff; }
.ghost { font-size: 10px; }
.head { padding: 14px 20px 12px 20px; }
.lens { color: #8a8a8a; font-size: 15px; }
text.q { font-size: 17px; font-weight: 500; color: #ffffff; caret-color: #ffffff;
         background: transparent; min-height: 26px; }
text.q placeholder { color: #575757; }
.key { font-size: 10px; color: #777777; border: 1px solid #353535; border-radius: 5px;
       padding: 1px 6px; }
.sep { min-height: 1px; background: rgba(255,255,255,0.08); margin: 0 14px; }
.list { padding: 2px 8px 10px 8px; }
.gt { font-size: 11px; letter-spacing: 1px; color: #6a6a6a; padding: 12px 10px 6px 10px; }
.row { min-height: 34px; padding: 0 10px; color: #ffffff; font-size: 13px;
       transition: color 120ms; }
.row .ico { min-width: 24px; min-height: 24px; border-radius: 6px;
            background: rgba(255,255,255,0.07); font-size: 12px; transition: background 120ms; }
.row .d { color: #767676; font-size: 11px; transition: color 120ms; }
.row .ret { opacity: 0; font-size: 11px; }
.row .pin { color: #6a6a6a; font-size: 10px; }
.row.sel .pin { color: #555555; }
.row.sel { color: #000000; }
.row.sel .d { color: #555555; }
.row.sel .ico { background: rgba(0,0,0,0.08); }
.row.sel .ret { opacity: 0.55; }
.row.top { min-height: 56px; }
.row.top .ico { min-width: 38px; min-height: 38px; border-radius: 10px; font-size: 18px; }
.row.top .t { font-size: 14px; font-weight: 600; }
.row.calc .t { font-size: 22px; font-weight: 700; }
.foot { padding: 9px 20px 11px 20px; font-size: 10px; color: #656565;
        border-top: 1px solid rgba(255,255,255,0.08); }
.foot .b { color: #a8a8a8; }
"""


def lerp(a, b, t):
    return a + (b - a) * t


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def rgba(r, g, b, a):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def point(x, y):
    return Graphene.Point().init(x, y)


def rounded(x, y, w, h, r):
    rr = Gsk.RoundedRect()
    rr.init_from_rect(Graphene.Rect().init(x, y, w, h), r)
    return rr


class Spring:
    def __init__(self, k, c):
        self.x = self.v = self.to = 0.0
        self.k, self.c = k, c

    def set(self, k, c):
        self.k, self.c = k, c

    def step(self, dt):
        f = -self.k * (self.x - self.to) - self.c * self.v
        self.v += f * dt
        self.x += self.v * dt

    def done(self, eps):
        return abs(self.x - self.to) < eps and abs(self.v) < eps * 4

    def snap(self):
        self.x, self.v = self.to, 0.0


class Animator:
    """Крутит пружины от кадра к кадру, пока они не успокоятся."""

    def __init__(self, widget, springs, eps, on_done=None):
        self.widget, self.springs, self.eps, self.on_done = widget, springs, eps, on_done
        self.tick_id = 0
        self.last = None
        self.frames: list[float] = []

    def kick(self):
        if not self.tick_id:
            self.last = None
            self.frames = []
            self.tick_id = self.widget.add_tick_callback(self._tick)

    def _tick(self, _widget, clock):
        t = clock.get_frame_time() / 1e6
        dt = 1 / 60 if self.last is None else min(t - self.last, 1 / 30)
        if self.last is not None:
            self.frames.append(t - self.last)
        self.last = t
        n = max(1, math.ceil(dt * 240))
        for _ in range(n):
            for s in self.springs:
                s.step(dt / n)
        done = all(s.done(e) for s, e in zip(self.springs, self.eps, strict=True))
        if done:
            for s in self.springs:
                s.snap()
        self.widget.queue_draw()
        if not done:
            return GLib.SOURCE_CONTINUE
        self.tick_id = 0
        if DEBUG and self.frames:
            worst = max(self.frames) * 1000
            avg = sum(self.frames) / len(self.frames) * 1000
            print(f"анимация: {len(self.frames)} кадров, в среднем {avg:.1f} мс, худший {worst:.1f} мс",
                  file=sys.stderr, flush=True)
            print("  кадры:", " ".join(f"{f * 1000:.0f}" for f in self.frames), file=sys.stderr, flush=True)
            snaps = getattr(self.widget, "snap_ms", [])
            if snaps:
                print("  рисование:", " ".join(f"{x:.1f}" for x in snaps), file=sys.stderr, flush=True)
                snaps.clear()
        if self.on_done:
            self.on_done()
        return GLib.SOURCE_REMOVE


class Stage(Gtk.Widget):
    """Весь слой: рисует остров и раскладывает в нём содержимое."""

    __gtype_name__ = "SpotlightStage"

    def __init__(self, content: Gtk.Widget, on_closed):
        super().__init__()
        self.content = content
        self.ghost = Gtk.Label(label="")
        self.ghost.add_css_class("ghost")
        self.ghost.set_parent(self)
        content.set_parent(self)
        self.p = Spring(*OPEN)        # 0 — кнопка, 1 — остров
        self.h = Spring(420, 38)      # высота острова, пиксели
        self.anim = Animator(self, [self.p, self.h], [8e-4, 0.4], self._done)
        self.on_closed = on_closed
        self.snap_ms: list[float] = []
        self.btn = (SURF_W / 2 + BUTTON["dx"] - BUTTON["w"] / 2, BUTTON["y"], BUTTON["w"], BUTTON["h"])

    def do_measure(self, orientation, _for_size):
        n = SURF_W if orientation == Gtk.Orientation.HORIZONTAL else SURF_H
        return n, n, -1, -1

    def do_size_allocate(self, width, height, _baseline):
        _, nat, _, _ = self.content.measure(Gtk.Orientation.VERTICAL, W)
        nat = min(nat, height - TOP - PAD)
        self.content.allocate(W, nat, -1, Gsk.Transform.new().translate(point((width - W) / 2, TOP)))
        bx, by, bw, bh = self.btn
        self.ghost.allocate(bw, bh, -1, Gsk.Transform.new().translate(
            point(bx + GHOST_NUDGE[0], by + GHOST_NUDGE[1])))
        if nat != self.h.to:
            self.h.to = nat
            if self.p.x < 0.01:
                self.h.snap()
            self.anim.kick()

    def island(self):
        """Где остров сейчас: x, y, ширина, высота, радиус."""
        p, pc = self.p.x, clamp(self.p.x)
        bx, by, bw, bh = self.btn
        cx = lerp(bx + bw / 2, self.get_width() / 2, p)
        w = lerp(bw, W, p)
        return cx - w / 2, lerp(by, TOP, p), w, lerp(bh, self.h.x, p), lerp(BUTTON["radius"], 18, pc)

    def do_snapshot(self, s):
        t0 = time.perf_counter()
        self._snapshot(s)
        if DEBUG:
            self.snap_ms.append((time.perf_counter() - t0) * 1000)

    def _snapshot(self, s):
        p, pc = self.p.x, clamp(self.p.x)
        x, y, w, h, r = self.island()
        rr = rounded(x, y, w, h, r)
        if pc > 0:
            s.append_outset_shadow(rr, rgba(0, 0, 0, 0.55 * pc), 0, 16, -4, 36)
        s.push_rounded_clip(rr)
        # Цвет кнопки панели (#1A1A1A, непрозрачность 95 %) → тёмное стекло.
        g = lerp(26, 16, pc) / 255
        s.append_color(rgba(g, g, g, lerp(0.95, 0.8, pc)), Graphene.Rect().init(x, y, w, h))
        ghost = 1 - clamp(p * 4)
        if ghost > 0:
            s.push_opacity(ghost)
            self.snapshot_child(self.ghost, s)
            s.pop()
        shown = clamp((p - 0.3) / 0.5)
        if shown > 0:
            k = lerp(0.96, 1, pc)
            cx = self.get_width() / 2
            s.push_opacity(shown)
            s.save()
            s.translate(point(cx, TOP))
            s.scale(k, k)
            s.translate(point(-cx, -TOP))
            self.snapshot_child(self.content, s)
            s.restore()
            s.pop()
        s.pop()
        if pc > 0:
            edge = rgba(1, 1, 1, 0.09 * pc)
            s.append_border(rr, [1, 1, 1, 1], [edge, edge, edge, edge])

    def open(self):
        self.p.set(*OPEN)
        self.p.to = 1
        self.anim.kick()

    def close(self):
        self.p.set(*CLOSE)
        self.p.to = 0
        self.anim.kick()

    def is_open(self):
        return self.p.to > 0

    def _done(self):
        if self.p.to == 0 and self.on_closed:
            self.on_closed()


class ResultList(Gtk.Box):
    """Список с белой плашкой выбора, которая переезжает между строками."""

    __gtype_name__ = "SpotlightResults"

    def __init__(self, on_activate, on_pin):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("list")
        self.on_activate = on_activate
        self.on_pin = on_pin
        self.is_pinned = lambda item: False
        # Выбор мышью — только когда мышь правда сдвинулась. Иначе курсор,
        # оставленный над островом, сам перехватывал выбор при каждой
        # пересборке списка, и Enter открывал не лучшее совпадение.
        self.pointer = None
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)
        self.rows: list[tuple[Gtk.Widget, Item]] = []
        self.sel = 0
        self.y = Spring(520, 36)
        self.hh = Spring(520, 36)
        self.anim = Animator(self, [self.y, self.hh], [0.3, 0.3])
        self.jump = True
        self.scroller: Gtk.ScrolledWindow | None = None

    def set_groups(self, groups, sel=0, keep=False):
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self.rows = []
        for title, items, big in groups:
            gt = Gtk.Label(label=title.upper(), xalign=0)
            gt.add_css_class("gt")
            self.append(gt)
            for item in items:
                row = self._row(item, big)
                self.append(row)
                self.rows.append((row, item))
        self.sel = min(sel, len(self.rows) - 1) if self.rows else 0
        # Новый запрос — плашка сразу на первой строке; закрепление и
        # перестановка — плашка едет за строкой.
        self.jump = not keep
        self._mark()

    def _row(self, item: Item, big: bool) -> Gtk.Widget:
        row = Gtk.Box(spacing=12 if big else 10)
        row.add_css_class("row")
        if big:
            row.add_css_class("top")
        if item.kind == "calc":
            row.add_css_class("calc")
        # Значок — сам Image или Label с min-width из CSS: оба рисуют себя по
        # центру своего места. Обёртка-Box с hexpand внутри растягивалась и
        # уводила значок к середине строки.
        if item.icon is not None:
            ico = Gtk.Image.new_from_gicon(item.icon)
            ico.set_pixel_size(26 if big else 18)
        else:
            ico = Gtk.Label(label=item.glyph)
        ico.add_css_class("ico")
        ico.set_valign(Gtk.Align.CENTER)
        row.append(ico)
        title = Gtk.Label(label=item.title, xalign=0, ellipsize=3)  # 3 = END
        title.add_css_class("t")
        sub = Gtk.Label(label=item.sub, xalign=0, ellipsize=3)
        sub.add_css_class("d")
        if big:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
            col.append(title)
            col.append(sub)
            row.append(col)
        else:
            title.set_hexpand(True)
            sub.set_xalign(1)
            sub.set_max_width_chars(36)
            row.append(title)
            row.append(sub)
        if self.is_pinned(item):
            pin = Gtk.Label(label="\uf08d")      # кнопка-булавка
            pin.add_css_class("pin")
            row.append(pin)
        ret = Gtk.Label(label="↵")
        ret.add_css_class("ret")
        row.append(ret)

        index = len(self.rows)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.on_activate(self.rows[index][1], False))
        row.add_controller(click)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("released", lambda *_: self.on_pin(index))
        row.add_controller(right)
        return row

    def _on_motion(self, _ctl, x, y):
        if self.pointer is not None and abs(self.pointer[0] - x) < 1 and abs(self.pointer[1] - y) < 1:
            return
        self.pointer = (x, y)
        for n, (row, _) in enumerate(self.rows):
            ok, b = row.compute_bounds(self)
            if ok and b.get_y() <= y < b.get_y() + b.get_height():
                if n != self.sel:
                    self.select(n)
                return

    def select(self, i):
        if not self.rows:
            return
        self.sel = i % len(self.rows)
        self._mark()

    def move(self, d):
        self.select(self.sel + d)

    def current(self) -> Item | None:
        return self.rows[self.sel][1] if self.rows else None

    def _mark(self):
        for n, (row, _) in enumerate(self.rows):
            if n == self.sel:
                row.add_css_class("sel")
            else:
                row.remove_css_class("sel")
        self.anim.kick()
        GLib.idle_add(self._scroll_into_view)

    def _scroll_into_view(self):
        if not self.rows or self.scroller is None:
            return GLib.SOURCE_REMOVE
        ok, b = self.rows[self.sel][0].compute_bounds(self)
        if ok:
            adj = self.scroller.get_vadjustment()
            top, bottom = b.get_y(), b.get_y() + b.get_height()
            if self.sel == 0:
                adj.set_value(0)
            elif top < adj.get_value():
                adj.set_value(top - 8)
            elif bottom > adj.get_value() + adj.get_page_size():
                adj.set_value(bottom - adj.get_page_size() + 8)
        return GLib.SOURCE_REMOVE

    def do_snapshot(self, s):
        if self.rows:
            ok, b = self.rows[self.sel][0].compute_bounds(self)
            if ok:
                # Цель плашки берём из настоящего положения строки на каждом
                # кадре: после пересборки списка строки раскладываются позже.
                self.y.to, self.hh.to = b.get_y(), b.get_height()
                if self.jump:
                    self.y.snap()
                    self.hh.snap()
                    self.jump = False
                if not (self.y.done(0.3) and self.hh.done(0.3)):
                    self.anim.kick()
                rr =rounded(b.get_x(), self.y.x, b.get_width(), self.hh.x, 9)
                s.push_rounded_clip(rr)
                s.append_color(rgba(0.95, 0.95, 0.95, 1),
                               Graphene.Rect().init(b.get_x(), self.y.x, b.get_width(), self.hh.x))
                s.pop()
        child = self.get_first_child()
        while child is not None:
            self.snapshot_child(child, s)
            child = child.get_next_sibling()


class Catcher(Gtk.Window):
    """Прозрачный слой на весь экран под островом: щелчок мимо острова —
    закрыть. Пока остров открыт, клавиатура только у него (EXCLUSIVE), и
    фокус больше не подскажет, что щёлкнули в другое окно. Полоса панели
    сверху пропускает щелчки: кнопка поиска на ней сама закроет остров."""

    def __init__(self, app, on_click):
        super().__init__(application=app)
        self.add_css_class("spotlight")
        self.set_decorated(False)
        LayerShell.init_for_window(self)
        # Другое имя, чтобы layerrule с размытием его не задел: размывать
        # весь экран на каждом кадре T460 не потянет.
        LayerShell.set_namespace(self, "spotlight-catcher")
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self, edge, True)
        LayerShell.set_exclusive_zone(self, -1)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
        self.set_hide_on_close(True)
        # На отпускании: иначе слой исчезнет между нажатием и отпусканием, и
        # отпускание без нажатия достанется окну под ним.
        click = Gtk.GestureClick(button=0)     # любая кнопка мыши
        click.connect("released", lambda *_: on_click())
        self.add_controller(click)
        self.connect("map", lambda *_: self.get_surface().set_input_region(
            cairo.Region(cairo.RectangleInt(0, TOP, 1 << 15, 1 << 15))))


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Поиск")
        self.add_css_class("spotlight")
        self.set_decorated(False)
        LayerShell.init_for_window(self)
        LayerShell.set_namespace(self, "spotlight")
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
        LayerShell.set_margin(self, LayerShell.Edge.TOP, 0)
        # -1: не отступать от панели, а встать в самый верх экрана, на кнопку.
        LayerShell.set_exclusive_zone(self, -1)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.ON_DEMAND)
        # Монитор пропал — композитор закрывает слой. По умолчанию окно бы
        # уничтожилось, а программа продолжала отвечать на toggle впустую.
        self.set_hide_on_close(True)
        self.connect("close-request", self._on_close_request)

        self.catcher = Catcher(app, self.close)
        self.search = Search()
        self.pending_key = None
        self.pending_shift = False
        self.had_focus = False

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("content")
        head = Gtk.Box(spacing=12)
        head.add_css_class("head")
        lens = Gtk.Label(label="")
        lens.add_css_class("lens")
        self.entry = Gtk.Text(hexpand=True)
        self.entry.add_css_class("q")
        self.entry.set_placeholder_text("Программы, файлы, заметки, интернет…")
        self.entry.connect("changed", lambda *_: self.refresh())
        esc = Gtk.Label(label="esc", valign=Gtk.Align.CENTER)
        esc.add_css_class("key")
        head.append(lens)
        head.append(self.entry)
        head.append(esc)
        content.append(head)
        sep = Gtk.Box()
        sep.add_css_class("sep")
        content.append(sep)

        self.results = ResultList(self.activate_item, self.toggle_pin)
        self.results.is_pinned = lambda item: self.search.pins.has(item.key)
        scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=LIST_MAX)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.results)
        self.results.scroller = scroller
        content.append(scroller)

        foot = Gtk.Box(spacing=16)
        foot.add_css_class("foot")
        for key, what in (("↑↓", "выбрать"), ("↵", "открыть"), ("⇧↵", "в папке"), ("⌃P", "закрепить")):
            box = Gtk.Box(spacing=5)
            k = Gtk.Label(label=key)
            k.add_css_class("b")
            box.append(k)
            box.append(Gtk.Label(label=what))
            foot.append(box)
        self.count = Gtk.Label(hexpand=True, xalign=1)
        foot.append(self.count)
        content.append(foot)

        self.stage = Stage(content, self._closed)
        self.set_child(self.stage)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_press)
        keys.connect("key-released", self.on_release)
        self.add_controller(keys)

        # Щелчок мимо острова (по прозрачной части слоя) — закрыть.
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_click)
        self.add_controller(click)

        self.connect("notify::is-active", self.on_active)
        self.input_active = True
        self.connect("map", lambda *_: self.set_input_region(self.input_active))

    # ---------- открыть / закрыть ----------

    def toggle(self):
        if self.stage.is_open():
            self.close()
        else:
            self.open()

    def open(self, text=""):
        # Открыли снова, пока остров ещё сжимался: клавиатуру close() уже
        # отпустил, а вернуть фокус уже показанному слою Hyprland не умеет —
        # только при показе. Прячем и показываем заново; пружина при этом
        # продолжает с того места, где была.
        if self.get_visible():
            self.set_visible(False)
        # Ловушка показывается раньше острова: в одном слое Hyprland кладёт
        # показанное позже сверху, и остров окажется над ней.
        self.catcher.set_visible(True)
        self.had_focus = False
        self.pending_key = None
        # Не ON_DEMAND: при нём Hyprland отдаёт клавиатуру окну под курсором,
        # стоит сдвинуть мышь, — и остров закрывался, будто щёлкнули мимо.
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.EXCLUSIVE)
        self.set_input_region(True)
        self.search.files.refresh()
        self.entry.set_text(text)
        self.entry.set_position(-1)
        self.refresh()
        self.set_visible(True)
        self.present()
        # Без выделения: иначе первая же буква сотрёт готовый запрос.
        self.entry.grab_focus_without_selecting()
        self.stage.open()

    def close(self):
        if self.stage.is_open():
            self.stage.close()
            # Пока остров сжимается, клавиатура и мышь уже не наши: иначе то,
            # что набрал сразу после Enter, ушло бы в пустоту.
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
            self.set_input_region(False)
            self.catcher.set_visible(False)

    def _on_close_request(self, *_):
        self.stage.p.to = 0
        self.stage.p.snap()
        self.catcher.set_visible(False)   # иначе невидимо ест щелчки по всему экрану
        return False      # hide_on_close спрячет, а не уничтожит

    def pose(self, p):
        self.set_visible(True)
        self.stage.p.to = p          # цель = сама точка: пружине некуда тянуть
        self.stage.p.x, self.stage.p.v = p, 0.0
        self.stage.h.snap()
        self.stage.queue_draw()

    def _closed(self):
        self.set_visible(False)

    def set_input_region(self, active=True):
        """Ловить мышь только под панелью, где бывает остров. Сама панель с
        кнопкой остаётся кликабельной: щелчок по кнопке закроет остров."""
        self.input_active = active
        surface = self.get_surface()
        if surface is None:
            return
        if active:
            x = int(SURF_W / 2 - W / 2)
            region = cairo.Region(cairo.RectangleInt(x, TOP, W, SURF_H - PAD - TOP))
        else:
            region = cairo.Region()
        surface.set_input_region(region)

    # ---------- ввод ----------

    def refresh(self, sel=0, keep=False):
        text = self.entry.get_text()
        t0 = time.perf_counter()
        groups = self.search.run(text)
        self.results.set_groups(groups, sel, keep)
        n = sum(len(items) for _, items, _ in groups)
        self.count.set_label(f"{n} {plural(n)}" if text.strip() else "")
        if DEBUG:
            print(f"поиск «{text}»: {(time.perf_counter() - t0) * 1000:.1f} мс", file=sys.stderr, flush=True)

    def on_press(self, _ctl, keyval, _code, state):
        name = Gdk.keyval_name(keyval)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if name in ("Escape", "Return", "KP_Enter"):
            self.pending_key = name
            # Shift запоминаем на нажатии: отпустил его раньше Enter — всё
            # равно «показать в папке».
            self.pending_shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            return True
        if ctrl and name in ("p", "P", "Cyrillic_ze", "Cyrillic_ZE"):   # Ctrl+P и в русской раскладке
            self.toggle_pin(self.results.sel)
            return True
        if ctrl and name in ("Up", "Down") and not self.entry.get_text().strip():
            self.move_pin(-1 if name == "Up" else 1)
            return True
        if name in ("Down", "Tab"):
            self.results.move(1)
            return True
        if name in ("Up", "ISO_Left_Tab"):
            self.results.move(-1)
            return True
        return False

    def on_release(self, _ctl, keyval, _code, _state):
        name = Gdk.keyval_name(keyval)
        # pending_key is None — проверка обязательна: у клавиши без имени
        # keyval_name тоже None, и «совпадение» открыло бы выбранное.
        if self.pending_key is None or name != self.pending_key:
            return
        self.pending_key = None
        if name == "Escape":
            self.close()
        else:
            item = self.results.current()
            if item:
                self.activate_item(item, self.pending_shift)

    # ---------- закрепление ----------

    def toggle_pin(self, index):
        if not 0 <= index < len(self.results.rows):
            return
        item = self.results.rows[index][1]
        if not item.key:
            return          # калькулятор и поиск в сети закрепить нельзя
        self.search.pins.toggle(item.key)
        self.refresh(sel=index, keep=True)

    def move_pin(self, d):
        item = self.results.current()
        if item is None or not self.search.pins.has(item.key):
            return
        shown = [it.key for _, it in self.results.rows]
        self.search.pins.move(item.key, d, shown)
        self.refresh(keep=True)
        # Выбор — на ту же строку, где бы она теперь ни оказалась.
        for n, (_, it) in enumerate(self.results.rows):
            if it.key == item.key:
                self.results.select(n)
                break

    def _cursor_to_end(self):
        n = len(self.entry.get_text())
        self.entry.select_region(n, n)
        return GLib.SOURCE_REMOVE

    def on_click(self, gesture, _n, x, y):
        ix, iy, iw, ih, _ = self.stage.island()
        if not (ix <= x <= ix + iw and iy <= y <= iy + ih):
            self.close()

    def on_active(self, *_):
        if DEBUG:
            print(f"фокус: {self.is_active()}", file=sys.stderr, flush=True)
        if self.is_active():
            self.had_focus = True
            # GTK выделяет весь текст, когда окно получает фокус, — первая же
            # буква стёрла бы запрос. Ставим курсор в конец — после того, как
            # GTK закончит со своим выделением.
            GLib.idle_add(self._cursor_to_end)
        elif self.had_focus and self.stage.is_open():
            # Фокус ушёл (щелчки мимо ловит Catcher, это запасной путь) —
            # прячемся, как Spotlight.
            self.close()

    def activate_item(self, item: Item, shift: bool):
        self.close()
        try:
            item.run(shift)
        except (GLib.Error, OSError) as e:
            complain(item.title, getattr(e, "message", None) or str(e))


def plural(n):
    if n % 10 == 1 and n % 100 != 11:
        return "результат"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "результата"
    return "результатов"


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None
        self.add_main_option("daemon", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "запуститься скрыто и ждать toggle", None)
        self.start_hidden = False

    def do_handle_local_options(self, options):
        if options.contains("daemon"):
            self.register(None)
            if self.get_is_remote():
                return 0      # уже работает — второй не нужен и окно не показываем
            self.start_hidden = True
        return -1

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # Дисплей уже открыт, протокол уже отключён — дальше переменная не
        # нужна. Не убрать — её унаследует всё, что запущено из поиска, и
        # GTK-программы потеряют свой фон с размытием.
        os.environ.pop("GDK_WAYLAND_DISABLE", None)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Window(self)
        toggle = Gio.SimpleAction.new("toggle", None)
        toggle.connect("activate", lambda *_: self.window.toggle())
        self.add_action(toggle)
        # Открыть с готовым запросом — для проверок без нажатия клавиш:
        # gdbus call … org.gtk.Actions.Activate query "[<'fire'>]" {}
        query = Gio.SimpleAction.new("query", GLib.VariantType.new("s"))
        query.connect("activate", lambda _a, v: self.window.open(v.get_string()))
        self.add_action(query)
        if DEBUG:
            # Застыть на доле p анимации (0 — кнопка, 1 — остров), для
            # скриншотов: pose 0.0 показывает, совпал ли остров с кнопкой.
            pose = Gio.SimpleAction.new("pose", GLib.VariantType.new("d"))
            pose.connect("activate", lambda _a, v: self.window.pose(v.get_double()))
            self.add_action(pose)
        self.hold()

    def do_activate(self):
        if self.start_hidden:
            self.start_hidden = False
            return
        self.window.toggle()


if __name__ == "__main__":
    GLib.set_prgname("spotlight")
    sys.exit(App().run(sys.argv))
