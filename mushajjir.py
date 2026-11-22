#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
مُشَجِّر (Mushajjir) — برنامج خفيف لصناعة المخططات التعليمية والتشجير العربي
Lightweight Arabic Educational Diagram & Tree Editor for Linux / GNOME (GTK4 + Cairo + Pango).

Features:
  - Native Arabic RTL & Tashkeel (diacritics) shaping via Pango + HarfBuzz + Cairo
  - Bidirectional Tree Branching (تشجير من الأعلى والأسفل، أو من اليمين واليسار)
  - Multi-line support (Enter for new lines) in both box titles and braced examples/notes
  - Hybrid Smart Auto-Layout (Tidy Tree, Vertical TB & Horizontal RL) + Free Positioning Mode
  - Smart dynamic branch routing when dragging nodes freely above or below their parent
  - Magnetic alignment snap guides when dragging boxes freely
  - First-class Curly Braces (الأقواس الحاصرة) for examples/annotations
  - Box shapes: Rectangle, Rounded, Dashed Rounded (إطار متقطّع), and Borderless
  - Fine control over vertical/horizontal box padding, minimum height, brace height & spacing
  - Resizable & collapsible split pane (Gtk.Paned) between inspector and canvas
  - Two drawing styles: Hand-drawn / Sketchy (يدوي) & Clean Geometric (هندسي)
  - Custom free-floating boxes & arbitrary cross-links/arrows between any two nodes
  - Crisp vector export to SVG, PDF, and high-DPI PNG (white, transparent, or dark background)
  - Full Undo/Redo, JSON (.msh) save/load, Dark Mode GNOME UI + Paper Preview toggle
  - Built-in CLI export & GNOME desktop launcher installer (--install-desktop)

Void Linux (x86_64-musl) dependencies:
  doas xbps-install -S python3 python3-gobject python3-cairo gtk4 pango
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402


APP_NAME = "مُشَجِّر"
APP_ID = "org.mushajjir.App"
APP_VERSION = "1.4.0"

# Preferred Arabic fonts in priority order
PREFERRED_ARABIC_FONTS = [
    "Amiri",
    "Scheherazade New",
    "Noto Naskh Arabic",
    "Vazirmatn",
    "Noto Sans Arabic",
    "Noto Kufi Arabic",
    "Traditional Arabic",
    "DejaVu Sans",
]

# Curated soft academic fill swatches: (name_ar, hex_light, hex_dark)
COLOR_SWATCHES = [
    ("شفاف (افتراضي)", "", ""),
    ("ورقي دافئ", "#FDF6E3", "#2A261E"),
    ("أزرق سماوي", "#EBF4FF", "#1C2838"),
    ("أخضر هادئ", "#ECFDF3", "#192D24"),
    ("عنبر فاتح", "#FEF3C7", "#332917"),
    ("وردي ناعم", "#FCE7F3", "#331D29"),
    ("بنفسجي خفيف", "#F3E8FF", "#281F36"),
]


