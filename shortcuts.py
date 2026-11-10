#!/usr/bin/env python3
"""Shortcuts: administrador de atajos de teclado de Hyprland para Omarchy.

Lee todos los atajos ejecutando la config Lua real con un `hl` simulado
(scan.lua) y guarda los cambios del usuario en dos archivos propios:

  ~/.config/hypr/shortcuts.json   estado (fuente de verdad)
  ~/.config/hypr/shortcuts.lua    generado desde el JSON, cargado al final de hyprland.lua

Nunca toca /usr/share/omarchy ni reescribe bindings.lua.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.markosamo.Shortcuts"
REPO_URL = "https://github.com/markosAMO/fluid-shortcuts"
HOME = os.path.expanduser("~")
HYPR = os.path.join(HOME, ".config/hypr")
MAIN_CONFIG = os.path.join(HYPR, "hyprland.lua")
MANAGED_LUA = os.path.join(HYPR, "shortcuts.lua")
STATE_FILE = os.path.join(HYPR, "shortcuts.json")
SCAN_LUA = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scan.lua")
HOOK_MARKER = "-- fluid-shortcuts (remove with uninstall.sh)"
# Sólo carga si el archivo existe: borrar shortcuts.lua nunca rompe Hyprland.
HOOK_LINE = ('do local f = (os.getenv("HOME") or "") .. "/.config/hypr/shortcuts.lua"; '
             'local h = io.open(f) if h then h:close() dofile(f) end end')
OMARCHY_PATH = os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")

# ---------------------------------------------------------------------------
# Combinaciones
# ---------------------------------------------------------------------------

MOD_ALIASES = {
    "SUPER": "SUPER", "WIN": "SUPER", "MOD4": "SUPER", "LOGO": "SUPER",
    "CTRL": "CTRL", "CONTROL": "CTRL",
    "ALT": "ALT", "MOD1": "ALT",
    "SHIFT": "SHIFT",
}
MOD_ORDER = ["SUPER", "CTRL", "ALT", "SHIFT"]
CODE_DIGITS = {str(c): str((c - 9) % 10) for c in range(10, 20)}  # code:10 -> 1 ... code:19 -> 0
KEY_PRETTY = {
    "return": "Enter", "space": "Espacio", "escape": "Esc", "backspace": "⌫",
    "tab": "Tab", "left": "←", "right": "→", "up": "↑", "down": "↓",
    "comma": ",", "period": ".", "slash": "/", "minus": "-", "equal": "=",
    "print": "ImprPant", "delete": "Supr", "home": "Inicio", "end": "Fin",
    "mouse:272": "Clic izq.", "mouse:273": "Clic der.", "mouse:274": "Clic medio",
    "mouse_down": "Rueda ↓", "mouse_up": "Rueda ↑",
}
MOD_PRETTY = {"SUPER": "Super", "CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift"}


def parse_combo(text):
    """'SUPER SHIFT + RETURN' -> (frozenset({'SUPER','SHIFT'}), 'RETURN')."""
    mods, key = set(), ""
    for part in str(text or "").split("+"):
        for token in part.split():
            mod = MOD_ALIASES.get(token.upper())
            if mod:
                mods.add(mod)
            else:
                key = token
    return frozenset(mods), key


def normalize_key(key):
    k = key.strip()
    if k.lower().startswith("code:") and k[5:] in CODE_DIGITS:
        return CODE_DIGITS[k[5:]]
    return k.lower()


def combo_id(text):
    mods, key = parse_combo(text)
    return ",".join(sorted(mods)) + "|" + normalize_key(key)


def combo_text(mods, key):
    return " + ".join([m for m in MOD_ORDER if m in mods] + [key])


def canonical(text):
    mods, key = parse_combo(text)
    return combo_text(mods, key) if key else ""


def bare_key_warning(text):
    """True si la combinación es una tecla común sin modificadores (p. ej. Home, A, Return)."""
    mods, key = parse_combo(text)
    if mods or not key:
        return False
    k = key.lower()
    special = (k.startswith("xf86") or k.startswith("mouse") or k in ("print", "pause", "scroll_lock", "menu")
               or (k[0] == "f" and k[1:].isdigit() and int(k[1:]) >= 13))
    return not special


def pretty_parts(text):
    mods, key = parse_combo(text)
    parts = [MOD_PRETTY[m] for m in MOD_ORDER if m in mods]
    k = key
    if k.lower().startswith("code:") and k[5:] in CODE_DIGITS:
        k = CODE_DIGITS[k[5:]]
    k = KEY_PRETTY.get(k.lower(), k)
    if k.startswith("XF86"):
        k = k[4:]
    if len(k) == 1:
        k = k.upper()
    if k:
        parts.append(k)
    return parts


# ---------------------------------------------------------------------------
# Lectura de la config
# ---------------------------------------------------------------------------

CATEGORY_NAMES = {
    "applications": "Aplicaciones",
    "tiling": "Ventanas y espacios de trabajo",
    "media": "Multimedia y hardware",
    "utilities": "Utilidades y menús",
    "clipboard": "Portapapeles",
    "voxtype": "Dictado (Voxtype)",
}


@dataclass(eq=False)
class Binding:
    keys: str
    desc: str
    kind: str  # exec | lua
    arg: str
    file: str
    line: int
    locked: bool
    repeating: bool
    removed_by: "Event | None" = None
    replaced_by: "Binding | None" = None

    @property
    def cid(self):
        return combo_id(self.keys)

    @property
    def origin(self):
        if self.file == MANAGED_LUA:
            return "atajos"
        if self.file.startswith(OMARCHY_PATH):
            return "omarchy"
        return "user"

    @property
    def category(self):
        if self.origin == "atajos":
            return "Mis atajos"
        if self.origin == "omarchy":
            stem = os.path.splitext(os.path.basename(self.file))[0]
            return "Omarchy · " + CATEGORY_NAMES.get(stem, stem.capitalize())
        if "/plugins/" in self.file:
            plugin = self.file.split("/plugins/")[1].split("/")[0]
            return "Plugin · " + plugin.split(".")[-1]
        return "Tus archivos · " + self.short_file

    @property
    def short_file(self):
        return self.file.replace(HOME, "~")

    @property
    def command_display(self):
        if self.kind == "exec":
            return self.arg
        return "Acción de Hyprland: " + self.arg


@dataclass
class Event:
    kind: str
    keys: str
    file: str
    line: int


@dataclass
class ScanResult:
    bindings: list = field(default_factory=list)
    unbinds: list = field(default_factory=list)
    error: str = ""


def scan(config=MAIN_CONFIG):
    res = ScanResult()
    try:
        out = subprocess.run(["lua", SCAN_LUA, config], capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001
        res.error = f"No se pudo ejecutar lua: {exc}"
        return res
    if out.returncode != 0 and not out.stdout:
        res.error = out.stderr.strip() or "lua terminó con error"
        return res
    active = {}
    for raw in out.stdout.splitlines():
        f = raw.split("\t")
        if f[0] == "E":
            res.error = f[1] if len(f) > 1 else "error"
        elif f[0] == "B" and len(f) >= 9:
            b = Binding(f[1], f[2], f[3], f[4], f[5], int(f[6] or 0), f[7] == "true", f[8] == "true")
            active.setdefault(b.cid, []).append(b)
            res.bindings.append(b)
        elif f[0] == "U" and len(f) >= 7:
            ev = Event("U", f[1], f[5], int(f[6] or 0))
            res.unbinds.append(ev)
            for b in active.pop(combo_id(ev.keys), []):
                b.removed_by = ev
    # Vincular reemplazos: bind quitado por un unbind y luego vuelto a definir
    last_active = {}
    for b in res.bindings:
        if b.removed_by is None:
            last_active[b.cid] = b
    for b in res.bindings:
        if b.removed_by is not None and b.cid in last_active:
            b.replaced_by = last_active[b.cid]
    return res


# ---------------------------------------------------------------------------
# Estado propio y generación de shortcuts.lua
# ---------------------------------------------------------------------------


def load_state():
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
    except FileNotFoundError:
        st = {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{STATE_FILE} no es JSON válido: {exc}") from exc
    st.setdefault("version", 1)
    st.setdefault("disabled", [])
    st.setdefault("binds", [])
    return st


def lua_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def render_lua(st, replaced_ids):
    lines = [
        "-- Generado por Shortcuts (fluid-shortcuts). No editar a mano:",
        "-- se regenera desde ~/.config/hypr/shortcuts.json cada vez que guardás.",
        "",
    ]
    unbinds, seen = [], set()
    for keys in st["disabled"] + [b["keys"] for b in st["binds"] if combo_id(b["keys"]) in replaced_ids]:
        cid = combo_id(keys)
        if cid not in seen:
            seen.add(cid)
            unbinds.append(keys)
    if unbinds:
        lines.append("-- Atajos desactivados o reemplazados")
        lines += [f"hl.unbind({lua_str(k)})" for k in unbinds]
        lines.append("")
    if st["binds"]:
        lines.append("-- Atajos propios")
    for b in st["binds"]:
        if b.get("kind") == "lua":
            action = "hl.dsp." + b["command"]
        elif b.get("launch"):
            action = "{ launch = " + lua_str(b["command"]) + " }"
        else:
            action = lua_str(b["command"])
        opts = []
        if b.get("locked"):
            opts.append("locked = true")
        if b.get("repeating"):
            opts.append("repeating = true")
        extra = (", { " + ", ".join(opts) + " }") if opts else ""
        desc = lua_str(b["desc"]) if b.get("desc") else "nil"
        lines.append(f"o.bind({lua_str(b['keys'])}, {desc}, {action}{extra})")
    return "\n".join(lines) + "\n"


def backup(path):
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak.{int(time.time())}")


def write_atomic(path, content):
    tmp = path + ".tmp-shortcuts"
    with open(tmp, "w") as fh:
        fh.write(content)
    os.replace(tmp, path)


def hypr(*args):
    try:
        out = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"hyprctl falló: {exc}"


def config_errors():
    return "\n".join(line for line in hypr("configerrors").splitlines() if line.strip())


def ensure_require():
    with open(MAIN_CONFIG) as fh:
        content = fh.read()
    if HOOK_MARKER in content:
        return
    backup(MAIN_CONFIG)
    sep = "" if content.endswith("\n") else "\n"
    content += sep + "\n" + HOOK_MARKER + "\n" + HOOK_LINE + "\n"
    write_atomic(MAIN_CONFIG, content)


def apply_state(st, others):
    """Escribe shortcuts.lua + shortcuts.json, valida y recarga. Revierte si falla.

    `others` son los binds que no son de Shortcuts, para decidir qué teclas
    hay que liberar con hl.unbind antes de reasignarlas.
    """
    taken = {b.cid for b in others}
    replaced_ids = {combo_id(b["keys"]) for b in st["binds"] if combo_id(b["keys"]) in taken}
    lua = render_lua(st, replaced_ids)

    old_lua = open(MANAGED_LUA).read() if os.path.exists(MANAGED_LUA) else None
    old_state = open(STATE_FILE).read() if os.path.exists(STATE_FILE) else None
    old_main = open(MAIN_CONFIG).read()

    def revert():
        if old_lua is None:
            if os.path.exists(MANAGED_LUA):
                os.remove(MANAGED_LUA)
        else:
            write_atomic(MANAGED_LUA, old_lua)
        if old_state is not None:
            write_atomic(STATE_FILE, old_state)
        if open(MAIN_CONFIG).read() != old_main:
            write_atomic(MAIN_CONFIG, old_main)
        hypr("reload")

    write_atomic(MANAGED_LUA, lua)
    ensure_require()
    check = scan()
    if check.error:
        revert()
        raise RuntimeError("La config generada no carga en Lua:\n" + check.error)
    hypr("reload")
    time.sleep(0.4)
    errors = config_errors()
    if errors:
        revert()
        raise RuntimeError("Hyprland reportó errores y se revirtió el cambio:\n" + errors)
    write_atomic(STATE_FILE, json.dumps(st, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

CSS = b"""
.keycap {
  font-family: monospace;
  font-size: 0.85em;
  font-weight: bold;
  padding: 2px 7px;
  min-width: 12px;
  border-radius: 6px;
  border: 1px solid alpha(currentColor, 0.22);
  border-bottom-width: 2px;
  background: alpha(currentColor, 0.06);
}
.keycap-plus { opacity: 0.45; font-size: 0.8em; }
.keycaps-large .keycap { font-size: 1.15em; padding: 6px 12px; }
.capture-box { padding: 18px; border-radius: 12px; border: 2px dashed alpha(currentColor, 0.2); }
.capture-box.capturing { border-color: @accent_color; background: alpha(@accent_color, 0.08); }
.origin-tag { font-size: 0.75em; padding: 1px 6px; border-radius: 99px; background: alpha(currentColor, 0.08); }
.dim-row { opacity: 0.55; }
"""


def keycaps(text, large=False):
    box = Gtk.Box(spacing=3, valign=Gtk.Align.CENTER)
    if large:
        box.add_css_class("keycaps-large")
    parts = pretty_parts(text)
    for i, p in enumerate(parts):
        if i:
            plus = Gtk.Label(label="+")
            plus.add_css_class("keycap-plus")
            box.append(plus)
        lab = Gtk.Label(label=p)
        lab.add_css_class("keycap")
        box.append(lab)
    return box


def flat_button(label, icon, cb, destructive=False):
    content = Adw.ButtonContent(label=label, icon_name=icon, halign=Gtk.Align.START)
    btn = Gtk.Button(child=content)
    btn.add_css_class("flat")
    if destructive:
        btn.add_css_class("error")
    btn.connect("clicked", cb)
    return btn


class EditorDialog(Adw.Dialog):
    """Crear o editar un atajo propio."""

    def __init__(self, window, *, initial=None, title="Nuevo atajo", own_index=None, moving=None):
        super().__init__(title=title, content_width=520)
        self.window = window
        self.own_index = own_index  # índice en state["binds"] si se edita uno propio
        self.moving = moving  # Binding de Omarchy/usuario que se mueve a otra tecla
        init = initial or {}
        self.kind = init.get("kind", "exec")
        self.capturing = False

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button(label="Guardar")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self.on_save)
        header.pack_end(self.save_btn)

        page = Adw.PreferencesPage()

        # --- Combinación ---
        g1 = Adw.PreferencesGroup(title="Combinación")
        self.capture_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.FILL)
        self.capture_box.add_css_class("capture-box")
        self.capture_caps = Gtk.Box(halign=Gtk.Align.CENTER)
        self.capture_hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.capture_hint.add_css_class("dim-label")
        self.capture_btn = Gtk.Button(label="Capturar combinación", halign=Gtk.Align.CENTER)
        self.capture_btn.add_css_class("pill")
        self.capture_btn.connect("clicked", self.toggle_capture)
        self.capture_box.append(self.capture_caps)
        self.capture_box.append(self.capture_hint)
        self.capture_box.append(self.capture_btn)
        g1.add(self.capture_box)

        self.keys_row = Adw.EntryRow(title="O escribila: SUPER + SHIFT + R")
        self.keys_row.set_text(init.get("keys", ""))
        self.keys_row.connect("changed", self.on_keys_changed)
        g1.add(self.keys_row)
        self.conflict_row = Adw.ActionRow(visible=False)
        self.conflict_icon = Gtk.Image(icon_name="dialog-warning-symbolic")
        self.conflict_row.add_prefix(self.conflict_icon)
        g1.add(self.conflict_row)
        page.add(g1)

        # --- Acción ---
        g2 = Adw.PreferencesGroup(title="Acción")
        self.desc_row = Adw.EntryRow(title="Descripción")
        self.desc_row.set_text(init.get("desc", ""))
        g2.add(self.desc_row)
        if self.kind == "lua":
            self.cmd_row = Adw.ActionRow(title="Acción interna de Hyprland", subtitle=init.get("command", ""))
            self.cmd_row.set_subtitle_selectable(True)
            self.cmd_value = init.get("command", "")
            g2.add(self.cmd_row)
            self.launch_row = None
        else:
            self.cmd_row = Adw.EntryRow(title="Comando")
            self.cmd_row.set_text(init.get("command", ""))
            g2.add(self.cmd_row)
            self.launch_row = Adw.SwitchRow(
                title="Abrir como aplicación",
                subtitle="Lo lanza con uwsm-app; usalo para apps gráficas",
                active=init.get("launch", False),
            )
            g2.add(self.launch_row)
        page.add(g2)

        g3 = Adw.PreferencesGroup(title="Opciones")
        self.locked_row = Adw.SwitchRow(title="Funciona con la pantalla bloqueada", active=init.get("locked", False))
        self.repeat_row = Adw.SwitchRow(title="Repetir al mantener apretado", active=init.get("repeating", False))
        g3.add(self.locked_row)
        g3.add(self.repeat_row)
        page.add(g3)

        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(header)
        self.set_child(tv)

        keyctl = Gtk.EventControllerKey()
        keyctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keyctl.connect("key-pressed", self.on_key)
        self.add_controller(keyctl)

        self.on_keys_changed()
        self.set_hint()

    # -- captura --
    def set_hint(self):
        if self.capturing:
            self.capture_hint.set_label("Apretá la combinación ahora… (Esc cancela)\n"
                                        "Si Hyprland ya usa esa combinación la va a interceptar: en ese caso escribila abajo.")
            self.capture_btn.set_label("Cancelar captura")
            self.capture_box.add_css_class("capturing")
        else:
            self.capture_hint.set_label("" if self.keys_row.get_text().strip() else "Todavía no elegiste una combinación")
            self.capture_btn.set_label("Capturar combinación")
            self.capture_box.remove_css_class("capturing")

    def toggle_capture(self, *_):
        self.capturing = not self.capturing
        self.set_hint()
        if self.capturing:
            self.capture_btn.grab_focus()

    def on_key(self, ctl, keyval, keycode, state):
        if not self.capturing:
            return False
        name = Gdk.keyval_name(keyval) or ""
        if name == "Escape" and not (state & Gdk.ModifierType.MODIFIER_MASK):
            self.capturing = False
            self.set_hint()
            return True
        if name.split("_")[0] in ("Super", "Shift", "Control", "Alt", "Meta", "Hyper", "ISO"):
            return True  # sólo modificador: seguir esperando
        mods = set()
        if state & Gdk.ModifierType.SUPER_MASK:
            mods.add("SUPER")
        if state & Gdk.ModifierType.CONTROL_MASK:
            mods.add("CTRL")
        if state & Gdk.ModifierType.ALT_MASK:
            mods.add("ALT")
        if state & Gdk.ModifierType.SHIFT_MASK:
            mods.add("SHIFT")
        # Tecla sin Shift aplicado (Shift+1 -> 1, no "exclam")
        display = self.get_display()
        ok, base, *_ = display.translate_key(keycode, 0, ctl.get_group())
        if ok and base:
            name = Gdk.keyval_name(base) or name
        if len(name) == 1:
            name = name.upper()
        self.keys_row.set_text(combo_text(mods, name))
        self.capturing = False
        self.set_hint()
        return True

    # -- validación --
    def current_keys(self):
        return canonical(self.keys_row.get_text())

    def on_keys_changed(self, *_):
        keys = self.current_keys()
        child = self.capture_caps.get_first_child()
        if child:
            self.capture_caps.remove(child)
        if keys:
            self.capture_caps.append(keycaps(keys, large=True))
        self.conflict_row.set_visible(False)
        self.save_btn.set_sensitive(bool(keys))
        if not keys:
            self.set_hint()
            return
        cid = combo_id(keys)
        own = [i for i, b in enumerate(self.window.state["binds"]) if combo_id(b["keys"]) == cid and i != self.own_index]
        if own:
            other = self.window.state["binds"][own[0]]
            self.conflict_row.set_title("Ya tenés un atajo propio con esta combinación")
            self.conflict_row.set_subtitle(other.get("desc") or other["command"])
            self.conflict_icon.set_from_icon_name("dialog-error-symbolic")
            self.conflict_row.set_visible(True)
            self.save_btn.set_sensitive(False)
        else:
            clash = [b for b in self.window.others_active() if b.cid == cid and b is not self.moving]
            if clash:
                self.conflict_row.set_title("Va a reemplazar a: " + (clash[0].desc or clash[0].command_display))
                self.conflict_row.set_subtitle(clash[0].category)
                self.conflict_icon.set_from_icon_name("dialog-warning-symbolic")
                self.conflict_row.set_visible(True)
            elif bare_key_warning(keys):
                self.conflict_row.set_title("Sin modificadores: esta tecla deja de funcionar en tus apps")
                self.conflict_row.set_subtitle("Hyprland la captura en todo el sistema. Sumale Super, Ctrl o Alt.")
                self.conflict_icon.set_from_icon_name("dialog-warning-symbolic")
                self.conflict_row.set_visible(True)
        self.set_hint()

    def on_save(self, *_):
        keys = self.current_keys()
        if self.kind == "lua":
            command = self.cmd_value
        else:
            command = self.cmd_row.get_text().strip()
        if not command:
            self.window.toast("Falta el comando")
            return
        entry = {
            "keys": keys,
            "desc": self.desc_row.get_text().strip(),
            "kind": self.kind,
            "command": command,
            "launch": bool(self.launch_row and self.launch_row.get_active()),
            "locked": self.locked_row.get_active(),
            "repeating": self.repeat_row.get_active(),
        }
        st = json.loads(json.dumps(self.window.state))
        if self.own_index is not None:
            old = st["binds"][self.own_index]
            if old.get("moved_from"):
                entry["moved_from"] = old["moved_from"]
            st["binds"][self.own_index] = entry
        else:
            if self.moving is not None and combo_id(self.moving.keys) != combo_id(keys):
                entry["moved_from"] = self.moving.keys
                if all(combo_id(k) != self.moving.cid for k in st["disabled"]):
                    st["disabled"].append(self.moving.keys)
            st["binds"].append(entry)
        if self.window.commit(st, "Atajo guardado"):
            self.close()


class ShortcutsWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Shortcuts", default_width=920, default_height=760)
        self.state = {"disabled": [], "binds": []}
        self.result = ScanResult()

        self.toasts = Adw.ToastOverlay()
        header = Adw.HeaderBar()

        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Nuevo atajo")
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda *_: EditorDialog(self).present(self))
        header.pack_start(add)

        self.filter = Adw.ToggleGroup()
        for name, label in (("all", "Activos"), ("mine", "Míos"), ("off", "Desactivados")):
            self.filter.add(Adw.Toggle(name=name, label=label))
        self.filter.set_active_name("all")
        self.filter.connect("notify::active", lambda *_: self.rebuild())
        header.set_title_widget(self.filter)

        menu = Gio.Menu()
        menu.append("Volver a leer la config", "win.reload")
        menu.append("Abrir hyprland.lua", "win.open-main")
        menu.append("Abrir bindings.lua", "win.open-bindings")
        menu.append("Acerca de", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        for name, cb in (("reload", lambda *_: self.reload(toast=True)),
                         ("open-main", lambda *_: self.open_file(MAIN_CONFIG)),
                         ("open-bindings", lambda *_: self.open_file(os.path.join(HYPR, "bindings.lua"))),
                         ("about", self.on_about)):
            act = Gio.SimpleAction(name=name)
            act.connect("activate", cb)
            self.add_action(act)

        self.search = Gtk.SearchEntry(placeholder_text="Buscar por tecla, descripción o comando…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.rebuild())
        self.search.set_key_capture_widget(self)
        search_bar = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        search_clamp = Adw.Clamp(maximum_size=860, child=self.search)
        search_bar.append(search_clamp)
        search_clamp.set_hexpand(True)

        self.banner = Adw.Banner()

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                margin_top=12, margin_bottom=24, margin_start=12, margin_end=12)
        self.scroller = Gtk.ScrolledWindow(vexpand=True,
                                           child=Adw.Clamp(maximum_size=860, child=self.list_box))

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.add_top_bar(search_bar)
        tv.add_top_bar(self.banner)
        tv.set_content(self.scroller)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        esc = Gtk.ShortcutController()
        esc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                      action=Gtk.CallbackAction.new(lambda *_: self.search.grab_focus() or True)))
        esc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>n"),
                                      action=Gtk.CallbackAction.new(
                                          lambda *_: EditorDialog(self).present(self) or True)))
        self.add_controller(esc)

        self.reload()

    # -- datos --
    def reload(self, toast=False):
        try:
            self.state = load_state()
        except RuntimeError as exc:
            self.toast(str(exc))
        self.result = scan()
        if self.result.error:
            self.banner.set_title("Error leyendo la config: " + self.result.error)
            self.banner.set_revealed(True)
        else:
            errs = config_errors()
            self.banner.set_title("Hyprland reporta errores de config: " + errs.splitlines()[0] if errs else "")
            self.banner.set_revealed(bool(errs))
        self.rebuild()
        if toast:
            self.toast(f"{len(self.result.bindings)} atajos leídos")

    def others_active(self):
        return [b for b in self.result.bindings if b.origin != "atajos" and b.removed_by is None]

    def own_index_for(self, b):
        own = [b2 for b2 in self.result.bindings if b2.origin == "atajos"]
        if b not in own:
            return None
        pos = own.index(b)
        return pos if pos < len(self.state["binds"]) else None

    def commit(self, st, message):
        others = [b for b in self.result.bindings if b.origin != "atajos"]
        try:
            apply_state(st, others)
        except Exception as exc:  # noqa: BLE001
            self.error_dialog(str(exc))
            self.reload()
            return False
        self.reload()
        self.toast(message)
        return True

    # -- acciones --
    def edit_own(self, b):
        idx = self.own_index_for(b)
        if idx is None:
            return
        EditorDialog(self, initial=self.state["binds"][idx], title="Editar atajo", own_index=idx).present(self)

    def delete_own(self, b):
        idx = self.own_index_for(b)
        if idx is None:
            return
        st = json.loads(json.dumps(self.state))
        removed = st["binds"].pop(idx)
        msg = "Atajo eliminado"
        if removed.get("moved_from"):
            mid = combo_id(removed["moved_from"])
            st["disabled"] = [k for k in st["disabled"] if combo_id(k) != mid]
            msg = "Atajo eliminado; el original volvió a " + canonical(removed["moved_from"])
        self.commit(st, msg)

    def move_other(self, b):
        initial = {
            "keys": "", "desc": b.desc, "kind": b.kind, "command": b.arg,
            "launch": False, "locked": b.locked, "repeating": b.repeating,
        }
        if b.kind == "exec" and b.arg.startswith("uwsm-app -- "):
            initial["command"] = b.arg[len("uwsm-app -- "):]
            initial["launch"] = True
        EditorDialog(self, initial=initial, title="Cambiar combinación", moving=b).present(self)

    def edit_action_other(self, b):
        initial = {
            "keys": b.keys, "desc": b.desc, "kind": "exec",
            "command": b.arg if b.kind == "exec" else "", "launch": False,
            "locked": b.locked, "repeating": b.repeating,
        }
        EditorDialog(self, initial=initial, title="Reemplazar acción").present(self)

    def disable_other(self, b):
        st = json.loads(json.dumps(self.state))
        if all(combo_id(k) != b.cid for k in st["disabled"]):
            st["disabled"].append(canonical(b.keys) or b.keys)
        self.commit(st, "Desactivado: " + " + ".join(pretty_parts(b.keys)))

    def reenable(self, b):
        st = json.loads(json.dumps(self.state))
        st["disabled"] = [k for k in st["disabled"] if combo_id(k) != b.cid]
        self.commit(st, "Reactivado: " + (b.desc or canonical(b.keys)))

    def open_file(self, path, line=0):
        if not os.path.exists(path):
            self.toast("No existe " + path)
            return
        try:
            subprocess.Popen(["omarchy-launch-editor", path], start_new_session=True)
        except FileNotFoundError:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)

    # -- UI --
    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=4))

    def error_dialog(self, msg):
        d = Adw.AlertDialog(heading="No se aplicó el cambio", body=msg)
        d.add_response("ok", "Entendido")
        d.present(self)

    def on_about(self, *_):
        Adw.AboutDialog(
            application_name="Shortcuts", application_icon=APP_ID,
            version="1.0", developer_name="Markos", website=REPO_URL, issue_url=REPO_URL + "/issues",
            license_type=Gtk.License.MIT_X11,
            comments="Administrador de atajos de Hyprland para Omarchy.\n\n"
                     "Tus cambios viven en ~/.config/hypr/shortcuts.json y shortcuts.lua.",
        ).present(self)

    def matches(self, b, q):
        if not q:
            return True
        hay = " ".join([b.desc, b.arg, b.keys, " ".join(pretty_parts(b.keys)), b.category]).lower()
        return all(term in hay for term in q.lower().split())

    def rebuild(self):
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        mode = self.filter.get_active_name() or "all"
        q = self.search.get_text().strip()

        if mode == "all":
            items = [b for b in self.result.bindings if b.removed_by is None]
        elif mode == "mine":
            items = [b for b in self.result.bindings if b.origin != "omarchy" and b.removed_by is None]
        else:
            items = [b for b in self.result.bindings if b.removed_by is not None]
        items = [b for b in items if self.matches(b, q)]

        dup = {}
        for b in self.result.bindings:
            if b.removed_by is None:
                dup.setdefault(b.cid, []).append(b)

        groups = {}
        for b in items:
            groups.setdefault(b.category, []).append(b)

        def order(cat):
            if cat == "Mis atajos":
                return (0, cat)
            if cat.startswith("Tus archivos") or cat.startswith("Plugin"):
                return (1, cat)
            return (2, cat)

        if mode == "mine" and not q:
            hint = Adw.PreferencesGroup(
                title="Tus atajos",
                description="«Mis atajos» los administra Shortcuts. Los de tus archivos o plugins se muestran "
                            "como referencia: se editan a mano, pero podés desactivarlos o moverlos desde acá.")
            self.list_box.append(hint)

        if not items:
            status = Adw.StatusPage(
                icon_name="edit-find-symbolic" if q else "preferences-desktop-keyboard-shortcuts-symbolic",
                title="Sin resultados" if q else ("Nada desactivado" if mode == "off" else "Sin atajos"),
                description=("Probá con otra búsqueda" if q else
                             "Los atajos que desactives o reemplaces aparecen acá" if mode == "off" else
                             "Creá uno con el botón +"),
            )
            self.list_box.append(status)
            return

        for cat in sorted(groups, key=order):
            grp = Adw.PreferencesGroup(title=cat)
            for b in groups[cat]:
                grp.add(self.make_row(b, len(dup.get(b.cid, [])) > 1, mode))
            self.list_box.append(grp)

    def make_row(self, b, duplicated, mode):
        row = Adw.ActionRow(use_markup=False)
        row.set_title(b.desc or "(sin descripción)")
        sub = b.command_display
        if mode == "off" and b.removed_by is not None:
            if b.replaced_by is not None:
                sub = "Reemplazado por: " + (b.replaced_by.desc or b.replaced_by.command_display)
            elif b.removed_by.file == MANAGED_LUA:
                sub = "Desactivado por vos · " + b.command_display
            else:
                sub = f"Desactivado en {b.removed_by.file.replace(HOME, '~')}:{b.removed_by.line}"
        row.set_subtitle(sub)
        row.set_subtitle_lines(1)
        row.set_title_lines(1)
        row.set_tooltip_text(f"{b.short_file}:{b.line}\n{b.command_display}")

        if duplicated:
            warn = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="Hay más de un atajo activo con esta combinación")
            warn.add_css_class("warning")
            row.add_suffix(warn)
        row.add_suffix(keycaps(b.keys))

        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        popover = Gtk.Popover(child=pop_box)

        def add(label, icon, fn, destructive=False):
            def cb(*_):
                popover.popdown()
                GLib.idle_add(lambda: fn(b) and False)
            pop_box.append(flat_button(label, icon, cb, destructive))

        if b.removed_by is not None:
            if b.removed_by.file == MANAGED_LUA and b.replaced_by is None:
                add("Reactivar", "edit-undo-symbolic", self.reenable)
            elif b.replaced_by is not None and b.replaced_by.origin == "atajos":
                add("Ver el atajo que lo reemplaza", "go-jump-symbolic", lambda _b: self.edit_own(b.replaced_by))
            if b.origin != "omarchy":
                add("Abrir archivo", "document-edit-symbolic", lambda _b: self.open_file(b.file, b.line))
        elif b.origin == "atajos":
            add("Editar", "document-edit-symbolic", self.edit_own)
            add("Eliminar", "user-trash-symbolic", self.delete_own, destructive=True)
            row.set_activatable(True)
            row.connect("activated", lambda *_: self.edit_own(b))
        else:
            add("Cambiar combinación…", "input-keyboard-symbolic", self.move_other)
            add("Reemplazar la acción…", "document-edit-symbolic", self.edit_action_other)
            add("Desactivar", "action-unavailable-symbolic", self.disable_other, destructive=True)
            if b.origin != "omarchy":
                add("Abrir archivo", "text-x-generic-symbolic", lambda _b: self.open_file(b.file, b.line))

        if pop_box.get_first_child() is not None:
            mb = Gtk.MenuButton(icon_name="view-more-symbolic", popover=popover, valign=Gtk.Align.CENTER)
            mb.add_css_class("flat")
            row.add_suffix(mb)
        if mode == "off":
            row.add_css_class("dim-row")
        return row


class ShortcutsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_act = Gio.SimpleAction(name="quit")
        quit_act.connect("activate", lambda *_: self.quit())
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        win = self.props.active_window or ShortcutsWindow(self)
        win.present()


def cli(argv):
    """Modo texto para depurar: `shortcuts --list` / `shortcuts --render`."""
    res = scan()
    if res.error:
        print("ERROR:", res.error, file=sys.stderr)
    if argv[0] == "--list":
        for b in res.bindings:
            flag = " " if b.removed_by is None else "x"
            print(f"{flag} {canonical(b.keys):34} {b.desc[:38]:38} {b.origin:8} {b.short_file}:{b.line}")
    elif argv[0] == "--render":
        st = load_state()
        taken = {b.cid for b in res.bindings if b.origin != "atajos"}
        print(render_lua(st, {combo_id(b["keys"]) for b in st["binds"] if combo_id(b["keys"]) in taken}), end="")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--list", "--render"):
        sys.exit(cli(sys.argv[1:]))
    sys.exit(ShortcutsApp().run(sys.argv))