def hex_to_rgb(hex_color: str) -> Tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0.0, 0.0, 0.0)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def detect_default_arabic_font() -> str:
    """Detect the best installed Arabic font via fc-list, falling back cleanly."""
    try:
        out = subprocess.check_output(
            ["fc-list", ":lang=ar", "family"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        installed = set()
        for line in out.splitlines():
            for part in line.split(","):
                name = part.strip()
                if name:
                    installed.add(name)
        for preferred in PREFERRED_ARABIC_FONTS:
            if preferred in installed:
                return preferred
        if installed:
            return sorted(installed)[0]
    except Exception:
        pass
    return "Noto Naskh Arabic"


def list_installed_fonts() -> List[str]:
    """Return sorted list of installed font families, prioritizing Arabic fonts."""
    fonts = set()
    try:
        out = subprocess.check_output(
            ["fc-list", ":lang=ar", "family"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        for line in out.splitlines():
            for part in line.split(","):
                name = part.strip()
                if name:
                    fonts.add(name)
    except Exception:
        pass
    ordered = [f for f in PREFERRED_ARABIC_FONTS if f in fonts]
    rest = sorted(f for f in fonts if f not in ordered)
    result = ordered + rest
    return result if result else ["Noto Naskh Arabic", "DejaVu Sans", "Sans"]


# =============================================================================
# 1. DATA MODEL
# =============================================================================

@dataclass
class Node:
    id: str
    title: str
    note: str = ""  # Multi-line text under/above/beside curly brace
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)
    shape: str = "rect"  # "rect", "rounded", "dashed", "none"
    fill_idx: int = 0  # Index into COLOR_SWATCHES
    is_free: bool = False  # True if independent free-floating box (not part of tree)
    free_pos: Optional[List[float]] = None  # [x, y] center if manually placed/dragged
    show_brace: bool = True  # Draw curly brace when note is non-empty
    branch_side: str = "down"  # "down" (تحت/يسار) or "up" (فوق/يمين)
    seed: int = 0  # Deterministic seed for hand-drawn sketchy rendering

    # Transient computed layout fields (not serialized)
    x: float = 0.0
    y: float = 0.0
    w: float = 80.0
    h: float = 34.0
    note_w: float = 0.0
    note_h: float = 0.0
    total_w: float = 80.0
    total_h: float = 34.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "note": self.note,
            "parent_id": self.parent_id,
            "children": list(self.children),
            "shape": self.shape,
            "fill_idx": self.fill_idx,
            "is_free": self.is_free,
            "free_pos": list(self.free_pos) if self.free_pos is not None else None,
            "show_brace": self.show_brace,
            "branch_side": self.branch_side,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            note=d.get("note", ""),
            parent_id=d.get("parent_id"),
            children=list(d.get("children", [])),
            shape=d.get("shape", "rect"),
            fill_idx=int(d.get("fill_idx", 0)),
            is_free=bool(d.get("is_free", False)),
            free_pos=list(d["free_pos"]) if d.get("free_pos") is not None else None,
            show_brace=bool(d.get("show_brace", True)),
            branch_side=d.get("branch_side", "down"),
            seed=int(d.get("seed", abs(hash(d["id"])) % 100000)),
        )


@dataclass
class CustomLink:
    id: str
    src_id: str
    dst_id: str
    label: str = ""
    dashed: bool = False
    arrow: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CustomLink":
        return cls(
            id=d["id"],
            src_id=d["src_id"],
            dst_id=d["dst_id"],
            label=d.get("label", ""),
            dashed=bool(d.get("dashed", False)),
            arrow=bool(d.get("arrow", True)),
        )


@dataclass
class DiagramSettings:
    direction: str = "vertical"  # "vertical" (TB/BT, RTL siblings) or "horizontal" (RL/LR)
    style: str = "sketchy"  # "sketchy" (يدوي) or "clean" (هندسي)
    connector: str = "orthogonal"  # "orthogonal", "curved", "straight"
    show_arrows: bool = False
    font_family: str = field(default_factory=detect_default_arabic_font)
    font_size: int = 18
    note_font_size: int = 15
    h_gap: float = 24.0
    v_gap: float = 42.0
    box_pad_x: float = 16.0
    box_pad_y: float = 6.0
    min_box_h: float = 28.0
    brace_height: float = 14.0
    line_spacing: float = 4.0
    line_width: float = 2.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DiagramSettings":
        return cls(
            direction=d.get("direction", "vertical"),
            style=d.get("style", "sketchy"),
            connector=d.get("connector", "orthogonal"),
            show_arrows=bool(d.get("show_arrows", False)),
            font_family=d.get("font_family", detect_default_arabic_font()),
            font_size=int(d.get("font_size", 18)),
            note_font_size=int(d.get("note_font_size", 15)),
            h_gap=float(d.get("h_gap", 24.0)),
            v_gap=float(d.get("v_gap", 42.0)),
            box_pad_x=float(d.get("box_pad_x", 16.0)),
            box_pad_y=float(d.get("box_pad_y", 6.0)),
            min_box_h=float(d.get("min_box_h", 28.0)),
            brace_height=float(d.get("brace_height", 14.0)),
            line_spacing=float(d.get("line_spacing", 4.0)),
            line_width=float(d.get("line_width", 2.0)),
        )


def get_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "mushajjir" / "config.json"


def load_user_config() -> dict:
    path = get_config_path()
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_user_config(config: dict) -> bool:
    path = get_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def get_default_diagram_settings(config: Optional[dict] = None) -> DiagramSettings:
    cfg = config if config is not None else load_user_config()
    saved = cfg.get("default_settings")
    if isinstance(saved, dict):
        return DiagramSettings.from_dict(saved)
    return DiagramSettings()


class Diagram:
    def __init__(self, settings: Optional[DiagramSettings] = None) -> None:
        self.nodes: Dict[str, Node] = {}
        self.root_ids: List[str] = []
        self.links: List[CustomLink] = []
        self.settings: DiagramSettings = settings if settings is not None else DiagramSettings()

    def new_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def add_node(
        self,
        title: str,
        parent_id: Optional[str] = None,
        note: str = "",
        is_free: bool = False,
        free_pos: Optional[List[float]] = None,
        shape: str = "rect",
        fill_idx: int = 0,
        branch_side: Optional[str] = None,
    ) -> Node:
        nid = self.new_id()
        # Inherit parent's branch_side if not explicitly provided
        if branch_side is None:
            if parent_id and parent_id in self.nodes and self.nodes[parent_id].parent_id is not None:
                resolved_side = self.nodes[parent_id].branch_side
            else:
                resolved_side = "down"
        else:
            resolved_side = branch_side

        node = Node(
            id=nid,
            title=title,
            note=note,
            parent_id=None if is_free else parent_id,
            is_free=is_free,
            free_pos=free_pos,
            shape=shape,
            fill_idx=fill_idx,
            branch_side=resolved_side,
            seed=random.randint(1, 999999),
        )
        self.nodes[nid] = node
        if is_free:
            pass
        elif parent_id and parent_id in self.nodes:
            self.nodes[parent_id].children.append(nid)
        else:
            self.root_ids.append(nid)
        return node

    def set_subtree_side(self, nid: str, side: str) -> None:
        """Flip a node and its entire subtree to 'up' or 'down', clearing stale free_pos."""
        if nid not in self.nodes:
            return
        node = self.nodes[nid]
        node.branch_side = side
        node.free_pos = None
        for cid in node.children:
            self.set_subtree_side(cid, side)

    def delete_node(self, nid: str) -> None:
        if nid not in self.nodes:
            return
        node = self.nodes[nid]
        for cid in list(node.children):
            self.delete_node(cid)
        if node.parent_id and node.parent_id in self.nodes:
            parent = self.nodes[node.parent_id]
            if nid in parent.children:
                parent.children.remove(nid)
        if nid in self.root_ids:
            self.root_ids.remove(nid)
        self.links = [lk for lk in self.links if lk.src_id != nid and lk.dst_id != nid]
        del self.nodes[nid]

    def move_sibling(self, nid: str, delta: int) -> bool:
        """Reorder a node among its same-side siblings (delta = -1 right/up, +1 left/down)."""
        node = self.nodes.get(nid)
        if not node or node.is_free:
            return False
        lst = self.nodes[node.parent_id].children if node.parent_id else self.root_ids
        if nid not in lst:
            return False
        same_side = [cid for cid in lst if cid in self.nodes and self.nodes[cid].branch_side == node.branch_side]
        s_idx = same_side.index(nid)
        target_s_idx = s_idx + delta
        if 0 <= target_s_idx < len(same_side):
            other_id = same_side[target_s_idx]
            i1, i2 = lst.index(nid), lst.index(other_id)
            lst[i1], lst[i2] = lst[i2], lst[i1]
            return True
        return False

    def add_link(self, src_id: str, dst_id: str, label: str = "") -> Optional[CustomLink]:
        if src_id == dst_id or src_id not in self.nodes or dst_id not in self.nodes:
            return None
        for lk in self.links:
            if lk.src_id == src_id and lk.dst_id == dst_id:
                return lk
        lk = CustomLink(id=self.new_id(), src_id=src_id, dst_id=dst_id, label=label)
        self.links.append(lk)
        return lk

    def clear_links_for_node(self, nid: str) -> int:
        before = len(self.links)
        self.links = [lk for lk in self.links if lk.src_id != nid and lk.dst_id != nid]
        return before - len(self.links)

    def has_links_for_node(self, nid: str) -> bool:
        return any(lk.src_id == nid or lk.dst_id == nid for lk in self.links)

    def reset_all_free_positions(self) -> None:
        """Snap all tree nodes back to automatic tidy layout (keeps independent free boxes)."""
        for node in self.nodes.values():
            if not node.is_free:
                node.free_pos = None

    def to_dict(self) -> dict:
        return {
            "version": APP_VERSION,
            "settings": self.settings.to_dict(),
            "root_ids": list(self.root_ids),
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "links": [lk.to_dict() for lk in self.links],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Diagram":
        diag = cls()
        diag.settings = DiagramSettings.from_dict(data.get("settings", {}))
        diag.root_ids = list(data.get("root_ids", []))
        for nd in data.get("nodes", []):
            node = Node.from_dict(nd)
            diag.nodes[node.id] = node
        for ld in data.get("links", []):
            diag.links.append(CustomLink.from_dict(ld))
        return diag

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "Diagram":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


@dataclass
class DiagramTab:
    id: str
    diagram: Diagram
    current_file: Optional[str] = None
    undo_stack: List[dict] = field(default_factory=list)
    redo_stack: List[dict] = field(default_factory=list)
    selected_id: Optional[str] = None
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    dirty: bool = False
    is_starter: bool = False
    view_initialized: bool = False
    title_fallback: str = "مخطط جديد"

    def display_name(self) -> str:
        if self.current_file:
            return Path(self.current_file).name
        return self.title_fallback


def create_starter_diagram(settings: Optional[DiagramSettings] = None) -> Diagram:
    """Create the classic Arabic grammar 'أقسام الاسم' diagram as the starter template."""
    d = Diagram(settings=settings)
    root = d.add_node("الاسم")
    root.seed = 101

    zahir = d.add_node("ظاهر", parent_id=root.id, note="الله، ربّ")
    zahir.seed = 202

    mubham = d.add_node("مبهم", parent_id=root.id)
    mubham.seed = 303

    damir = d.add_node("ضمير", parent_id=mubham.id)
    damir.seed = 404
    muttasil = d.add_node("متّصل", parent_id=damir.id, note="ـت، ك، ـه")
    muttasil.seed = 505
    munfasil = d.add_node("منفصل", parent_id=damir.id, note="هو، أنت، إيّاك")
    munfasil.seed = 606

    mawsul = d.add_node("اسم موصول", parent_id=mubham.id, note="الذي، اللاتي، مَن")
    mawsul.seed = 707

    ishara = d.add_node("اسم إشارة", parent_id=mubham.id, note="هذا، أولئك")
    ishara.seed = 808

    shart = d.add_node("اسم شرط", parent_id=mubham.id, note="مَن، أينما")
    shart.seed = 909

    istifham = d.add_node("اسم استفهام", parent_id=mubham.id, note="ما؟ أين؟")
    istifham.seed = 1010

    return d


# =============================================================================
# 2. TEXT MEASUREMENT & BIDIRECTIONAL HYBRID TIDY-TREE LAYOUT ENGINE
# =============================================================================

BRACE_GAP = 5.0
MIN_BOX_W = 68.0


def format_multiline(text: str) -> str:
    return text.replace("\\n", "\n")


def make_pango_layout(
    cr: cairo.Context,
    text: str,
    font_family: str,
    font_size: int,
    bold: bool = False,
    line_spacing: float = 4.0,
) -> Pango.Layout:
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription()
    desc.set_family(font_family)
    desc.set_size(int(font_size * Pango.SCALE))
    if bold:
        desc.set_weight(Pango.Weight.SEMIBOLD)
    layout.set_font_description(desc)
    layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_auto_dir(True)
    layout.set_spacing(int(max(0.0, line_spacing) * Pango.SCALE))
    layout.set_text(format_multiline(text), -1)
    return layout


def get_tight_extents(layout: Pango.Layout) -> Tuple[float, float, float, float]:
    """
    Return (x_offset, y_offset, width, height) using ink extents when available
    so Arabic fonts with tall logical line-boxes don't create excessive vertical whitespace.
    """
    ink, logical = layout.get_pixel_extents()
    if ink.width > 0 and ink.height > 0:
        w = max(float(ink.width), float(logical.width))
        h = float(ink.height)
        return (float(logical.x), float(ink.y), w, h)
    return (float(logical.x), float(logical.y), float(logical.width), float(logical.height))


def node_visual_extents(node: Node, direction: str) -> Tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) covering the node box AND its braced note on the proper side."""
    if direction == "vertical":
        nx0 = node.x - node.total_w / 2.0
        nx1 = node.x + node.total_w / 2.0
        if node.branch_side == "up":
            ny1 = node.y + node.h / 2.0
            ny0 = ny1 - node.total_h
        else:
            ny0 = node.y - node.h / 2.0
            ny1 = ny0 + node.total_h
        return (nx0, ny0, nx1, ny1)
    else:
        ny0 = node.y - node.total_h / 2.0
        ny1 = node.y + node.total_h / 2.0
        if node.branch_side == "up":
            nx0 = node.x - node.w / 2.0
            nx1 = nx0 + node.total_w
        else:
            nx1 = node.x + node.w / 2.0
            nx0 = nx1 - node.total_w
        return (nx0, ny0, nx1, ny1)


class LayoutEngine:
    """Computes tight Arabic text sizes, bidirectional tidy tree layout, and magnetic snap guides."""

    def __init__(self) -> None:
        self._measure_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 16, 16)
        self._measure_cr = cairo.Context(self._measure_surface)

    def measure_text(
        self,
        text: str,
        font_family: str,
        font_size: int,
        bold: bool = False,
        line_spacing: float = 4.0,
    ) -> Tuple[float, float]:
        if not text:
            return (0.0, 0.0)
        layout = make_pango_layout(
            self._measure_cr, text, font_family, font_size, bold=bold, line_spacing=line_spacing
        )
        _ox, _oy, w, h = get_tight_extents(layout)
        return (w, h)

    def layout(self, diagram: Diagram) -> None:
        s = diagram.settings
        for node in diagram.nodes.values():
            tw, th = self.measure_text(
                node.title or " ", s.font_family, s.font_size, bold=False, line_spacing=s.line_spacing
            )
            node.w = max(MIN_BOX_W, tw + s.box_pad_x * 2.0)
            node.h = max(s.min_box_h, th + s.box_pad_y * 2.0)
            if node.note.strip():
                nw, nh = self.measure_text(
                    node.note.strip(), s.font_family, s.note_font_size, bold=False, line_spacing=s.line_spacing
                )
                node.note_w = nw
                node.note_h = nh
            else:
                node.note_w = 0.0
                node.note_h = 0.0

            if s.direction == "vertical":
                node.total_w = max(node.w, node.note_w + 8.0)
                extra_h = (
                    (s.brace_height + BRACE_GAP + node.note_h)
                    if (node.note.strip() and node.show_brace)
                    else ((BRACE_GAP + node.note_h) if node.note.strip() else 0.0)
                )
                node.total_h = node.h + extra_h
            else:
                extra_w = (
                    (s.brace_height + BRACE_GAP + node.note_w)
                    if (node.note.strip() and node.show_brace)
                    else ((BRACE_GAP + node.note_w) if node.note.strip() else 0.0)
                )
                node.total_w = node.w + extra_w
                node.total_h = max(node.h, node.note_h + 6.0)

        if s.direction == "vertical":
            self._layout_vertical(diagram)
        else:
            self._layout_horizontal(diagram)

        for node in diagram.nodes.values():
            if node.free_pos is not None:
                node.x = float(node.free_pos[0])
                node.y = float(node.free_pos[1])

    def _layout_vertical(self, diagram: Diagram) -> None:
        """Bidirectional vertical tidy tree: 'down' children below, 'up' children above, RTL siblings."""
        s = diagram.settings
        subtree_w: Dict[str, float] = {}

        def split_children(nid: str) -> Tuple[List[str], List[str]]:
            node = diagram.nodes[nid]
            valid = [c for c in node.children if c in diagram.nodes]
            down = [c for c in valid if diagram.nodes[c].branch_side != "up"]
            up = [c for c in valid if diagram.nodes[c].branch_side == "up"]
            return (down, up)

        def calc_width(nid: str) -> float:
            node = diagram.nodes[nid]
            down, up = split_children(nid)
            w = node.total_w
            if down:
                dw = sum(calc_width(c) for c in down) + s.h_gap * (len(down) - 1)
                w = max(w, dw)
            if up:
                uw = sum(calc_width(c) for c in up) + s.h_gap * (len(up) - 1)
                w = max(w, uw)
            subtree_w[nid] = w
            return w

        def assign_node(nid: str, left_x: float, anchor_y: float, growing_up: bool) -> None:
            node = diagram.nodes[nid]
            w = subtree_w[nid]
            node.x = left_x + w / 2.0
            if growing_up:
                # anchor_y is the bottom edge of this node's box
                node.y = anchor_y - node.h / 2.0
            else:
                # anchor_y is the top edge of this node's box
                node.y = anchor_y + node.h / 2.0

            down, up = split_children(nid)

            if down:
                down_total = sum(subtree_w[c] for c in down) + s.h_gap * (len(down) - 1)
                cur_right = node.x + down_total / 2.0
                _, _, _, ny1 = node_visual_extents(node, "vertical")
                child_top = ny1 + s.v_gap
                for cid in down:
                    cw = subtree_w[cid]
                    assign_node(cid, cur_right - cw, child_top, growing_up=False)
                    cur_right -= cw + s.h_gap
                # If only downward children exist, center parent over them cleanly
                if not up:
                    first_c = diagram.nodes[down[0]]
                    last_c = diagram.nodes[down[-1]]
                    node.x = (first_c.x + last_c.x) / 2.0

            if up:
                up_total = sum(subtree_w[c] for c in up) + s.h_gap * (len(up) - 1)
                cur_right = node.x + up_total / 2.0
                _, ny0, _, _ = node_visual_extents(node, "vertical")
                child_bottom = ny0 - s.v_gap
                for cid in up:
                    cw = subtree_w[cid]
                    assign_node(cid, cur_right - cw, child_bottom, growing_up=True)
                    cur_right -= cw + s.h_gap
                if not down:
                    first_c = diagram.nodes[up[0]]
                    last_c = diagram.nodes[up[-1]]
                    node.x = (first_c.x + last_c.x) / 2.0

        valid_roots = [r for r in diagram.root_ids if r in diagram.nodes]
        if not valid_roots:
            return
        total_roots_w = sum(calc_width(r) for r in valid_roots) + s.h_gap * 2.5 * max(0, len(valid_roots) - 1)
        cur_right = total_roots_w / 2.0
        for rid in valid_roots:
            rw = subtree_w[rid]
            assign_node(rid, cur_right - rw, 0.0, growing_up=False)
            cur_right -= rw + s.h_gap * 2.5

    def _layout_horizontal(self, diagram: Diagram) -> None:
        """Bidirectional horizontal mushajjar: 'down' children grow LEFT, 'up' children grow RIGHT."""
        s = diagram.settings
        subtree_h: Dict[str, float] = {}
        level_gap = max(s.v_gap, 56.0)
        sib_gap = max(s.h_gap * 0.8, 14.0)

        def split_children(nid: str) -> Tuple[List[str], List[str]]:
            node = diagram.nodes[nid]
            valid = [c for c in node.children if c in diagram.nodes]
            left = [c for c in valid if diagram.nodes[c].branch_side != "up"]
            right = [c for c in valid if diagram.nodes[c].branch_side == "up"]
            return (left, right)

        def calc_height(nid: str) -> float:
            node = diagram.nodes[nid]
            left, right = split_children(nid)
            h = node.total_h
            if left:
                lh = sum(calc_height(c) for c in left) + sib_gap * (len(left) - 1)
                h = max(h, lh)
            if right:
                rh = sum(calc_height(c) for c in right) + sib_gap * (len(right) - 1)
                h = max(h, rh)
            subtree_h[nid] = h
            return h

        def assign_node(nid: str, anchor_x: float, top_y: float, growing_right: bool) -> None:
            node = diagram.nodes[nid]
            h = subtree_h[nid]
            node.y = top_y + h / 2.0
            if growing_right:
                node.x = anchor_x + node.w / 2.0
            else:
                node.x = anchor_x - node.w / 2.0

            left, right = split_children(nid)

            if left:
                left_total = sum(subtree_h[c] for c in left) + sib_gap * (len(left) - 1)
                cur_top = node.y - left_total / 2.0
                nx0, _, _, _ = node_visual_extents(node, "horizontal")
                child_right = nx0 - level_gap
                for cid in left:
                    ch = subtree_h[cid]
                    assign_node(cid, child_right, cur_top, growing_right=False)
                    cur_top += ch + sib_gap
                if not right:
                    first_c = diagram.nodes[left[0]]
                    last_c = diagram.nodes[left[-1]]
                    node.y = (first_c.y + last_c.y) / 2.0

            if right:
                right_total = sum(subtree_h[c] for c in right) + sib_gap * (len(right) - 1)
                cur_top = node.y - right_total / 2.0
                _, _, nx1, _ = node_visual_extents(node, "horizontal")
                child_left = nx1 + level_gap
                for cid in right:
                    ch = subtree_h[cid]
                    assign_node(cid, child_left, cur_top, growing_right=True)
                    cur_top += ch + sib_gap
                if not left:
                    first_c = diagram.nodes[right[0]]
                    last_c = diagram.nodes[right[-1]]
                    node.y = (first_c.y + last_c.y) / 2.0

        valid_roots = [r for r in diagram.root_ids if r in diagram.nodes]
        if not valid_roots:
            return
        cur_top = 0.0
        for rid in valid_roots:
            rh = calc_height(rid)
            assign_node(rid, 0.0, cur_top, growing_right=False)
            cur_top += rh + sib_gap * 3.0

    def get_bounds(self, diagram: Diagram, margin: float = 40.0) -> Tuple[float, float, float, float]:
        if not diagram.nodes:
            return (-200.0, -150.0, 400.0, 300.0)
        s = diagram.settings
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for n in diagram.nodes.values():
            nx0, ny0, nx1, ny1 = node_visual_extents(n, s.direction)
            min_x = min(min_x, nx0)
            min_y = min(min_y, ny0)
            max_x = max(max_x, nx1)
            max_y = max(max_y, ny1)
        return (min_x - margin, min_y - margin, (max_x - min_x) + margin * 2, (max_y - min_y) + margin * 2)

    @staticmethod
    def compute_snap(
        diagram: Diagram, moving_id: str, raw_x: float, raw_y: float, threshold: float = 9.0
    ) -> Tuple[float, float, List[Tuple[str, float, float, float]]]:
        moving = diagram.nodes.get(moving_id)
        if not moving:
            return (raw_x, raw_y, [])

        best_dx = threshold + 1.0
        best_dy = threshold + 1.0
        snap_x = raw_x
        snap_y = raw_y
        v_guide = None
        h_guide = None

        m_left = raw_x - moving.w / 2.0
        m_right = raw_x + moving.w / 2.0
        m_top = raw_y - moving.h / 2.0
        m_bottom = raw_y + moving.h / 2.0

        for other in diagram.nodes.values():
            if other.id == moving_id:
                continue
            o_left = other.x - other.w / 2.0
            o_right = other.x + other.w / 2.0
            o_top = other.y - other.h / 2.0
            o_bottom = other.y + other.h / 2.0

            for m_val, o_val, offset in [
                (raw_x, other.x, 0.0),
                (m_left, o_left, moving.w / 2.0),
                (m_right, o_right, -moving.w / 2.0),
            ]:
                d = abs(m_val - o_val)
                if d < best_dx:
                    best_dx = d
                    snap_x = o_val + offset
                    y1 = min(raw_y - moving.h / 2.0, o_top) - 24.0
                    y2 = max(raw_y + moving.h / 2.0, o_bottom) + 24.0
                    v_guide = ("v", o_val, y1, y2)

            for m_val, o_val, offset in [
                (raw_y, other.y, 0.0),
                (m_top, o_top, moving.h / 2.0),
                (m_bottom, o_bottom, -moving.h / 2.0),
            ]:
                d = abs(m_val - o_val)
                if d < best_dy:
                    best_dy = d
                    snap_y = o_val + offset
                    x1 = min(raw_x - moving.w / 2.0, o_left) - 24.0
                    x2 = max(raw_x + moving.w / 2.0, o_right) + 24.0
                    h_guide = ("h", o_val, x1, x2)

        guides = []
        if v_guide and best_dx <= threshold:
            guides.append(v_guide)
        if h_guide and best_dy <= threshold:
            guides.append(h_guide)
        return (snap_x, snap_y, guides)


# =============================================================================
# 3. CAIRO + PANGOCAIRO VECTOR RENDERER (SKETCHY & CLEAN)
# =============================================================================

class DiagramRenderer:
    """Renders Diagram to any Cairo context (Screen, SVG, PDF, PNG)."""

    def render(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        dark_canvas: bool = False,
        selected_id: Optional[str] = None,
        hover_id: Optional[str] = None,
        connect_src_id: Optional[str] = None,
        guides: Optional[List[Tuple[str, float, float, float]]] = None,
        bg_mode: str = "theme",
        bounds: Optional[Tuple[float, float, float, float]] = None,
    ) -> None:
        if bg_mode == "white":
            is_dark = False
        elif bg_mode == "dark":
            is_dark = True
        elif bg_mode == "transparent":
            is_dark = False
        else:
            is_dark = dark_canvas

        if bounds is not None and bg_mode != "transparent":
            bx, by, bw, bh = bounds
            if is_dark:
                cr.set_source_rgb(0.067, 0.078, 0.098)
            else:
                cr.set_source_rgb(1.0, 1.0, 1.0)
            cr.rectangle(bx, by, bw, bh)
            cr.fill()

        fg = (0.93, 0.94, 0.96) if is_dark else (0.08, 0.09, 0.11)
        note_fg = (0.82, 0.85, 0.90) if is_dark else (0.12, 0.13, 0.16)
        box_bg = (0.11, 0.13, 0.16) if is_dark else (1.0, 1.0, 1.0)
        accent = (0.36, 0.68, 1.0) if is_dark else (0.14, 0.45, 0.85)
        free_badge = (0.95, 0.65, 0.25)

        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        self._draw_all_tree_branches(cr, diagram, fg)

        for lk in diagram.links:
            self._draw_custom_link(cr, diagram, lk, fg, accent, is_dark)

        for node in diagram.nodes.values():
            self._draw_node(
                cr,
                diagram,
                node,
                fg=fg,
                note_fg=note_fg,
                default_box_bg=box_bg,
                accent=accent,
                free_badge=free_badge,
                is_dark=is_dark,
                is_selected=(node.id == selected_id),
                is_hover=(node.id == hover_id),
                is_connect_src=(node.id == connect_src_id),
            )

        if guides:
            cr.save()
            cr.set_source_rgba(0.22, 0.78, 0.92, 0.85)
            cr.set_line_width(1.2)
            cr.set_dash([5.0, 4.0])
            for kind, pos, a, b in guides:
                if kind == "v":
                    cr.move_to(pos, a)
                    cr.line_to(pos, b)
                else:
                    cr.move_to(a, pos)
                    cr.line_to(b, pos)
                cr.stroke()
            cr.restore()

    def _draw_all_tree_branches(self, cr: cairo.Context, diagram: Diagram, fg: Tuple[float, float, float]) -> None:
        s = diagram.settings
        for parent in diagram.nodes.values():
            valid_children = [diagram.nodes[c] for c in parent.children if c in diagram.nodes]
            if not valid_children:
                continue
            if s.connector == "orthogonal":
                if s.direction == "vertical":
                    # Dynamically split by actual vertical position so both upward branches AND freely-dragged
                    # nodes above the parent route cleanly from the parent's top edge!
                    above = [c for c in valid_children if c.y < parent.y]
                    below = [c for c in valid_children if c.y >= parent.y]
                    if below:
                        self._draw_orthogonal_vertical(cr, diagram, parent, below, fg, upward=False)
                    if above:
                        self._draw_orthogonal_vertical(cr, diagram, parent, above, fg, upward=True)
                else:
                    left = [c for c in valid_children if c.x <= parent.x]
                    right = [c for c in valid_children if c.x > parent.x]
                    if left:
                        self._draw_orthogonal_horizontal(cr, diagram, parent, left, fg, rightward=False)
                    if right:
                        self._draw_orthogonal_horizontal(cr, diagram, parent, right, fg, rightward=True)
            else:
                for child in valid_children:
                    self._draw_single_branch(cr, diagram, parent, child, fg)

    def _draw_orthogonal_vertical(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        parent: Node,
        children: List[Node],
        fg: Tuple[float, float, float],
        upward: bool,
    ) -> None:
        s = diagram.settings
        is_root = parent.parent_id is None
        base_lw = s.line_width * (1.30 if is_root else 1.0)
        rng = random.Random(parent.seed + (177 if upward else 77))

        cr.save()
        cr.set_source_rgb(*fg)
        cr.set_line_width(base_lw)

        px = parent.x
        _, p_ny0, _, p_ny1 = node_visual_extents(parent, "vertical")
        if upward:
            py = p_ny0 - (3.0 if (parent.note.strip() and parent.branch_side == "up") else 0.0)
            child_anchors = []
            for c in children:
                _, _, _, c_ny1 = node_visual_extents(c, "vertical")
                cy = c_ny1 + (3.0 if (c.note.strip() and c.branch_side != "up") else 0.0)
                child_anchors.append((c, c.x, cy))
            max_child_bot = max(cy for _, _, cy in child_anchors)
            bus_y = py - (py - max_child_bot) * 0.48 if py > max_child_bot + 10.0 else py - 18.0
        else:
            py = p_ny1 + (3.0 if (parent.note.strip() and parent.branch_side != "up") else 0.0)
            child_anchors = []
            for c in children:
                _, c_ny0, _, _ = node_visual_extents(c, "vertical")
                cy = c_ny0 - (3.0 if (c.note.strip() and c.branch_side == "up") else 0.0)
                child_anchors.append((c, c.x, cy))
            min_child_top = min(cy for _, _, cy in child_anchors)
            bus_y = py + (min_child_top - py) * 0.48 if min_child_top > py + 10.0 else py + 18.0

        self._stroke_segment(cr, px, py, px, bus_y, s.style == "sketchy", rng, amp=0.8)

        r = 7.5
        for _child, cx, cy in child_anchors:
            if abs(cx - px) < 1.5:
                self._stroke_segment(cr, px, bus_y, cx, cy, s.style == "sketchy", rng, amp=0.7)
            else:
                direction = 1.0 if cx > px else -1.0
                vdir = 1.0 if cy >= bus_y else -1.0
                rad = min(r, abs(cx - px) * 0.45, max(2.0, abs(cy - bus_y) * 0.45))
                if s.style == "sketchy":
                    self._stroke_sketchy_elbow(
                        cr, px, bus_y, cx, cy, horizontal_first=True, radius=rad, rng=rng
                    )
                else:
                    cr.new_path()
                    cr.move_to(px, bus_y)
                    cr.line_to(cx - direction * rad, bus_y)
                    cr.curve_to(cx, bus_y, cx, bus_y, cx, bus_y + vdir * rad)
                    cr.line_to(cx, cy)
                    cr.stroke()

            if s.show_arrows:
                self._draw_arrowhead(cr, cx, cy + (10.0 if upward else -10.0), cx, cy, size=8.0)

        cr.restore()

    def _draw_orthogonal_horizontal(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        parent: Node,
        children: List[Node],
        fg: Tuple[float, float, float],
        rightward: bool,
    ) -> None:
        s = diagram.settings
        is_root = parent.parent_id is None
        base_lw = s.line_width * (1.30 if is_root else 1.0)
        rng = random.Random(parent.seed + (277 if rightward else 127))

        cr.save()
        cr.set_source_rgb(*fg)
        cr.set_line_width(base_lw)

        py = parent.y
        p_nx0, _, p_nx1, _ = node_visual_extents(parent, "horizontal")
        if rightward:
            px = p_nx1 + (3.0 if (parent.note.strip() and parent.branch_side == "up") else 0.0)
            child_anchors = []
            for c in children:
                c_nx0, _, _, _ = node_visual_extents(c, "horizontal")
                cx = c_nx0 - (3.0 if (c.note.strip() and c.branch_side != "up") else 0.0)
                child_anchors.append((c, cx, c.y))
            min_child_left = min(cx for _, cx, _ in child_anchors)
            bus_x = px + (min_child_left - px) * 0.48 if min_child_left > px + 10.0 else px + 18.0
        else:
            px = p_nx0 - (3.0 if (parent.note.strip() and parent.branch_side != "up") else 0.0)
            child_anchors = []
            for c in children:
                _, _, c_nx1, _ = node_visual_extents(c, "horizontal")
                cx = c_nx1 + (3.0 if (c.note.strip() and c.branch_side == "up") else 0.0)
                child_anchors.append((c, cx, c.y))
            max_child_right = max(cx for _, cx, _ in child_anchors)
            bus_x = px - (px - max_child_right) * 0.48 if px > max_child_right + 10.0 else px - 18.0

        self._stroke_segment(cr, px, py, bus_x, py, s.style == "sketchy", rng, amp=0.8)

        r = 7.5
        for _child, cx, cy in child_anchors:
            if abs(cy - py) < 1.5:
                self._stroke_segment(cr, bus_x, py, cx, cy, s.style == "sketchy", rng, amp=0.7)
            else:
                vdir = 1.0 if cy > py else -1.0
                hdir = 1.0 if cx >= bus_x else -1.0
                rad = min(r, abs(cy - py) * 0.45, max(2.0, abs(cx - bus_x) * 0.45))
                if s.style == "sketchy":
                    self._stroke_sketchy_elbow(
                        cr, bus_x, py, cx, cy, horizontal_first=False, radius=rad, rng=rng
                    )
                else:
                    cr.new_path()
                    cr.move_to(bus_x, py)
                    cr.line_to(bus_x, cy - vdir * rad)
                    cr.curve_to(bus_x, cy, bus_x, cy, bus_x + hdir * rad, cy)
                    cr.line_to(cx, cy)
                    cr.stroke()

            if s.show_arrows:
                self._draw_arrowhead(cr, cx + (-10.0 if rightward else 10.0), cy, cx, cy, size=8.0)

        cr.restore()

    def _draw_single_branch(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        parent: Node,
        child: Node,
        fg: Tuple[float, float, float],
    ) -> None:
        s = diagram.settings
        rng = random.Random(child.seed + 31)
        cr.save()
        cr.set_source_rgb(*fg)
        cr.set_line_width(s.line_width)

        if s.direction == "vertical":
            upward = child.y < parent.y
            x1 = parent.x
            y1 = (parent.y - parent.h / 2.0) if upward else (parent.y + parent.h / 2.0)
            x2 = child.x
            y2 = (child.y + child.h / 2.0) if upward else (child.y - child.h / 2.0)
            if s.connector == "curved":
                mid_y = (y1 + y2) / 2.0
                cr.move_to(x1, y1)
                cr.curve_to(x1, mid_y, x2, mid_y, x2, y2)
                cr.stroke()
            else:
                self._stroke_segment(cr, x1, y1, x2, y2, s.style == "sketchy", rng, amp=1.0)
            if s.show_arrows:
                self._draw_arrowhead(
                    cr,
                    x1 if s.connector == "straight" else x2,
                    y1 if s.connector == "straight" else (y2 + (12 if upward else -12)),
                    x2,
                    y2,
                    8.0,
                )
        else:
            rightward = child.x > parent.x
            x1 = (parent.x + parent.w / 2.0) if rightward else (parent.x - parent.w / 2.0)
            y1 = parent.y
            x2 = (child.x - child.w / 2.0) if rightward else (child.x + child.w / 2.0)
            y2 = child.y
            if s.connector == "curved":
                mid_x = (x1 + x2) / 2.0
                cr.move_to(x1, y1)
                cr.curve_to(mid_x, y1, mid_x, y2, x2, y2)
                cr.stroke()
            else:
                self._stroke_segment(cr, x1, y1, x2, y2, s.style == "sketchy", rng, amp=1.0)
            if s.show_arrows:
                self._draw_arrowhead(
                    cr,
                    x1 if s.connector == "straight" else (x2 + (-12 if rightward else 12)),
                    y1 if s.connector == "straight" else y2,
                    x2,
                    y2,
                    8.0,
                )

        cr.restore()

    def _draw_custom_link(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        lk: CustomLink,
        fg: Tuple[float, float, float],
        accent: Tuple[float, float, float],
        is_dark: bool,
    ) -> None:
        src = diagram.nodes.get(lk.src_id)
        dst = diagram.nodes.get(lk.dst_id)
        if not src or not dst:
            return
        x1, y1 = self._box_edge_point(src, dst.x, dst.y)
        x2, y2 = self._box_edge_point(dst, src.x, src.y)

        cr.save()
        cr.set_source_rgb(*accent)
        cr.set_line_width(max(1.8, diagram.settings.line_width))
        if lk.dashed:
            cr.set_dash([6.0, 5.0])

        mx = (x1 + x2) / 2.0
        my = (y1 + y2) / 2.0
        dx = x2 - x1
        dy = y2 - y1
        dist = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / dist, dx / dist
        bend = min(32.0, dist * 0.14)
        cx, cy = mx + nx * bend, my + ny * bend

        cr.move_to(x1, y1)
        cr.curve_to(cx, cy, cx, cy, x2, y2)
        cr.stroke()

        cr.set_dash([])
        if lk.arrow:
            self._draw_arrowhead(cr, cx, cy, x2, y2, size=9.0)

        if lk.label.strip():
            layout = make_pango_layout(
                cr,
                lk.label.strip(),
                diagram.settings.font_family,
                max(12, diagram.settings.note_font_size - 1),
                line_spacing=diagram.settings.line_spacing,
            )
            ox, oy, tw, th = get_tight_extents(layout)
            lx = (x1 + 2 * cx + x2) / 4.0 - tw / 2.0
            ly = (y1 + 2 * cy + y2) / 4.0 - th / 2.0
            cr.set_source_rgba(*(0.08, 0.10, 0.13, 0.9) if is_dark else (1.0, 1.0, 1.0, 0.92))
            cr.rectangle(lx - 4, ly - 2, tw + 8, th + 4)
            cr.fill()
            cr.set_source_rgb(*accent)
            cr.move_to(lx - ox, ly - oy)
            PangoCairo.show_layout(cr, layout)

        cr.restore()

    @staticmethod
    def _box_edge_point(node: Node, tx: float, ty: float) -> Tuple[float, float]:
        dx = tx - node.x
        dy = ty - node.y
        if abs(dx) < 1e-5 and abs(dy) < 1e-5:
            return (node.x, node.y)
        hw = node.w / 2.0 + 2.0
        hh = node.h / 2.0 + 2.0
        scale = min(hw / abs(dx) if abs(dx) > 1e-5 else 1e9, hh / abs(dy) if abs(dy) > 1e-5 else 1e9)
        return (node.x + dx * scale, node.y + dy * scale)

    def _draw_node(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        node: Node,
        fg: Tuple[float, float, float],
        note_fg: Tuple[float, float, float],
        default_box_bg: Tuple[float, float, float],
        accent: Tuple[float, float, float],
        free_badge: Tuple[float, float, float],
        is_dark: bool,
        is_selected: bool,
        is_hover: bool,
        is_connect_src: bool,
    ) -> None:
        s = diagram.settings
        x0 = node.x - node.w / 2.0
        y0 = node.y - node.h / 2.0
        rng = random.Random(node.seed)

        fill_rgb = default_box_bg
        if 0 < node.fill_idx < len(COLOR_SWATCHES):
            _name, h_light, h_dark = COLOR_SWATCHES[node.fill_idx]
            fill_rgb = hex_to_rgb(h_dark if is_dark else h_light)

        if is_selected or is_connect_src:
            cr.save()
            glow_col = free_badge if is_connect_src else accent
            cr.set_source_rgba(glow_col[0], glow_col[1], glow_col[2], 0.20)
            self._rounded_rect_path(cr, x0 - 6, y0 - 6, node.w + 12, node.h + 12, 10)
            cr.fill()
            cr.set_source_rgb(*glow_col)
            cr.set_line_width(2.2)
            self._rounded_rect_path(cr, x0 - 4, y0 - 4, node.w + 8, node.h + 8, 8)
            cr.stroke()
            cr.restore()
        elif is_hover:
            cr.save()
            cr.set_source_rgba(accent[0], accent[1], accent[2], 0.12)
            self._rounded_rect_path(cr, x0 - 4, y0 - 4, node.w + 8, node.h + 8, 8)
            cr.fill()
            cr.restore()

        if node.shape != "none":
            cr.save()
            is_dashed = node.shape == "dashed"
            is_rounded = node.shape in ("rounded", "dashed")
            if s.style == "sketchy" and not is_dashed:
                cr.set_source_rgb(*fill_rgb)
                self._rounded_rect_path(cr, x0, y0, node.w, node.h, 12 if is_rounded else 5)
                cr.fill()
                cr.set_source_rgb(*fg)
                cr.set_line_width(s.line_width)
                self._stroke_sketchy_box(cr, x0, y0, node.w, node.h, rounded=is_rounded, rng=rng)
            else:
                rad = 12.0 if is_rounded else 4.0
                cr.set_source_rgb(*fill_rgb)
                self._rounded_rect_path(cr, x0, y0, node.w, node.h, rad)
                cr.fill_preserve()
                cr.set_source_rgb(*fg)
                cr.set_line_width(s.line_width * (0.88 if is_dashed else 1.0))
                if is_dashed:
                    cr.set_dash([9.0, 5.0])
                cr.stroke()
            cr.restore()

        if is_selected and (node.free_pos is not None or node.is_free):
            cr.save()
            cr.set_source_rgb(*free_badge)
            cr.arc(x0 + 8, y0 + 8, 4.0, 0, math.tau)
            cr.fill()
            cr.restore()

        cr.save()
        cr.set_source_rgb(*fg)
        layout = make_pango_layout(cr, node.title or " ", s.font_family, s.font_size, line_spacing=s.line_spacing)
        ox, oy, tw, th = get_tight_extents(layout)
        cr.move_to(node.x - tw / 2.0 - ox, node.y - th / 2.0 - oy)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

        if node.note.strip():
            self._draw_node_note(cr, diagram, node, fg, note_fg)

    def _draw_node_note(
        self,
        cr: cairo.Context,
        diagram: Diagram,
        node: Node,
        fg: Tuple[float, float, float],
        note_fg: Tuple[float, float, float],
    ) -> None:
        s = diagram.settings
        note_text = node.note.strip()
        layout = make_pango_layout(cr, note_text, s.font_family, s.note_font_size, line_spacing=s.line_spacing)
        ox, oy, tw, th = get_tight_extents(layout)
        is_up = node.branch_side == "up"

        if s.direction == "vertical":
            if is_up:
                brace_base = node.y - node.h / 2.0 - 3.0
                if node.show_brace:
                    bw = max(node.w * 0.92, min(node.note_w, node.w * 1.15))
                    self._draw_horizontal_brace(
                        cr, node.x, brace_base, bw, max(6.0, s.brace_height - 3.0), fg, s.line_width, upward=True
                    )
                    note_bottom = brace_base - s.brace_height - 2.0
                else:
                    note_bottom = brace_base - 3.0
                cr.save()
                cr.set_source_rgb(*note_fg)
                cr.move_to(node.x - tw / 2.0 - ox, note_bottom - th - oy)
                PangoCairo.show_layout(cr, layout)
                cr.restore()
            else:
                brace_y = node.y + node.h / 2.0 + 3.0
                if node.show_brace:
                    bw = max(node.w * 0.92, min(node.note_w, node.w * 1.15))
                    self._draw_horizontal_brace(
                        cr, node.x, brace_y, bw, max(6.0, s.brace_height - 3.0), fg, s.line_width, upward=False
                    )
                    note_y = brace_y + s.brace_height + 2.0
                else:
                    note_y = brace_y + 3.0
                cr.save()
                cr.set_source_rgb(*note_fg)
                cr.move_to(node.x - tw / 2.0 - ox, note_y - oy)
                PangoCairo.show_layout(cr, layout)
                cr.restore()
        else:
            if is_up:
                brace_x = node.x + node.w / 2.0 + 3.0
                if node.show_brace:
                    bh = max(node.h * 0.9, min(node.note_h, node.h * 1.15))
                    self._draw_vertical_brace(
                        cr, brace_x, node.y, bh, max(6.0, s.brace_height - 3.0), fg, s.line_width, rightward=True
                    )
                    note_x = brace_x + s.brace_height + 4.0
                else:
                    note_x = brace_x + 5.0
                cr.save()
                cr.set_source_rgb(*note_fg)
                cr.move_to(note_x - ox, node.y - th / 2.0 - oy)
                PangoCairo.show_layout(cr, layout)
                cr.restore()
            else:
                brace_x = node.x - node.w / 2.0 - 3.0
                if node.show_brace:
                    bh = max(node.h * 0.9, min(node.note_h, node.h * 1.15))
                    self._draw_vertical_brace(
                        cr, brace_x, node.y, bh, max(6.0, s.brace_height - 3.0), fg, s.line_width, rightward=False
                    )
                    note_x = brace_x - s.brace_height - 4.0 - tw
                else:
                    note_x = brace_x - 5.0 - tw
                cr.save()
                cr.set_source_rgb(*note_fg)
                cr.move_to(note_x - ox, node.y - th / 2.0 - oy)
                PangoCairo.show_layout(cr, layout)
                cr.restore()

    @staticmethod
    def _draw_horizontal_brace(
        cr: cairo.Context,
        cx: float,
        base_y: float,
        width: float,
        height: float,
        color: Tuple[float, float, float],
        lw: float,
        upward: bool = False,
    ) -> None:
        half = width / 2.0
        left = cx - half
        right = cx + half
        sign = -1.0 if upward else 1.0
        mid_y = base_y + sign * height * 0.48
        tip_y = base_y + sign * height
        curl = min(13.0, half * 0.35)

        cr.save()
        cr.set_source_rgb(*color)
        cr.set_line_width(max(1.5, lw * 0.9))
        cr.move_to(left, base_y)
        cr.curve_to(left + curl * 0.25, mid_y, left + curl, mid_y, left + curl * 1.4, mid_y)
        cr.line_to(cx - curl * 0.8, mid_y)
        cr.curve_to(cx - curl * 0.2, mid_y, cx, mid_y + sign * height * 0.18, cx, tip_y)
        cr.curve_to(cx, mid_y + sign * height * 0.18, cx + curl * 0.2, mid_y, cx + curl * 0.8, mid_y)
        cr.line_to(right - curl * 1.4, mid_y)
        cr.curve_to(right - curl, mid_y, right - curl * 0.25, mid_y, right, base_y)
        cr.stroke()
        cr.restore()

    @staticmethod
    def _draw_vertical_brace(
        cr: cairo.Context,
        base_x: float,
        cy: float,
        height: float,
        width: float,
        color: Tuple[float, float, float],
        lw: float,
        rightward: bool = False,
    ) -> None:
        half = height / 2.0
        top = cy - half
        bot = cy + half
        sign = 1.0 if rightward else -1.0
        mid_x = base_x + sign * width * 0.48
        tip_x = base_x + sign * width
        curl = min(11.0, half * 0.38)

        cr.save()
        cr.set_source_rgb(*color)
        cr.set_line_width(max(1.5, lw * 0.9))
        cr.move_to(base_x, top)
        cr.curve_to(mid_x, top + curl * 0.25, mid_x, top + curl, mid_x, top + curl * 1.35)
        cr.line_to(mid_x, cy - curl * 0.75)
        cr.curve_to(mid_x, cy - curl * 0.2, mid_x + sign * width * 0.18, cy, tip_x, cy)
        cr.curve_to(mid_x + sign * width * 0.18, cy, mid_x, cy + curl * 0.2, mid_x, cy + curl * 0.75)
        cr.line_to(mid_x, bot - curl * 1.35)
        cr.curve_to(mid_x, bot - curl, mid_x, bot - curl * 0.25, base_x, bot)
        cr.stroke()
        cr.restore()

    @staticmethod
    def _rounded_rect_path(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
        r = min(r, w / 2.0, h / 2.0)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    @staticmethod
    def _stroke_segment(
        cr: cairo.Context,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        sketchy: bool,
        rng: random.Random,
        amp: float = 1.0,
    ) -> None:
        if not sketchy:
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()
            return
        dist = math.hypot(x2 - x1, y2 - y1)
        if dist < 4.0:
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()
            return
        steps = max(2, int(dist / 36.0))
        pts = [(x1, y1)]
        for i in range(1, steps):
            t = i / steps
            px = x1 + (x2 - x1) * t + rng.uniform(-amp, amp)
            py = y1 + (y2 - y1) * t + rng.uniform(-amp, amp)
            pts.append((px, py))
        pts.append((x2, y2))
        cr.move_to(*pts[0])
        for i in range(1, len(pts)):
            cr.line_to(*pts[i])
        cr.stroke()

    def _stroke_sketchy_box(
        self, cr: cairo.Context, x: float, y: float, w: float, h: float, rounded: bool, rng: random.Random
    ) -> None:
        r = min(12.0 if rounded else 3.0, h / 2.5, w / 2.5)
        j = 1.05
        tl = (x + rng.uniform(-j, j), y + rng.uniform(-j, j))
        tr = (x + w + rng.uniform(-j, j), y + rng.uniform(-j, j))
        br = (x + w + rng.uniform(-j, j), y + h + rng.uniform(-j, j))
        bl = (x + rng.uniform(-j, j), y + h + rng.uniform(-j, j))

        cr.new_path()
        cr.move_to(tl[0] + r, tl[1])
        cr.curve_to(
            x + w * 0.35, y + rng.uniform(-j, j),
            x + w * 0.65, y + rng.uniform(-j, j),
            tr[0] - r, tr[1],
        )
        cr.curve_to(tr[0], tr[1], tr[0], tr[1], tr[0], tr[1] + r)
        cr.curve_to(
            x + w + rng.uniform(-j, j), y + h * 0.5,
            x + w + rng.uniform(-j, j), y + h * 0.6,
            br[0], br[1] - r,
        )
        cr.curve_to(br[0], br[1], br[0], br[1], br[0] - r, br[1])
        cr.curve_to(
            x + w * 0.65, y + h + rng.uniform(-j, j),
            x + w * 0.35, y + h + rng.uniform(-j, j),
            bl[0] + r, bl[1],
        )
        cr.curve_to(bl[0], bl[1], bl[0], bl[1], bl[0], bl[1] - r)
        cr.curve_to(
            x + rng.uniform(-j, j), y + h * 0.6,
            x + rng.uniform(-j, j), y + h * 0.4,
            tl[0], tl[1] + r,
        )
        cr.curve_to(tl[0], tl[1], tl[0], tl[1], tl[0] + r, tl[1])
        cr.stroke()

    @staticmethod
    def _stroke_sketchy_elbow(
        cr: cairo.Context,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        horizontal_first: bool,
        radius: float,
        rng: random.Random,
    ) -> None:
        j = 0.85
        cr.new_path()
        cr.move_to(x1, y1)
        if horizontal_first:
            dx = 1.0 if x2 >= x1 else -1.0
            dy = 1.0 if y2 >= y1 else -1.0
            mx = (x1 + x2) / 2.0
            cr.curve_to(
                mx, y1 + rng.uniform(-j, j),
                mx, y1 + rng.uniform(-j, j),
                x2 - dx * radius, y1 + rng.uniform(-j * 0.5, j * 0.5),
            )
            cr.curve_to(x2, y1, x2, y1, x2, y1 + dy * radius)
            cr.line_to(x2 + rng.uniform(-j * 0.5, j * 0.5), y2)
        else:
            dx = 1.0 if x2 >= x1 else -1.0
            dy = 1.0 if y2 >= y1 else -1.0
            my = (y1 + y2) / 2.0
            cr.curve_to(
                x1 + rng.uniform(-j, j), my,
                x1 + rng.uniform(-j, j), my,
                x1 + rng.uniform(-j * 0.5, j * 0.5), y2 - dy * radius,
            )
            cr.curve_to(x1, y2, x1, y2, x1 + dx * radius, y2)
            cr.line_to(x2, y2 + rng.uniform(-j * 0.5, j * 0.5))
        cr.stroke()

    @staticmethod
    def _draw_arrowhead(cr: cairo.Context, x1: float, y1: float, x2: float, y2: float, size: float = 8.5) -> None:
        angle = math.atan2(y2 - y1, x2 - x1)
        a1 = angle + math.pi - 0.45
        a2 = angle + math.pi + 0.45
        cr.new_path()
        cr.move_to(x2, y2)
        cr.line_to(x2 + size * math.cos(a1), y2 + size * math.sin(a1))
        cr.line_to(x2 + size * math.cos(a2), y2 + size * math.sin(a2))
        cr.close_path()
        cr.fill()


# =============================================================================
# 4. EXPORTER (SVG / PDF / PNG)
# =============================================================================

def export_diagram(
    diagram: Diagram,
    output_path: str,
    bg_mode: str = "white",
    scale: float = 2.5,
) -> None:
    engine = LayoutEngine()
    engine.layout(diagram)
    bx, by, bw, bh = engine.get_bounds(diagram, margin=34.0)
    renderer = DiagramRenderer()
    ext = Path(output_path).suffix.lower()

    if ext == ".png":
        w_px = max(1, int(math.ceil(bw * scale)))
        h_px = max(1, int(math.ceil(bh * scale)))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w_px, h_px)
        cr = cairo.Context(surface)
        cr.scale(scale, scale)
        cr.translate(-bx, -by)
        renderer.render(cr, diagram, bg_mode=bg_mode, bounds=(bx, by, bw, bh))
        surface.write_to_png(output_path)
    elif ext == ".pdf":
        surface = cairo.PDFSurface(output_path, bw, bh)
        cr = cairo.Context(surface)
        cr.translate(-bx, -by)
        renderer.render(cr, diagram, bg_mode=bg_mode, bounds=(bx, by, bw, bh))
        surface.finish()
    else:
        surface = cairo.SVGSurface(output_path, bw, bh)
        cr = cairo.Context(surface)
        cr.translate(-bx, -by)
        renderer.render(cr, diagram, bg_mode=bg_mode, bounds=(bx, by, bw, bh))
        surface.finish()


# =============================================================================
# 5. GNOME DESKTOP LAUNCHER INSTALLER
# =============================================================================

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="26" fill="#182230"/>
  <g stroke="#E6EEF8" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <rect x="40" y="18" width="48" height="24" rx="5" fill="#25364A"/>
    <path d="M64 42 V58 M64 58 H30 V74 M64 58 H98 V74"/>
    <rect x="12" y="74" width="36" height="24" rx="5" fill="#1F3B36"/>
    <rect x="80" y="74" width="36" height="24" rx="5" fill="#3B2E1F"/>
    <path d="M16 104 C20 112 26 110 30 116 C34 110 40 112 44 104" stroke="#60A5FA" stroke-width="3.5"/>
    <path d="M84 104 C88 112 94 110 98 116 C102 110 108 112 112 104" stroke="#FBBF24" stroke-width="3.5"/>
  </g>
</svg>
"""


def install_desktop_entry() -> None:
    script_src = Path(__file__).resolve()
    bin_dir = Path.home() / ".local" / "bin"
    apps_dir = Path.home() / ".local" / "share" / "applications"
    icons_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    bin_dir.mkdir(parents=True, exist_ok=True)
    apps_dir.mkdir(parents=True, exist_ok=True)
    icons_dir.mkdir(parents=True, exist_ok=True)

    target_bin = bin_dir / "mushajjir"
    if script_src != target_bin:
        shutil.copy2(script_src, target_bin)
    target_bin.chmod(0o755)

    icon_path = icons_dir / "mushajjir.svg"
    icon_path.write_text(ICON_SVG, encoding="utf-8")

    desktop_path = apps_dir / "mushajjir.desktop"
    desktop_content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Name[en]=Mushajjir\n"
        "Comment=صانع المخططات التعليمية والتشجير العربي\n"
        f"Exec={target_bin} %F\n"
        "Icon=mushajjir\n"
        "Terminal=false\n"
        "Categories=Education;Graphics;Office;\n"
        "StartupWMClass=org.mushajjir.App\n"
    )
    desktop_path.write_text(desktop_content, encoding="utf-8")
    print(f"تم التثبيت بنجاح:\n  - البرنامج: {target_bin}\n  - الاختصار: {desktop_path}\n  - الأيقونة: {icon_path}")


# =============================================================================
# 6. NATIVE GTK4 ARABIC RTL DESKTOP APPLICATION
# =============================================================================

def build_app_css() -> str:
    swatch_rules = []
    for idx, (_name, h_light, _h_dark) in enumerate(COLOR_SWATCHES):
        bg = h_light if h_light else "#262C36"
        swatch_rules.append(f".swatch-{idx} {{ background: {bg}; }}")
    return (
        """
window {
    font-family: "Noto Sans Arabic", "Vazirmatn", "DejaVu Sans", sans-serif;
}
.sidebar-panel {
    background-color: #161A20;
    padding: 12px;
}
paned > separator {
    min-width: 6px;
    background-color: rgba(255, 255, 255, 0.10);
    transition: background-color 150ms ease;
}
paned > separator:hover {
    background-color: rgba(96, 165, 250, 0.55);
}
.multiline-frame {
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 6px;
    background-color: #202630;
}
.multiline-frame textview,
.multiline-frame textview text {
    background-color: #202630;
    color: #F1F5F9;
}
.section-title {
    font-weight: 700;
    font-size: 13px;
    color: #93C5FD;
    margin-top: 8px;
    margin-bottom: 4px;
}
.hint-label {
    font-size: 11px;
    color: #9CA3AF;
}
.status-bar {
    background-color: #13171D;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    padding: 4px 12px;
    font-size: 12px;
    color: #CBD5E1;
}
.swatch-btn {
    min-width: 24px;
    min-height: 24px;
    padding: 0;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.25);
}
.swatch-btn.active-swatch {
    border: 2px solid #60A5FA;
}
.tab-bar-strip {
    background-color: #12161C;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding: 4px 8px;
}
.tab-item {
    background-color: #1A2029;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 7px;
    padding: 1px 4px;
}
.tab-item.active-tab {
    background-color: #232D3B;
    border: 1px solid rgba(96, 165, 250, 0.72);
}
.tab-title-btn {
    padding: 3px 8px;
    min-height: 24px;
    font-size: 12.5px;
    color: #CBD5E1;
}
.tab-item.active-tab .tab-title-btn {
    color: #F8FAFC;
    font-weight: 600;
}
.tab-close-btn {
    min-width: 20px;
    min-height: 20px;
    padding: 0 4px;
    border-radius: 5px;
    font-size: 12px;
    color: #94A3B8;
}
.tab-close-btn:hover {
    background-color: rgba(248, 113, 113, 0.22);
    color: #FCA5A5;
}
.tab-add-btn {
    min-width: 28px;
    min-height: 26px;
    padding: 0 8px;
    border-radius: 7px;
    font-weight: 700;
}
"""
        + "\n".join(swatch_rules)
    )


def get_textview_text(tv) -> str:
    buf = tv.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


def set_textview_text(tv, text: str) -> None:
    buf = tv.get_buffer()
    cur = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
    if cur != text:
        buf.set_text(text)


def make_multiline_box(Gtk, min_height: int = 64, on_changed=None):
    """Create a clean framed scrollable Gtk.TextView supporting Enter for new lines."""
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(min_height)
    scroll.set_max_content_height(min_height + 40)
    scroll.add_css_class("multiline-frame")

    tv = Gtk.TextView()
    tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    tv.set_accepts_tab(False)
    tv.set_top_margin(6)
    tv.set_bottom_margin(6)
    tv.set_left_margin(8)
    tv.set_right_margin(8)
    if on_changed is not None:
        tv.get_buffer().connect("changed", on_changed)
    scroll.set_child(tv)
    return scroll, tv



# Layout-independent keyboard shortcut resolution (works in Arabic, English, French, etc.)
ARABIC_KEYVAL_TO_LATIN = {
    "Arabic_seen": "s",
    "Arabic_hamzaonyeh": "z",
    "Arabic_ghain": "y",
    "Arabic_khah": "o",
    "Arabic_alefmaksura": "n",
    "Arabic_meem": "l",
    "Arabic_theh": "e",
    "Arabic_sheen": "a",
    "Arabic_hamzaonwaw": "c",
    "Arabic_sukun": "x",
    "Arabic_ra": "v",
    "Arabic_feh": "t",
    "Arabic_sad": "w",
}

LINUX_KEYCODE_TO_LATIN = {
    39: "s",
    52: "z",
    29: "y",
    32: "o",
    57: "n",
    46: "l",
    26: "e",
    38: "a",
    54: "c",
    19: "0",
    28: "t",
    25: "w",
}


def resolve_shortcut_letter(keyval_name: str, keycode: int) -> str:
    """Resolve a physical shortcut key to its lowercase Latin letter regardless of Arabic/Latin layout."""
    low = (keyval_name or "").lower()
    if len(low) == 1 and ("a" <= low <= "z" or "0" <= low <= "9"):
        return low
    if keyval_name in ARABIC_KEYVAL_TO_LATIN:
        return ARABIC_KEYVAL_TO_LATIN[keyval_name]
    if keycode in LINUX_KEYCODE_TO_LATIN:
        return LINUX_KEYCODE_TO_LATIN[keycode]
    return low


def run_gui(initial_files: Optional[List[str]] = None) -> int:
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gtk, Gdk, Gio, GLib  # noqa: E402
    except Exception as exc:
        print(
            "تعذّر تحميل GTK4. على Void Linux نفّذ:\n"
            "  doas xbps-install -S python3-gobject python3-cairo gtk4 pango\n"
            f"التفاصيل: {exc}",
            file=sys.stderr,
        )
        return 1

    Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)

    class MushajjirWindow(Gtk.ApplicationWindow):
        def __init__(self, app: Gtk.Application, open_paths: Optional[List[str]] = None) -> None:
            super().__init__(application=app, title=f"{APP_NAME} — المخططات التعليمية")
            self.set_default_size(1240, 780)

            self.user_config: dict = load_user_config()
            self.layout_engine = LayoutEngine()
            self.renderer = DiagramRenderer()
            self.tabs: List[DiagramTab] = []
            self.active_tab_idx: int = 0
            self._tab_counter: int = 0
            self._pending_close_tab_id: Optional[str] = None

            loaded_any = False
            for p in (open_paths or []):
                norm = os.path.abspath(p)
                if os.path.isfile(norm):
                    try:
                        diag = Diagram.load_json(norm)
                        sel = diag.root_ids[0] if diag.root_ids else None
                        self.tabs.append(
                            DiagramTab(
                                id=uuid.uuid4().hex[:8],
                                diagram=diag,
                                current_file=norm,
                                selected_id=sel,
                                is_starter=False,
                            )
                        )
                        loaded_any = True
                    except Exception:
                        pass

            if not loaded_any:
                starter = create_starter_diagram(
                    settings=get_default_diagram_settings(self.user_config)
                )
                self._tab_counter += 1
                self.tabs.append(
                    DiagramTab(
                        id=uuid.uuid4().hex[:8],
                        diagram=starter,
                        current_file=None,
                        selected_id=starter.root_ids[0] if starter.root_ids else None,
                        is_starter=True,
                        title_fallback=f"مخطط {self._tab_counter}",
                    )
                )

            self.diagram: Diagram = self.tabs[0].diagram
            self.current_file: Optional[str] = self.tabs[0].current_file
            self.undo_stack: List[dict] = self.tabs[0].undo_stack
            self.redo_stack: List[dict] = self.tabs[0].redo_stack
            self.selected_id: Optional[str] = self.tabs[0].selected_id

            self.hover_id: Optional[str] = None
            self.connect_mode: bool = False
            self.connect_src_id: Optional[str] = None
            self.dark_canvas: bool = bool(self.user_config.get("dark_canvas", True))
            self._updating_ui: bool = False

            self.zoom: float = 1.0
            self.pan_x: float = 0.0
            self.pan_y: float = 0.0

            self._drag_mode: Optional[str] = None
            self._drag_node_id: Optional[str] = None
            self._drag_node_start: Tuple[float, float] = (0.0, 0.0)
            self._drag_pan_start: Tuple[float, float] = (0.0, 0.0)
            self._drag_moved: bool = False
            self._active_guides: List[Tuple[str, float, float, float]] = []
            self._context_world_pos: Tuple[float, float] = (0.0, 0.0)

            self.layout_engine.layout(self.diagram)
            self._build_ui(Gtk, Gdk, Gio, GLib)
            self._refresh_tab_bar()
            self._update_window_title()
            self._sync_inspector()
            GLib.idle_add(self._fit_initial_view)

        def _build_ui(self, Gtk, Gdk, Gio, GLib) -> None:
            self._Gtk = Gtk
            self._Gdk = Gdk
            self._Gio = Gio
            self._GLib = GLib

            settings = Gtk.Settings.get_default()
            if settings:
                settings.set_property("gtk-application-prefer-dark-theme", True)
            provider = Gtk.CssProvider()
            provider.load_from_data(build_app_css().encode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

            header = Gtk.HeaderBar()
            self.set_titlebar(header)

            self.btn_sidebar = Gtk.ToggleButton(
                icon_name="sidebar-show-symbolic",
                tooltip_text="إظهار/إخفاء لوحة الخيارات (F9)",
            )
            self.btn_sidebar.set_active(True)
            self.btn_sidebar.connect("toggled", self._on_sidebar_toggled)
            header.pack_start(self.btn_sidebar)

            btn_new = Gtk.Button(icon_name="document-new-symbolic", tooltip_text="مخطط جديد (Ctrl+N)")
            btn_new.connect("clicked", lambda *_: self._new_diagram())
            header.pack_start(btn_new)

            btn_open = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="فتح ملف .msh (Ctrl+O)")
            btn_open.connect("clicked", lambda *_: self._open_file_dialog())
            header.pack_start(btn_open)

            btn_save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="حفظ (Ctrl+S)")
            btn_save.connect("clicked", lambda *_: self._save_file(save_as=False))
            header.pack_start(btn_save)

            header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

            btn_undo = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="تراجع (Ctrl+Z)")
            btn_undo.connect("clicked", lambda *_: self.undo())
            header.pack_start(btn_undo)

            btn_redo = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="إعادة (Ctrl+Y)")
            btn_redo.connect("clicked", lambda *_: self.redo())
            header.pack_start(btn_redo)

            header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

            btn_add_child = Gtk.Button(label="+ فرع ↓", tooltip_text="إضافة فرع للأسفل (Tab)")
            btn_add_child.connect("clicked", lambda *_: self.add_child_to_selected(side=None))
            header.pack_start(btn_add_child)

            btn_add_up = Gtk.Button(label="+ فرع ↑", tooltip_text="إضافة فرع للأعلى / لليمين (Shift+Tab)")
            btn_add_up.connect("clicked", lambda *_: self.add_child_to_selected(side="up"))
            header.pack_start(btn_add_up)

            btn_add_free = Gtk.Button(label="+ مربع حر", tooltip_text="إضافة مربع حر مستقل (Ctrl+Shift+A)")
            btn_add_free.connect("clicked", lambda *_: self.add_free_box())
            header.pack_start(btn_add_free)

            self.btn_connect = Gtk.ToggleButton(label="ربط بسهم", tooltip_text="اربط بين أي مربعين بسهم (C)")
            self.btn_connect.connect("toggled", self._on_connect_toggled)
            header.pack_start(self.btn_connect)

            btn_tidy = Gtk.Button(label="محاذاة تلقائية", tooltip_text="إعادة ترتيب الشجرة تلقائياً (Ctrl+L)")
            btn_tidy.connect("clicked", lambda *_: self.auto_layout_all())
            header.pack_start(btn_tidy)

            btn_export = Gtk.Button(label="تصدير SVG / PDF / PNG", tooltip_text="تصدير المخطط (Ctrl+E)")
            btn_export.add_css_class("suggested-action")
            btn_export.connect("clicked", lambda *_: self._show_export_dialog())
            header.pack_end(btn_export)

            self.btn_canvas_theme = Gtk.ToggleButton(
                label="معاينة ورق أبيض", tooltip_text="التبديل بين خلفية داكنة وخلفية ورقة طباعة بيضاء"
            )
            self.btn_canvas_theme.set_active(not self.dark_canvas)
            self.btn_canvas_theme.connect("toggled", self._on_canvas_theme_toggled)
            header.pack_end(self.btn_canvas_theme)

            btn_fit = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="ملاءمة الشاشة (Ctrl+0)")
            btn_fit.connect("clicked", lambda *_: self.fit_to_view())
            header.pack_end(btn_fit)

            root_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.set_child(root_vbox)

            tab_bar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            tab_bar_row.add_css_class("tab-bar-strip")
            tab_scroll = Gtk.ScrolledWindow()
            tab_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            tab_scroll.set_hexpand(True)
            tab_scroll.set_propagate_natural_height(True)
            self.tab_strip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            tab_scroll.set_child(self.tab_strip_box)
            tab_bar_row.append(tab_scroll)

            btn_new_tab = Gtk.Button(label="+", tooltip_text="لسان جديد لمخطط آخر (Ctrl+T / Ctrl+N)")
            btn_new_tab.add_css_class("flat")
            btn_new_tab.add_css_class("tab-add-btn")
            btn_new_tab.connect("clicked", lambda *_: self._new_diagram())
            tab_bar_row.append(btn_new_tab)
            root_vbox.append(tab_bar_row)

            self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            self.paned.set_vexpand(True)
            self.paned.set_hexpand(True)
            self.paned.set_wide_handle(True)
            self.paned.set_resize_start_child(False)
            self.paned.set_shrink_start_child(True)
            self.paned.set_resize_end_child(True)
            self.paned.set_shrink_end_child(False)
            self.paned.set_position(280)
            root_vbox.append(self.paned)

            self.sidebar_scroll = Gtk.ScrolledWindow()
            self.sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.sidebar_scroll.set_size_request(180, -1)
            self.paned.set_start_child(self.sidebar_scroll)

            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            sidebar.add_css_class("sidebar-panel")
            self.sidebar_scroll.set_child(sidebar)
            self._build_inspector(sidebar)

            self.canvas = Gtk.DrawingArea()
            self.canvas.set_hexpand(True)
            self.canvas.set_vexpand(True)
            self.canvas.set_focusable(True)
            self.canvas.set_draw_func(self._on_draw)
            self.paned.set_end_child(self.canvas)

            click_ctrl = Gtk.GestureClick()
            click_ctrl.set_button(0)
            click_ctrl.connect("pressed", self._on_canvas_pressed)
            self.canvas.add_controller(click_ctrl)

            drag_ctrl = Gtk.GestureDrag()
            drag_ctrl.set_button(0)
            drag_ctrl.connect("drag-begin", self._on_drag_begin)
            drag_ctrl.connect("drag-update", self._on_drag_update)
            drag_ctrl.connect("drag-end", self._on_drag_end)
            self.canvas.add_controller(drag_ctrl)

            motion_ctrl = Gtk.EventControllerMotion()
            motion_ctrl.connect("motion", self._on_motion)
            self.canvas.add_controller(motion_ctrl)

            scroll_ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            scroll_ctrl.connect("scroll", self._on_scroll)
            self.canvas.add_controller(scroll_ctrl)

            key_ctrl = Gtk.EventControllerKey()
            key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            key_ctrl.connect("key-pressed", self._on_key_pressed)
            self.add_controller(key_ctrl)

            self._build_edit_popover()
            self._build_context_popover()

            status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            status_box.add_css_class("status-bar")
            self.status_label = Gtk.Label(xalign=0.0)
            self.status_label.set_hexpand(True)
            status_box.append(self.status_label)
            self.zoom_label = Gtk.Label(label="100%")
            status_box.append(self.zoom_label)
            root_vbox.append(status_box)

            self._update_status("جاهز — استخدم (+ فرع ↑) أو Shift+Tab للتشجير للأعلى، و Tab للأسفل")

        def _build_inspector(self, box) -> None:
            Gtk = self._Gtk

            lbl_node = Gtk.Label(label="المربع المحدد", xalign=0.0)
            lbl_node.add_css_class("section-title")
            box.append(lbl_node)

            box.append(Gtk.Label(label="العنوان داخل الإطار (Enter لسطر جديد):", xalign=0.0))
            title_scroll, self.tv_title = make_multiline_box(Gtk, min_height=46, on_changed=self._on_title_changed)
            box.append(title_scroll)

            box.append(Gtk.Label(label="أمثلة / شرح تحت قوس (Enter لسطر جديد):", xalign=0.0))
            note_scroll, self.tv_note = make_multiline_box(Gtk, min_height=74, on_changed=self._on_note_changed)
            box.append(note_scroll)

            brace_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            brace_row.append(Gtk.Label(label="إظهار القوس الحاصر { :", xalign=0.0, hexpand=True))
            self.sw_brace = Gtk.Switch()
            self.sw_brace.connect("notify::active", self._on_brace_toggled)
            brace_row.append(self.sw_brace)
            box.append(brace_row)

            # Node quick actions
            act_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            b_child = Gtk.Button(label="+ فرع ↓", hexpand=True, tooltip_text="إضافة فرع للأسفل")
            b_child.connect("clicked", lambda *_: self.add_child_to_selected(side="down"))
            b_up = Gtk.Button(label="+ فرع ↑", hexpand=True, tooltip_text="إضافة فرع للأعلى (Shift+Tab)")
            b_up.connect("clicked", lambda *_: self.add_child_to_selected(side="up"))
            b_sib = Gtk.Button(label="+ نظير", hexpand=True, tooltip_text="إضافة نظير في نفس الجهة")
            b_sib.connect("clicked", lambda *_: self.add_sibling_to_selected())
            act_row1.append(b_child)
            act_row1.append(b_up)
            act_row1.append(b_sib)
            box.append(act_row1)

            act_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            b_right = Gtk.Button(label="يمين/أعلى", hexpand=True, tooltip_text="نقل الترتيب لليمين")
            b_right.connect("clicked", lambda *_: self._reorder_selected(-1))
            b_left = Gtk.Button(label="يسار/أسفل", hexpand=True, tooltip_text="نقل الترتيب لليسار")
            b_left.connect("clicked", lambda *_: self._reorder_selected(1))
            b_del = Gtk.Button(label="حذف", tooltip_text="حذف المربع المحدد (Delete)")
            b_del.add_css_class("destructive-action")
            b_del.connect("clicked", lambda *_: self.delete_selected())
            act_row2.append(b_right)
            act_row2.append(b_left)
            act_row2.append(b_del)
            box.append(act_row2)

            # Branch side (above/below parent)
            side_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            side_row.append(Gtk.Label(label="جهة الفرع:", xalign=0.0, hexpand=True))
            self.dd_side = Gtk.DropDown.new_from_strings(["تحت / يسار (عادي)", "فوق / يمين (علوي)"])
            self.dd_side.connect("notify::selected", self._on_branch_side_changed)
            side_row.append(self.dd_side)
            box.append(side_row)

            shape_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            shape_row.append(Gtk.Label(label="شكل الإطار:", xalign=0.0, hexpand=True))
            self.dd_shape = Gtk.DropDown.new_from_strings(["مستطيل", "دائري الحواف", "دائري متقطّع", "بدون إطار"])
            self.dd_shape.connect("notify::selected", self._on_shape_changed)
            shape_row.append(self.dd_shape)
            box.append(shape_row)

            box.append(Gtk.Label(label="لون الخلفية:", xalign=0.0))
            swatch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._swatch_buttons = []
            for idx, (cname, _h_light, _h_dark) in enumerate(COLOR_SWATCHES):
                btn = Gtk.Button(tooltip_text=cname)
                btn.add_css_class("swatch-btn")
                btn.add_css_class(f"swatch-{idx}")
                btn.connect("clicked", lambda _b, i=idx: self._on_swatch_clicked(i))
                swatch_box.append(btn)
                self._swatch_buttons.append(btn)
            box.append(swatch_box)

            self.btn_release_free = Gtk.Button(
                label="إعادة هذا المربع للمحاذاة التلقائية",
                tooltip_text="يتفعّل بعد سحب مربع شجري يدوياً (بالفأرة أو الأسهم) لإعادته إلى موضعه التلقائي في الشجرة",
            )
            self.btn_release_free.connect("clicked", lambda *_: self._release_selected_free())
            box.append(self.btn_release_free)

            self.btn_clear_links = Gtk.Button(label="حذف الأسهم الحرة المرتبطة بهذا المربع")
            self.btn_clear_links.connect("clicked", lambda *_: self._clear_selected_links())
            box.append(self.btn_clear_links)

            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            lbl_diag = Gtk.Label(label="إعدادات المخطط والمربعات", xalign=0.0)
            lbl_diag.add_css_class("section-title")
            box.append(lbl_diag)

            dir_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dir_row.append(Gtk.Label(label="الاتجاه:", xalign=0.0, hexpand=True))
            self.dd_dir = Gtk.DropDown.new_from_strings(["عمودي (فوق وتحت)", "أفقي (يمين ويسار)"])
            self.dd_dir.connect("notify::selected", self._on_dir_changed)
            dir_row.append(self.dd_dir)
            box.append(dir_row)

            style_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            style_row.append(Gtk.Label(label="نمط الرسم:", xalign=0.0, hexpand=True))
            self.dd_style = Gtk.DropDown.new_from_strings(["يدوي (عضوي)", "هندسي نظيف"])
            self.dd_style.connect("notify::selected", self._on_style_changed)
            style_row.append(self.dd_style)
            box.append(style_row)

            conn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            conn_row.append(Gtk.Label(label="شكل الفروع:", xalign=0.0, hexpand=True))
            self.dd_conn = Gtk.DropDown.new_from_strings(["متعامد ناعم", "منحني", "مستقيم"])
            self.dd_conn.connect("notify::selected", self._on_conn_changed)
            conn_row.append(self.dd_conn)
            box.append(conn_row)

            arr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            arr_row.append(Gtk.Label(label="رؤوس أسهم على الفروع:", xalign=0.0, hexpand=True))
            self.sw_arrows = Gtk.Switch()
            self.sw_arrows.connect("notify::active", self._on_arrows_toggled)
            arr_row.append(self.sw_arrows)
            box.append(arr_row)

            self._fonts_list = list_installed_fonts()
            font_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            font_row.append(Gtk.Label(label="الخط العربي:", xalign=0.0, hexpand=True))
            self.dd_font = Gtk.DropDown.new_from_strings(self._fonts_list)
            self.dd_font.connect("notify::selected", self._on_font_changed)
            font_row.append(self.dd_font)
            box.append(font_row)

            fs_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            fs_row.append(Gtk.Label(label="حجم الخط:", xalign=0.0, hexpand=True))
            self.spin_font = Gtk.SpinButton.new_with_range(10, 42, 1)
            self.spin_font.connect("value-changed", self._on_font_size_changed)
            fs_row.append(self.spin_font)
            box.append(fs_row)

            ls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            ls_row.append(Gtk.Label(label="تباعد الأسطر:", xalign=0.0, hexpand=True))
            self.spin_line_sp = Gtk.SpinButton.new_with_range(0, 24, 1)
            self.spin_line_sp.connect("value-changed", self._on_line_spacing_changed)
            ls_row.append(self.spin_line_sp)
            box.append(ls_row)

            py_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            py_row.append(Gtk.Label(label="الفراغ العمودي بالمربع:", xalign=0.0, hexpand=True))
            self.spin_pad_y = Gtk.SpinButton.new_with_range(0, 36, 1)
            self.spin_pad_y.connect("value-changed", self._on_pad_y_changed)
            py_row.append(self.spin_pad_y)
            box.append(py_row)

            px_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            px_row.append(Gtk.Label(label="الفراغ الأفقي بالمربع:", xalign=0.0, hexpand=True))
            self.spin_pad_x = Gtk.SpinButton.new_with_range(4, 50, 2)
            self.spin_pad_x.connect("value-changed", self._on_pad_x_changed)
            px_row.append(self.spin_pad_x)
            box.append(px_row)

            mh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mh_row.append(Gtk.Label(label="أدنى ارتفاع للمربع:", xalign=0.0, hexpand=True))
            self.spin_min_h = Gtk.SpinButton.new_with_range(16, 80, 2)
            self.spin_min_h.connect("value-changed", self._on_min_h_changed)
            mh_row.append(self.spin_min_h)
            box.append(mh_row)

            bh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bh_row.append(Gtk.Label(label="ارتفاع القوس الحاصر:", xalign=0.0, hexpand=True))
            self.spin_brace_h = Gtk.SpinButton.new_with_range(8, 30, 1)
            self.spin_brace_h.connect("value-changed", self._on_brace_h_changed)
            bh_row.append(self.spin_brace_h)
            box.append(bh_row)

            hg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hg_row.append(Gtk.Label(label="التباعد الأفقي:", xalign=0.0, hexpand=True))
            self.spin_hgap = Gtk.SpinButton.new_with_range(6, 140, 2)
            self.spin_hgap.connect("value-changed", self._on_hgap_changed)
            hg_row.append(self.spin_hgap)
            box.append(hg_row)

            vg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            vg_row.append(Gtk.Label(label="التباعد العمودي:", xalign=0.0, hexpand=True))
            self.spin_vgap = Gtk.SpinButton.new_with_range(14, 180, 2)
            self.spin_vgap.connect("value-changed", self._on_vgap_changed)
            vg_row.append(self.spin_vgap)
            box.append(vg_row)

            btn_save_defaults = Gtk.Button(
                label="حفظ الخيارات الحالية كافتراضية",
                tooltip_text="حفظ جميع الخيارات الحالية كخيارات تلقائية للمخططات الجديدة",
            )
            btn_save_defaults.add_css_class("suggested-action")
            btn_save_defaults.connect("clicked", lambda *_: self._save_current_as_defaults())
            box.append(btn_save_defaults)

            btn_reset_defaults = Gtk.Button(
                label="استعادة الإعدادات الأصلية",
                tooltip_text="إلغاء الإعدادات الافتراضية المحفوظة والعودة لإعدادات البرنامج الأصلية",
            )
            btn_reset_defaults.connect("clicked", lambda *_: self._reset_saved_defaults())
            box.append(btn_reset_defaults)

            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            hint = Gtk.Label(
                label="اختصارات سريعة:\n"
                "• Tab: إضافة فرع للأسفل (أو بنفس اتجاه الأب)\n"
                "• Shift+Tab: إضافة فرع للأعلى ↑\n"
                "• Shift+Enter: إضافة نظير\n"
                "• Enter داخل النص: سطر جديد\n"
                "• Ctrl+Enter في النافذة المنبثقة: حفظ\n"
                "• Ctrl+T / Ctrl+N: لسان جديد\n"
                "• Ctrl+W: إغلاق اللسان الحالي\n"
                "• Ctrl+Tab: التنقل بين الألسنة\n"
                "• F9: إظهار/إخفاء لوحة الخيارات\n"
                "• Ctrl+L: إعادة ترتيب الشجرة",
                xalign=0.0,
            )
            hint.add_css_class("hint-label")
            hint.set_wrap(True)
            box.append(hint)

        def _build_edit_popover(self) -> None:
            Gtk = self._Gtk
            Gdk = self._Gdk
            self.edit_popover = Gtk.Popover()
            self.edit_popover.set_parent(self.canvas)
            pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            pbox.set_margin_top(8)
            pbox.set_margin_bottom(8)
            pbox.set_margin_start(10)
            pbox.set_margin_end(10)
            pbox.set_size_request(280, -1)

            def attach_ctrl_enter(widget):
                kc = Gtk.EventControllerKey()
                def on_kp(_c, keyval, _code, state):
                    name = Gdk.keyval_name(keyval) or ""
                    if (state & Gdk.ModifierType.CONTROL_MASK) and name in ("Return", "KP_Enter"):
                        self._apply_popover_edit()
                        return True
                    return False
                kc.connect("key-pressed", on_kp)
                widget.add_controller(kc)

            pbox.append(Gtk.Label(label="العنوان (Enter لسطر جديد):", xalign=0.0))
            pop_title_scroll, self.pop_title = make_multiline_box(Gtk, min_height=44)
            attach_ctrl_enter(self.pop_title)
            pbox.append(pop_title_scroll)

            pbox.append(Gtk.Label(label="أمثلة تحت قوس (Enter لسطر جديد):", xalign=0.0))
            pop_note_scroll, self.pop_note = make_multiline_box(Gtk, min_height=74)
            attach_ctrl_enter(self.pop_note)
            pbox.append(pop_note_scroll)

            btn_ok = Gtk.Button(label="تم (Ctrl+Enter)")
            btn_ok.add_css_class("suggested-action")
            btn_ok.connect("clicked", lambda *_: self._apply_popover_edit())
            pbox.append(btn_ok)

            self.edit_popover.set_child(pbox)

        def _build_context_popover(self) -> None:
            Gtk = self._Gtk
            self.ctx_popover = Gtk.Popover()
            self.ctx_popover.set_parent(self.canvas)
            self.ctx_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.ctx_box.set_margin_top(6)
            self.ctx_box.set_margin_bottom(6)
            self.ctx_box.set_margin_start(6)
            self.ctx_box.set_margin_end(6)
            self.ctx_popover.set_child(self.ctx_box)

        def _open_context_menu(self, sx: float, sy: float, hit_id: Optional[str]) -> None:
            Gtk = self._Gtk
            child = self.ctx_box.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.ctx_box.remove(child)
                child = nxt

            def add_item(label: str, cb, destructive: bool = False) -> None:
                b = Gtk.Button(label=label)
                b.add_css_class("flat")
                if destructive:
                    b.add_css_class("destructive-action")
                b.connect("clicked", lambda *_: (self.ctx_popover.popdown(), cb()))
                self.ctx_box.append(b)

            if hit_id and hit_id in self.diagram.nodes:
                node = self.diagram.nodes[hit_id]
                add_item("تعديل النص والأمثلة", lambda: self._open_edit_popover(hit_id))
                add_item("+ إضافة فرع للأسفل ↓", lambda: self.add_child_to_selected(side="down"))
                add_item("+ إضافة فرع للأعلى ↑", lambda: self.add_child_to_selected(side="up"))
                if not node.is_free:
                    add_item("+ إضافة نظير", self.add_sibling_to_selected)
                if node.parent_id is not None and not node.is_free:
                    flip_lbl = "نقل الفرع للأسفل ↓" if node.branch_side == "up" else "نقل الفرع للأعلى ↑"
                    new_side = "down" if node.branch_side == "up" else "up"
                    add_item(flip_lbl, lambda s=new_side: self._flip_selected_side(s))
                if (not node.is_free) and (node.free_pos is not None):
                    add_item("إعادة للمحاذاة التلقائية", self._release_selected_free)
                add_item(
                    "ربط بسهم مع مربع آخر",
                    lambda: (
                        self.btn_connect.set_active(True),
                        setattr(self, "connect_src_id", hit_id),
                        self.canvas.queue_draw(),
                    ),
                )
                add_item("حذف المربع", self.delete_selected, destructive=True)
            else:
                wx, wy = self.screen_to_world(sx, sy)
                self._context_world_pos = (wx, wy)
                add_item("+ إضافة مربع حر هنا", lambda: self.add_free_box(pos=self._context_world_pos))
                add_item("إعادة محاذاة الشجرة تلقائياً", self.auto_layout_all)
                add_item("ملاءمة المخطط للشاشة", self.fit_to_view)

            rect = self._Gdk.Rectangle()
            rect.x = int(sx)
            rect.y = int(sy)
            rect.width = 1
            rect.height = 1
            self.ctx_popover.set_pointing_to(rect)
            self.ctx_popover.popup()

        def screen_to_world(self, sx: float, sy: float) -> Tuple[float, float]:
            return ((sx - self.pan_x) / self.zoom, (sy - self.pan_y) / self.zoom)

        def world_to_screen(self, wx: float, wy: float) -> Tuple[float, float]:
            return (wx * self.zoom + self.pan_x, wy * self.zoom + self.pan_y)

        def hit_test(self, wx: float, wy: float) -> Optional[str]:
            for node in reversed(list(self.diagram.nodes.values())):
                if (
                    node.x - node.w / 2.0 - 4 <= wx <= node.x + node.w / 2.0 + 4
                    and node.y - node.h / 2.0 - 4 <= wy <= node.y + node.h / 2.0 + 4
                ):
                    return node.id
            return None

        def fit_to_view(self) -> bool:
            w = self.canvas.get_width()
            h = self.canvas.get_height()
            if w < 50 or h < 50:
                return False
            self.layout_engine.layout(self.diagram)
            bx, by, bw, bh = self.layout_engine.get_bounds(self.diagram, margin=50.0)
            if bw <= 0 or bh <= 0:
                return False
            self.zoom = min(1.35, max(0.25, min(w / bw, h / bh)))
            cx = bx + bw / 2.0
            cy = by + bh / 2.0
            self.pan_x = w / 2.0 - cx * self.zoom
            self.pan_y = h / 2.0 - cy * self.zoom
            self.zoom_label.set_text(f"{int(self.zoom * 100)}%")
            self.canvas.queue_draw()
            return False

        def _push_undo(self) -> None:
            self.undo_stack.append(copy.deepcopy(self.diagram.to_dict()))
            if len(self.undo_stack) > 60:
                self.undo_stack.pop(0)
            self.redo_stack.clear()
            self._mark_dirty()

        def undo(self) -> None:
            if not self.undo_stack:
                return
            self.redo_stack.append(copy.deepcopy(self.diagram.to_dict()))
            self.diagram = Diagram.from_dict(self.undo_stack.pop())
            if self.selected_id not in self.diagram.nodes:
                self.selected_id = self.diagram.root_ids[0] if self.diagram.root_ids else None
            self._relayout_and_redraw()
            self._sync_inspector()

        def redo(self) -> None:
            if not self.redo_stack:
                return
            self.undo_stack.append(copy.deepcopy(self.diagram.to_dict()))
            self.diagram = Diagram.from_dict(self.redo_stack.pop())
            if self.selected_id not in self.diagram.nodes:
                self.selected_id = self.diagram.root_ids[0] if self.diagram.root_ids else None
            self._relayout_and_redraw()
            self._sync_inspector()

        def _relayout_and_redraw(self) -> None:
            self.layout_engine.layout(self.diagram)
            self.canvas.queue_draw()

        def _update_status(self, msg: str) -> None:
            self.status_label.set_text(f"{msg}   |   العناصر: {len(self.diagram.nodes)}")

        def _on_draw(self, _area, cr: cairo.Context, width: int, height: int) -> None:
            if self.dark_canvas:
                cr.set_source_rgb(0.067, 0.078, 0.098)
            else:
                cr.set_source_rgb(0.985, 0.985, 0.988)
            cr.rectangle(0, 0, width, height)
            cr.fill()

            self._draw_dot_grid(cr, width, height)

            cr.save()
            cr.translate(self.pan_x, self.pan_y)
            cr.scale(self.zoom, self.zoom)
            self.renderer.render(
                cr,
                self.diagram,
                dark_canvas=self.dark_canvas,
                selected_id=self.selected_id,
                hover_id=self.hover_id,
                connect_src_id=self.connect_src_id,
                guides=self._active_guides,
            )
            cr.restore()

        def _draw_dot_grid(self, cr: cairo.Context, width: int, height: int) -> None:
            step = 28.0 * self.zoom
            if step < 12.0:
                return
            ox = self.pan_x % step
            oy = self.pan_y % step
            if self.dark_canvas:
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.06)
            else:
                cr.set_source_rgba(0.0, 0.0, 0.0, 0.07)
            x = ox
            while x < width:
                y = oy
                while y < height:
                    cr.rectangle(x, y, 1.5, 1.5)
                    y += step
                x += step
            cr.fill()

        def _on_canvas_pressed(self, gesture, n_press: int, x: float, y: float) -> None:
            self.canvas.grab_focus()
            btn = gesture.get_current_button()
            wx, wy = self.screen_to_world(x, y)
            hit = self.hit_test(wx, wy)

            if btn == 3:
                self.selected_id = hit
                self._sync_inspector()
                self.canvas.queue_draw()
                self._open_context_menu(x, y, hit)
                return

            if self.connect_mode:
                if hit:
                    if self.connect_src_id is None:
                        self.connect_src_id = hit
                        self._update_status("اختر المربع الهدف لإتمام الربط بسهم (أو اضغط Esc للإلغاء)")
                    elif hit != self.connect_src_id:
                        self._push_undo()
                        self.diagram.add_link(self.connect_src_id, hit)
                        self.connect_src_id = None
                        self.btn_connect.set_active(False)
                        self._sync_inspector()
                        self._update_status("تم إنشاء سهم ربط بين المربعين")
                    self.canvas.queue_draw()
                return

            self.selected_id = hit
            self._sync_inspector()
            self.canvas.queue_draw()

            if n_press == 2 and hit:
                self._open_edit_popover(hit)

        def _on_drag_begin(self, gesture, start_x: float, start_y: float) -> None:
            btn = gesture.get_current_button()
            if btn == 3:
                self._drag_mode = None
                return
            wx, wy = self.screen_to_world(start_x, start_y)
            hit = self.hit_test(wx, wy)
            self._drag_moved = False
            self._active_guides = []

            if btn == 2 or (btn == 1 and hit is None):
                self._drag_mode = "pan"
                self._drag_pan_start = (self.pan_x, self.pan_y)
            elif btn == 1 and hit is not None and not self.connect_mode:
                self._drag_mode = "node"
                self._drag_node_id = hit
                node = self.diagram.nodes[hit]
                self._drag_node_start = (node.x, node.y)
                self.selected_id = hit
                self._sync_inspector()

        def _on_drag_update(self, _gesture, offset_x: float, offset_y: float) -> None:
            if self._drag_mode == "pan":
                self.pan_x = self._drag_pan_start[0] + offset_x
                self.pan_y = self._drag_pan_start[1] + offset_y
                self.canvas.queue_draw()
            elif self._drag_mode == "node" and self._drag_node_id in self.diagram.nodes:
                if not self._drag_moved and (abs(offset_x) > 3 or abs(offset_y) > 3):
                    self._push_undo()
                    self._drag_moved = True
                if not self._drag_moved:
                    return
                raw_x = self._drag_node_start[0] + offset_x / self.zoom
                raw_y = self._drag_node_start[1] + offset_y / self.zoom
                sx, sy, guides = self.layout_engine.compute_snap(self.diagram, self._drag_node_id, raw_x, raw_y)
                node = self.diagram.nodes[self._drag_node_id]
                node.free_pos = [sx, sy]
                node.x = sx
                node.y = sy
                self._active_guides = guides
                self.canvas.queue_draw()

        def _on_drag_end(self, _gesture, _ox: float, _oy: float) -> None:
            if self._drag_mode == "node" and self._drag_moved:
                self._sync_inspector()
                self._update_status("تم نقل المربع بحرية مع توجيه الفروع تلقائياً")
            self._drag_mode = None
            self._drag_node_id = None
            self._active_guides = []
            self.canvas.queue_draw()

        def _on_motion(self, _ctrl, x: float, y: float) -> None:
            wx, wy = self.screen_to_world(x, y)
            hit = self.hit_test(wx, wy)
            if hit != self.hover_id:
                self.hover_id = hit
                self.canvas.queue_draw()

        def _on_scroll(self, ctrl, _dx: float, dy: float) -> bool:
            state = ctrl.get_current_event_state()
            if state & self._Gdk.ModifierType.CONTROL_MASK:
                factor = 1.12 if dy < 0 else (1.0 / 1.12)
                cx = self.canvas.get_width() / 2.0
                cy = self.canvas.get_height() / 2.0
                wx, wy = self.screen_to_world(cx, cy)
                self.zoom = max(0.2, min(3.5, self.zoom * factor))
                self.pan_x = cx - wx * self.zoom
                self.pan_y = cy - wy * self.zoom
                self.zoom_label.set_text(f"{int(self.zoom * 100)}%")
                self.canvas.queue_draw()
                return True
            else:
                self.pan_y -= dy * 38.0
                self.canvas.queue_draw()
                return True

        def _on_key_pressed(self, _ctrl, keyval: int, keycode: int, state) -> bool:
            Gdk = self._Gdk
            Gtk = self._Gtk
            name = Gdk.keyval_name(keyval) or ""
            letter = resolve_shortcut_letter(name, keycode)
            ctrl_down = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift_down = bool(state & Gdk.ModifierType.SHIFT_MASK)

            # Global shortcuts that work regardless of Arabic/English layout OR text focus
            if name == "F9":
                self.btn_sidebar.set_active(not self.btn_sidebar.get_active())
                return True
            if ctrl_down and letter == "s":
                self._save_file(save_as=shift_down)
                return True
            if ctrl_down and letter == "o":
                self._open_file_dialog()
                return True
            if ctrl_down and letter in ("n", "t"):
                self._new_diagram()
                return True
            if ctrl_down and letter == "w":
                self._request_close_tab(self.active_tab_idx)
                return True
            if ctrl_down and (name == "Page_Down" or (name == "Tab" and not shift_down)):
                self._switch_tab_relative(1)
                return True
            if ctrl_down and (name in ("Page_Up", "ISO_Left_Tab") or (name == "Tab" and shift_down)):
                self._switch_tab_relative(-1)
                return True
            if ctrl_down and letter == "e":
                self._show_export_dialog()
                return True
            if ctrl_down and letter == "l":
                self.auto_layout_all()
                return True
            if ctrl_down and (letter == "0" or name in ("0", "KP_0")):
                self.fit_to_view()
                return True

            # Let TextView / Entry handle typing and text-editing shortcuts (Ctrl+C/V/A/Z)
            focus = self.get_focus()
            if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
                return False

            if name == "Escape":
                self.connect_src_id = None
                self.btn_connect.set_active(False)
                self.canvas.queue_draw()
                return True
            if ctrl_down and letter == "z":
                if shift_down:
                    self.redo()
                else:
                    self.undo()
                return True
            if ctrl_down and letter == "y":
                self.redo()
                return True
            if ctrl_down and shift_down and letter == "a":
                self.add_free_box()
                return True
            if name == "ISO_Left_Tab" or (name == "Tab" and shift_down):
                self.add_child_to_selected(side="up")
                return True
            if name == "Tab":
                self.add_child_to_selected(side=None)
                return True
            if (name == "Return" and shift_down) or name == "Insert":
                self.add_sibling_to_selected()
                return True
            if name in ("Return", "F2") and self.selected_id:
                self._open_edit_popover(self.selected_id)
                return True
            if name in ("Delete", "BackSpace") and self.selected_id:
                self.delete_selected()
                return True
            if letter == "c" and not ctrl_down:
                self.btn_connect.set_active(not self.btn_connect.get_active())
                return True
            if name in ("Left", "Right", "Up", "Down") and self.selected_id:
                step = 2.0 if shift_down else 10.0
                dx = -step if name == "Left" else (step if name == "Right" else 0.0)
                dy = -step if name == "Up" else (step if name == "Down" else 0.0)
                self._push_undo()
                node = self.diagram.nodes[self.selected_id]
                node.free_pos = [node.x + dx, node.y + dy]
                node.x += dx
                node.y += dy
                self._sync_inspector()
                self.canvas.queue_draw()
                return True
            return False

        def _open_edit_popover(self, nid: str) -> None:
            node = self.diagram.nodes.get(nid)
            if not node:
                return
            sx, sy = self.world_to_screen(node.x, node.y)
            rect = self._Gdk.Rectangle()
            rect.x = int(sx)
            rect.y = int(sy)
            rect.width = 1
            rect.height = 1
            self.edit_popover.set_pointing_to(rect)
            set_textview_text(self.pop_title, node.title)
            set_textview_text(self.pop_note, node.note)
            self.edit_popover.popup()
            self.pop_title.grab_focus()
            buf = self.pop_title.get_buffer()
            buf.select_range(buf.get_start_iter(), buf.get_end_iter())

        def _apply_popover_edit(self) -> None:
            if self.selected_id and self.selected_id in self.diagram.nodes:
                self._push_undo()
                node = self.diagram.nodes[self.selected_id]
                node.title = get_textview_text(self.pop_title)
                node.note = get_textview_text(self.pop_note)
                self._relayout_and_redraw()
                self._sync_inspector()
            self.edit_popover.popdown()
            self.canvas.grab_focus()

        def add_child_to_selected(self, side: Optional[str] = None) -> None:
            self._push_undo()
            parent_id = (
                self.selected_id
                if (self.selected_id in self.diagram.nodes)
                else (self.diagram.root_ids[0] if self.diagram.root_ids else None)
            )
            node = self.diagram.add_node("فرع جديد", parent_id=parent_id, branch_side=side)
            self.selected_id = node.id
            self._relayout_and_redraw()
            self._sync_inspector()
            self._open_edit_popover(node.id)

        def add_sibling_to_selected(self) -> None:
            if not self.selected_id or self.selected_id not in self.diagram.nodes:
                self.add_child_to_selected()
                return
            sel = self.diagram.nodes[self.selected_id]
            if sel.is_free:
                self.add_free_box()
                return
            self._push_undo()
            node = self.diagram.add_node("عنصر جديد", parent_id=sel.parent_id, branch_side=sel.branch_side)
            self.selected_id = node.id
            self._relayout_and_redraw()
            self._sync_inspector()
            self._open_edit_popover(node.id)

        def _flip_selected_side(self, side: str) -> None:
            if not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            node = self.diagram.nodes[self.selected_id]
            if node.parent_id is None or node.is_free:
                return
            self._push_undo()
            self.diagram.set_subtree_side(node.id, side)
            self._relayout_and_redraw()
            self._sync_inspector()
            self._update_status("تم تغيير جهة الفرع (فوق/تحت)")

        def add_free_box(self, pos: Optional[Tuple[float, float]] = None) -> None:
            self._push_undo()
            if pos is None:
                cx = self.canvas.get_width() / 2.0
                cy = self.canvas.get_height() / 2.0
                wx, wy = self.screen_to_world(cx, cy)
                pos = (wx + random.uniform(-20, 20), wy + random.uniform(-20, 20))
            node = self.diagram.add_node(
                "مربع حر",
                is_free=True,
                free_pos=[pos[0], pos[1]],
                fill_idx=1,
            )
            self.selected_id = node.id
            self._relayout_and_redraw()
            self._sync_inspector()
            self._open_edit_popover(node.id)
            self._update_status("تمت إضافة مربع حر — اسحبه لأي موضع")

        def delete_selected(self) -> None:
            if not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            node = self.diagram.nodes[self.selected_id]
            next_sel = node.parent_id
            self._push_undo()
            self.diagram.delete_node(self.selected_id)
            self.selected_id = (
                next_sel
                if next_sel in self.diagram.nodes
                else (self.diagram.root_ids[0] if self.diagram.root_ids else None)
            )
            self._relayout_and_redraw()
            self._sync_inspector()
            self._update_status("تم الحذف")

        def auto_layout_all(self) -> None:
            self._push_undo()
            self.diagram.reset_all_free_positions()
            self._relayout_and_redraw()
            self._sync_inspector()
            self.fit_to_view()
            self._update_status("تمت إعادة محاذاة الشجرة تلقائياً")

        def _reorder_selected(self, delta: int) -> None:
            if not self.selected_id:
                return
            self._push_undo()
            if self.diagram.move_sibling(self.selected_id, delta):
                self._relayout_and_redraw()

        def _release_selected_free(self) -> None:
            if not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            node = self.diagram.nodes[self.selected_id]
            if node.is_free:
                return
            self._push_undo()
            node.free_pos = None
            self._relayout_and_redraw()
            self._sync_inspector()
            self._update_status("أُعيد المربع إلى المحاذاة التلقائية")

        def _clear_selected_links(self) -> None:
            if not self.selected_id:
                return
            self._push_undo()
            removed = self.diagram.clear_links_for_node(self.selected_id)
            self._sync_inspector()
            self.canvas.queue_draw()
            if removed:
                self._update_status(f"تم حذف {removed} سهم/أسهم ربط حرة")

        def _sync_inspector(self) -> None:
            self._updating_ui = True
            s = self.diagram.settings
            node = self.diagram.nodes.get(self.selected_id) if self.selected_id else None

            self.tv_title.set_sensitive(node is not None)
            self.tv_note.set_sensitive(node is not None)
            self.sw_brace.set_sensitive(node is not None)
            self.dd_shape.set_sensitive(node is not None)
            self.dd_side.set_sensitive(node is not None and node.parent_id is not None and not node.is_free)

            if node:
                set_textview_text(self.tv_title, node.title)
                set_textview_text(self.tv_note, node.note)
                self.sw_brace.set_active(node.show_brace)
                self.dd_side.set_selected(1 if node.branch_side == "up" else 0)
                shapes = ["rect", "rounded", "dashed", "none"]
                self.dd_shape.set_selected(shapes.index(node.shape) if node.shape in shapes else 0)
                self.btn_release_free.set_sensitive((not node.is_free) and (node.free_pos is not None))
                self.btn_clear_links.set_sensitive(self.diagram.has_links_for_node(node.id))
                for idx, btn in enumerate(self._swatch_buttons):
                    if idx == node.fill_idx:
                        btn.add_css_class("active-swatch")
                    else:
                        btn.remove_css_class("active-swatch")
            else:
                set_textview_text(self.tv_title, "")
                set_textview_text(self.tv_note, "")
                self.btn_release_free.set_sensitive(False)
                self.btn_clear_links.set_sensitive(False)

            self.dd_dir.set_selected(0 if s.direction == "vertical" else 1)
            self.dd_style.set_selected(0 if s.style == "sketchy" else 1)
            conns = ["orthogonal", "curved", "straight"]
            self.dd_conn.set_selected(conns.index(s.connector) if s.connector in conns else 0)
            self.sw_arrows.set_active(s.show_arrows)
            if s.font_family in self._fonts_list:
                self.dd_font.set_selected(self._fonts_list.index(s.font_family))
            self.spin_font.set_value(s.font_size)
            self.spin_line_sp.set_value(s.line_spacing)
            self.spin_pad_y.set_value(s.box_pad_y)
            self.spin_pad_x.set_value(s.box_pad_x)
            self.spin_min_h.set_value(s.min_box_h)
            self.spin_brace_h.set_value(s.brace_height)
            self.spin_hgap.set_value(s.h_gap)
            self.spin_vgap.set_value(s.v_gap)

            self._updating_ui = False

        def _on_sidebar_toggled(self, btn) -> None:
            self.sidebar_scroll.set_visible(btn.get_active())

        def _on_title_changed(self, _buf) -> None:
            if self._updating_ui or not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            self.diagram.nodes[self.selected_id].title = get_textview_text(self.tv_title)
            self._mark_dirty()
            self._relayout_and_redraw()

        def _on_note_changed(self, _buf) -> None:
            if self._updating_ui or not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            self.diagram.nodes[self.selected_id].note = get_textview_text(self.tv_note)
            self._mark_dirty()
            self._relayout_and_redraw()

        def _on_brace_toggled(self, sw, _pspec) -> None:
            if self._updating_ui or not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            self._push_undo()
            self.diagram.nodes[self.selected_id].show_brace = sw.get_active()
            self._relayout_and_redraw()

        def _on_branch_side_changed(self, dd, _pspec) -> None:
            if self._updating_ui or not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            side = "up" if dd.get_selected() == 1 else "down"
            self._flip_selected_side(side)

        def _on_shape_changed(self, dd, _pspec) -> None:
            if self._updating_ui or not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            shapes = ["rect", "rounded", "dashed", "none"]
            self._push_undo()
            self.diagram.nodes[self.selected_id].shape = shapes[dd.get_selected()]
            self._relayout_and_redraw()

        def _on_swatch_clicked(self, idx: int) -> None:
            if not self.selected_id or self.selected_id not in self.diagram.nodes:
                return
            self._push_undo()
            self.diagram.nodes[self.selected_id].fill_idx = idx
            self._sync_inspector()
            self.canvas.queue_draw()

        def _on_dir_changed(self, dd, _pspec) -> None:
            if self._updating_ui:
                return
            self._push_undo()
            self.diagram.settings.direction = "vertical" if dd.get_selected() == 0 else "horizontal"
            self.diagram.reset_all_free_positions()
            self._relayout_and_redraw()
            self.fit_to_view()

        def _on_style_changed(self, dd, _pspec) -> None:
            if self._updating_ui:
                return
            self._push_undo()
            self.diagram.settings.style = "sketchy" if dd.get_selected() == 0 else "clean"
            self.canvas.queue_draw()

        def _on_conn_changed(self, dd, _pspec) -> None:
            if self._updating_ui:
                return
            self._push_undo()
            self.diagram.settings.connector = ["orthogonal", "curved", "straight"][dd.get_selected()]
            self.canvas.queue_draw()

        def _on_arrows_toggled(self, sw, _pspec) -> None:
            if self._updating_ui:
                return
            self._push_undo()
            self.diagram.settings.show_arrows = sw.get_active()
            self.canvas.queue_draw()

        def _on_font_changed(self, dd, _pspec) -> None:
            if self._updating_ui:
                return
            idx = dd.get_selected()
            if 0 <= idx < len(self._fonts_list):
                self.diagram.settings.font_family = self._fonts_list[idx]
                self._relayout_and_redraw()

        def _on_font_size_changed(self, spin) -> None:
            if self._updating_ui:
                return
            val = int(spin.get_value())
            self.diagram.settings.font_size = val
            self.diagram.settings.note_font_size = max(11, val - 3)
            self._relayout_and_redraw()

        def _on_line_spacing_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.line_spacing = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_pad_y_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.box_pad_y = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_pad_x_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.box_pad_x = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_min_h_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.min_box_h = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_brace_h_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.brace_height = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_hgap_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.h_gap = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_vgap_changed(self, spin) -> None:
            if self._updating_ui:
                return
            self.diagram.settings.v_gap = float(spin.get_value())
            self._relayout_and_redraw()

        def _on_connect_toggled(self, btn) -> None:
            self.connect_mode = btn.get_active()
            self.connect_src_id = None
            if self.connect_mode:
                self._update_status("وضع الربط: اختر المربع الأول ثم المربع الثاني")
            else:
                self._update_status("تم إنهاء وضع الربط")
            self.canvas.queue_draw()

        def _on_canvas_theme_toggled(self, btn) -> None:
            self.dark_canvas = not btn.get_active()
            self.canvas.queue_draw()

        def _get_last_dir(self) -> Optional[str]:
            last_dir = self.user_config.get("last_dir")
            if isinstance(last_dir, str) and last_dir and os.path.isdir(last_dir):
                return last_dir
            if self.current_file:
                parent = str(Path(self.current_file).parent)
                if os.path.isdir(parent):
                    return parent
            return None

        def _remember_dir_for_path(self, file_path: str) -> None:
            try:
                folder = str(Path(file_path).resolve().parent)
                if os.path.isdir(folder):
                    self.user_config["last_dir"] = folder
                    save_user_config(self.user_config)
            except Exception:
                pass

        def _apply_initial_folder(self, dlg) -> None:
            folder = self._get_last_dir()
            if folder and hasattr(dlg, "set_initial_folder"):
                try:
                    dlg.set_initial_folder(self._Gio.File.new_for_path(folder))
                except Exception:
                    pass

        def _save_current_as_defaults(self) -> None:
            self.user_config["default_settings"] = self.diagram.settings.to_dict()
            self.user_config["dark_canvas"] = self.dark_canvas
            if save_user_config(self.user_config):
                self._update_status("تم حفظ جميع الخيارات الحالية كخيارات تلقائية للمخططات الجديدة")
            else:
                self._update_status("تعذّر حفظ الإعدادات الافتراضية")

        def _reset_saved_defaults(self) -> None:
            self.user_config.pop("default_settings", None)
            self.user_config.pop("dark_canvas", None)
            save_user_config(self.user_config)
            self._push_undo()
            self.diagram.settings = DiagramSettings()
            self.dark_canvas = True
            self.btn_canvas_theme.set_active(False)
            self._relayout_and_redraw()
            self._sync_inspector()
            self._update_status("تمت استعادة الإعدادات الأصلية للبرنامج")

        def _current_tab(self) -> DiagramTab:
            return self.tabs[self.active_tab_idx]

        def _fit_initial_view(self) -> bool:
            self.fit_to_view()
            if self.tabs:
                self._current_tab().view_initialized = True
                self._save_active_tab_state()
            return False

        def _save_active_tab_state(self) -> None:
            if not self.tabs or not (0 <= self.active_tab_idx < len(self.tabs)):
                return
            tab = self.tabs[self.active_tab_idx]
            tab.diagram = self.diagram
            tab.current_file = self.current_file
            tab.undo_stack = self.undo_stack
            tab.redo_stack = self.redo_stack
            tab.selected_id = self.selected_id
            tab.zoom = self.zoom
            tab.pan_x = self.pan_x
            tab.pan_y = self.pan_y

        def _load_active_tab_state(self) -> None:
            tab = self.tabs[self.active_tab_idx]
            self.diagram = tab.diagram
            self.current_file = tab.current_file
            self.undo_stack = tab.undo_stack
            self.redo_stack = tab.redo_stack
            self.selected_id = tab.selected_id
            self.zoom = tab.zoom
            self.pan_x = tab.pan_x
            self.pan_y = tab.pan_y

        def _mark_dirty(self) -> None:
            if not self.tabs:
                return
            tab = self._current_tab()
            changed = (not tab.dirty) or tab.is_starter
            tab.dirty = True
            tab.is_starter = False
            if changed:
                self._refresh_tab_bar()
                self._update_window_title()

        def _update_window_title(self) -> None:
            if not self.tabs:
                self.set_title(f"{APP_NAME} — المخططات التعليمية")
                return
            tab = self._current_tab()
            mark = "• " if tab.dirty else ""
            self.set_title(f"{mark}{tab.display_name()} — {APP_NAME}")

        def _refresh_tab_bar(self) -> None:
            Gtk = self._Gtk
            child = self.tab_strip_box.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.tab_strip_box.remove(child)
                child = nxt

            for idx, tab in enumerate(self.tabs):
                item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
                item.add_css_class("tab-item")
                if idx == self.active_tab_idx:
                    item.add_css_class("active-tab")

                mark = "• " if tab.dirty else ""
                btn_title = Gtk.Button(label=f"{mark}{tab.display_name()}")
                btn_title.add_css_class("flat")
                btn_title.add_css_class("tab-title-btn")
                if tab.current_file:
                    btn_title.set_tooltip_text(tab.current_file)
                btn_title.connect("clicked", lambda _b, i=idx: self._switch_to_tab(i))
                item.append(btn_title)

                btn_close = Gtk.Button(label="×", tooltip_text="إغلاق اللسان (Ctrl+W)")
                btn_close.add_css_class("flat")
                btn_close.add_css_class("tab-close-btn")
                btn_close.connect("clicked", lambda _b, i=idx: self._request_close_tab(i))
                item.append(btn_close)

                self.tab_strip_box.append(item)

        def _switch_to_tab(self, idx: int) -> None:
            if not (0 <= idx < len(self.tabs)):
                return
            if idx != self.active_tab_idx:
                self._save_active_tab_state()
                self.active_tab_idx = idx
            self._load_active_tab_state()
            self.connect_src_id = None
            if hasattr(self, "btn_connect") and self.btn_connect.get_active():
                self.btn_connect.set_active(False)
            self.layout_engine.layout(self.diagram)
            self._sync_inspector()
            self._refresh_tab_bar()
            self._update_window_title()
            tab = self._current_tab()
            if not tab.view_initialized:
                self.fit_to_view()
                tab.view_initialized = True
                self._save_active_tab_state()
            else:
                self.zoom_label.set_text(f"{int(self.zoom * 100)}%")
                self.canvas.queue_draw()

        def _switch_tab_relative(self, delta: int) -> None:
            if len(self.tabs) <= 1:
                return
            nxt = (self.active_tab_idx + delta) % len(self.tabs)
            self._switch_to_tab(nxt)

        def _make_blank_tab(self) -> DiagramTab:
            diag = Diagram(settings=get_default_diagram_settings(self.user_config))
            r = diag.add_node("العنوان الرئيسي")
            diag.add_node("القسم الأول", parent_id=r.id, note="مثال 1\nمثال 2")
            diag.add_node("القسم الثاني", parent_id=r.id, note="مثال 3")
            self._tab_counter += 1
            return DiagramTab(
                id=uuid.uuid4().hex[:8],
                diagram=diag,
                current_file=None,
                selected_id=r.id,
                dirty=False,
                is_starter=False,
                view_initialized=False,
                title_fallback=f"مخطط {self._tab_counter}",
            )

        def open_file_in_tab(self, path: str) -> None:
            norm = os.path.abspath(path)
            if not os.path.isfile(norm):
                self._update_status(f"الملف غير موجود: {path}")
                return

            self._save_active_tab_state()
            for idx, tab in enumerate(self.tabs):
                if tab.current_file and os.path.abspath(tab.current_file) == norm:
                    self._switch_to_tab(idx)
                    self._update_status(f"الملف مفتوح مسبقاً: {Path(norm).name}")
                    return

            try:
                diag = Diagram.load_json(norm)
            except Exception as exc:
                self._update_status(f"تعذّر فتح الملف: {exc}")
                return

            new_tab = DiagramTab(
                id=uuid.uuid4().hex[:8],
                diagram=diag,
                current_file=norm,
                selected_id=diag.root_ids[0] if diag.root_ids else None,
                dirty=False,
                is_starter=False,
                view_initialized=False,
            )

            if (
                len(self.tabs) == 1
                and self.tabs[0].is_starter
                and not self.tabs[0].dirty
                and self.tabs[0].current_file is None
            ):
                self.tabs[0] = new_tab
                self.active_tab_idx = 0
                self._switch_to_tab(0)
            else:
                self.tabs.append(new_tab)
                self._switch_to_tab(len(self.tabs) - 1)
            self._update_status(f"تم فتح: {Path(norm).name}")

        def _request_close_tab(self, idx: int) -> None:
            if not (0 <= idx < len(self.tabs)):
                return
            self._save_active_tab_state()
            tab = self.tabs[idx]
            if not tab.dirty:
                self._close_tab_now(idx)
                return

            Gtk = self._Gtk
            win = Gtk.Window(transient_for=self, modal=True, title="تغييرات غير محفوظة")
            win.set_default_size(360, 140)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_margin_top(16)
            vbox.set_margin_bottom(14)
            vbox.set_margin_start(16)
            vbox.set_margin_end(16)
            win.set_child(vbox)

            lbl = Gtk.Label(
                label=f"يوجد تعديلات غير محفوظة في «{tab.display_name()}». هل تريد حفظها قبل الإغلاق؟",
                xalign=0.0,
            )
            lbl.set_wrap(True)
            vbox.append(lbl)

            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_cancel = Gtk.Button(label="إلغاء", hexpand=True)
            btn_cancel.connect("clicked", lambda *_: win.close())

            btn_discard = Gtk.Button(label="إغلاق بدون حفظ", hexpand=True)
            btn_discard.add_css_class("destructive-action")

            def on_discard(*_):
                win.close()
                for i, t in enumerate(self.tabs):
                    if t.id == tab.id:
                        self._close_tab_now(i)
                        break

            btn_discard.connect("clicked", on_discard)

            btn_save_close = Gtk.Button(label="حفظ وإغلاق", hexpand=True)
            btn_save_close.add_css_class("suggested-action")

            def on_save_close(*_):
                win.close()
                for i, t in enumerate(self.tabs):
                    if t.id == tab.id:
                        self._switch_to_tab(i)
                        self._pending_close_tab_id = tab.id
                        self._save_file(save_as=False)
                        break

            btn_save_close.connect("clicked", on_save_close)
            btn_row.append(btn_cancel)
            btn_row.append(btn_discard)
            btn_row.append(btn_save_close)
            vbox.append(btn_row)
            win.present()

        def _close_tab_now(self, idx: int) -> None:
            if not (0 <= idx < len(self.tabs)):
                return
            if len(self.tabs) == 1:
                self.tabs[0] = self._make_blank_tab()
                self.active_tab_idx = 0
                self._switch_to_tab(0)
                self._update_status("تم فتح مخطط جديد")
                return

            self._save_active_tab_state()
            self.tabs.pop(idx)
            if idx < self.active_tab_idx:
                self.active_tab_idx -= 1
            elif idx == self.active_tab_idx:
                self.active_tab_idx = min(idx, len(self.tabs) - 1)
            self._switch_to_tab(self.active_tab_idx)

        def _complete_pending_close(self, tab_id: str) -> None:
            if self._pending_close_tab_id != tab_id:
                return
            self._pending_close_tab_id = None
            for i, t in enumerate(self.tabs):
                if t.id == tab_id:
                    self._close_tab_now(i)
                    break

        def _new_diagram(self) -> None:
            self._save_active_tab_state()
            new_tab = self._make_blank_tab()
            self.tabs.append(new_tab)
            self._switch_to_tab(len(self.tabs) - 1)
            self._update_status(f"تم فتح لسان جديد: {new_tab.display_name()}")

        def _open_file_dialog(self) -> None:
            Gtk = self._Gtk
            Gio = self._Gio
            if hasattr(Gtk, "FileDialog"):
                dlg = Gtk.FileDialog(title="فتح مخطط مُشجِّر (.msh)")
                self._apply_initial_folder(dlg)
                flt = Gtk.FileFilter()
                flt.set_name("مخططات مُشجِّر (*.msh)")
                flt.add_pattern("*.msh")
                store = Gio.ListStore.new(Gtk.FileFilter)
                store.append(flt)
                dlg.set_filters(store)
                dlg.open(self, None, self._on_open_done)
            else:
                self._update_status("افتح الملف بتمريره كوسيط في الطرفية: mushajjir file.msh")

        def _on_open_done(self, dlg, res) -> None:
            try:
                gfile = dlg.open_finish(res)
                if gfile and gfile.get_path():
                    self.open_file_in_tab(gfile.get_path())
            except Exception:
                pass

        def _save_file(self, save_as: bool = False) -> None:
            tab = self._current_tab()
            if self.current_file and not save_as:
                self.diagram.save_json(self.current_file)
                tab.current_file = self.current_file
                tab.dirty = False
                tab.is_starter = False
                self._remember_dir_for_path(self.current_file)
                self._refresh_tab_bar()
                self._update_window_title()
                self._update_status(f"تم الحفظ: {Path(self.current_file).name}")
                self._complete_pending_close(tab.id)
                return
            Gtk = self._Gtk
            if hasattr(Gtk, "FileDialog"):
                dlg = Gtk.FileDialog(title="حفظ مخطط مُشجِّر")
                self._apply_initial_folder(dlg)
                init_name = Path(self.current_file).name if self.current_file else f"{tab.display_name()}.msh"
                dlg.set_initial_name(init_name)
                dlg.save(self, None, lambda d, r, tid=tab.id: self._on_save_done(d, r, tid))

        def _on_save_done(self, dlg, res, tab_id: str) -> None:
            try:
                gfile = dlg.save_finish(res)
                if gfile and gfile.get_path():
                    path = os.path.abspath(gfile.get_path())
                    if not path.endswith(".msh"):
                        path += ".msh"
                    target_tab = next((t for t in self.tabs if t.id == tab_id), None)
                    if target_tab is None:
                        return
                    if self._current_tab().id == tab_id:
                        self.diagram.save_json(path)
                        self.current_file = path
                    else:
                        target_tab.diagram.save_json(path)
                    target_tab.current_file = path
                    target_tab.dirty = False
                    target_tab.is_starter = False
                    self._remember_dir_for_path(path)
                    self._refresh_tab_bar()
                    self._update_window_title()
                    self._update_status(f"تم الحفظ: {Path(path).name}")
                    self._complete_pending_close(tab_id)
                else:
                    self._pending_close_tab_id = None
            except Exception:
                self._pending_close_tab_id = None

        def _show_export_dialog(self) -> None:
            Gtk = self._Gtk
            win = Gtk.Window(transient_for=self, modal=True, title="تصدير المخطط")
            win.set_default_size(360, 210)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            vbox.set_margin_top(14)
            vbox.set_margin_bottom(14)
            vbox.set_margin_start(16)
            vbox.set_margin_end(16)
            win.set_child(vbox)

            fmt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            fmt_row.append(Gtk.Label(label="الصيغة:", xalign=0.0, hexpand=True))
            dd_fmt = Gtk.DropDown.new_from_strings(["SVG (متجهي نقي)", "PDF (جاهز للطباعة)", "PNG (صورة عالية الدقة 3x)"])
            fmt_row.append(dd_fmt)
            vbox.append(fmt_row)

            bg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bg_row.append(Gtk.Label(label="الخلفية:", xalign=0.0, hexpand=True))
            dd_bg = Gtk.DropDown.new_from_strings(["أبيض (للكتب والطباعة)", "شفاف", "داكن"])
            bg_row.append(dd_bg)
            vbox.append(bg_row)

            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_cancel = Gtk.Button(label="إلغاء", hexpand=True)
            btn_cancel.connect("clicked", lambda *_: win.close())
            btn_go = Gtk.Button(label="اختيار مكان الحفظ...", hexpand=True)
            btn_go.add_css_class("suggested-action")

            def on_go(*_):
                exts = [".svg", ".pdf", ".png"]
                bgs = ["white", "transparent", "dark"]
                ext = exts[dd_fmt.get_selected()]
                bg = bgs[dd_bg.get_selected()]
                win.close()
                if hasattr(Gtk, "FileDialog"):
                    dlg = Gtk.FileDialog(title=f"تصدير ({ext.upper()})")
                    self._apply_initial_folder(dlg)
                    stem = Path(self.current_file).stem if self.current_file else "mushajjir"
                    dlg.set_initial_name(f"{stem}{ext}")
                    dlg.save(self, None, lambda d, r: self._on_export_save(d, r, ext, bg))

            btn_go.connect("clicked", on_go)
            btn_row.append(btn_cancel)
            btn_row.append(btn_go)
            vbox.append(btn_row)
            win.present()

        def _on_export_save(self, dlg, res, ext: str, bg: str) -> None:
            try:
                gfile = dlg.save_finish(res)
                if gfile and gfile.get_path():
                    path = gfile.get_path()
                    if not path.lower().endswith(ext):
                        path += ext
                    export_diagram(self.diagram, path, bg_mode=bg, scale=3.0)
                    self._remember_dir_for_path(path)
                    self._update_status(f"تم التصدير بنجاح: {Path(path).name}")
            except Exception as exc:
                self._update_status(f"خطأ أثناء التصدير: {exc}")

    class MushajjirApp(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id=APP_ID,
                flags=Gio.ApplicationFlags.HANDLES_OPEN,
            )

        def do_activate(self) -> None:
            win = self.props.active_window
            if not win:
                win = MushajjirWindow(self)
            win.present()

        def do_open(self, files, n_files: int, hint: str) -> None:
            paths = [gf.get_path() for gf in files if gf and gf.get_path()]
            win = self.props.active_window
            if not win:
                win = MushajjirWindow(self, open_paths=paths)
            else:
                for p in paths:
                    win.open_file_in_tab(p)
            win.present()

    app = MushajjirApp()
    argv = [sys.argv[0]] + [os.path.abspath(p) for p in (initial_files or [])]
    return app.run(argv)


# =============================================================================
# 7. CLI ENTRYPOINT
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mushajjir",
        description="مُشَجِّر — برنامج خفيف لصناعة المخططات التعليمية والتشجير العربي (GTK4 + Cairo + Pango)",
    )
    parser.add_argument("files", nargs="*", help="ملف أو ملفات مخطط .msh لفتحها أو تصديرها")
    parser.add_argument("--export", metavar="OUT", help="تصدير مباشر بدون واجهة إلى .svg أو .pdf أو .png")
    parser.add_argument("--bg", choices=["white", "transparent", "dark"], default="white", help="خلفية التصدير")
    parser.add_argument("--style", choices=["sketchy", "clean"], help="تجاوز نمط الرسم عند التصدير")
    parser.add_argument("--dir", choices=["vertical", "horizontal"], help="تجاوز اتجاه المخطط عند التصدير")
    parser.add_argument("--scale", type=float, default=2.5, help="دقة التصدير لصيغة PNG (افتراضي 2.5)")
    parser.add_argument("--install-desktop", action="store_true", help="تثبيت البرنامج وأيقونته في قائمة GNOME")
    args = parser.parse_args()

    if args.install_desktop:
        install_desktop_entry()
        return 0

    if args.export:
        first_file = args.files[0] if args.files else None
        diag = Diagram.load_json(first_file) if first_file else create_starter_diagram()
        if args.style:
            diag.settings.style = args.style
        if args.dir:
            diag.settings.direction = args.dir
        export_diagram(diag, args.export, bg_mode=args.bg, scale=args.scale)
        print(f"Exported: {args.export}")
        return 0

    return run_gui(initial_files=args.files)


if __name__ == "__main__":
    sys.exit(main())