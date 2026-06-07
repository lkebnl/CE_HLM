"""
FEMB PCB Layout Viewer — interactive dual-side viewer with per-category filtering.

Usage:
  python3 layout_viz.py          # interactive, all components
  python3 layout_viz.py --save   # save PNG snapshots and exit

Controls:
  [⟳ Flip Board]  — toggle front / back view
  Checkboxes      — show/hide individual component categories
  [All] / [None]  — check all or uncheck all at once
"""

import re, math, argparse
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle
from matplotlib.transforms import Affine2D
from matplotlib.widgets import Button, CheckButtons

# ─────────────────────────────────────────────────────────────────────────────
# Paths & scale
# ─────────────────────────────────────────────────────────────────────────────
BASE     = Path(__file__).parent / "FEMB_PADS_EXPORT"
LIB_FILE = BASE / "07_libraries/footprints/footprints_from_pcb_ascii_source.asc"
PCB_FILE = BASE / "02_pcb/pcb_ascii/pcb_layout_ascii.asc"
KYN_FILE = BASE / "03_netlist/keyin_netlist.kyn"

SCALE = 1 / 1_500_000          # PADS internal units → mm

# ─────────────────────────────────────────────────────────────────────────────
# Component categories — order determines checkbox order in sidebar
# ─────────────────────────────────────────────────────────────────────────────
CATEGORIES = [
    ("coldata",   "COLDATA ×2"),
    ("larasic",   "LArASIC ×8"),
    ("coldadc",   "ColdADC ×8"),
    ("ldo",       "LDO ×11"),
    ("switch",    "Switch ×13"),
    ("connector", "Connector"),
    ("capacitor", "Capacitor"),
    ("resistor",  "Resistor"),
    ("inductor",  "Inductor"),
    ("diode",     "Diode"),
    ("other",     "Other"),
]
CAT_KEYS = [k for k, _ in CATEGORIES]
CAT_LBLS = [l for _, l in CATEGORIES]

# ─────────────────────────────────────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────────────────────────────────────
BG_FIG  = "#ffffff"
BG_AXES = "#f5f5f5"
GHOST_C = "#aaaaaa"

COMP_COLORS = {
    "coldata":   "#c62828",   # deep red
    "larasic":   "#1565c0",   # deep blue
    "coldadc":   "#8e24aa",   # purple
    "ldo":       "#e65100",   # burnt orange
    "switch":    "#6a1b9a",   # dark purple
    "connector": "#00695c",   # dark teal
    "capacitor": "#37474f",   # dark blue-grey
    "resistor":  "#78909c",   # medium blue-grey
    "inductor":  "#795548",   # brown
    "diode":     "#2e7d32",   # dark green
    "other":     "#90a4ae",
}


def classify(parttype):
    fp = parttype.upper()
    if "COLDDATA"  in fp or "LQFP216" in fp: return "coldata"
    if "LAR_ASIC"  in fp:                     return "larasic"
    if "COLD_ADC"  in fp:                     return "coldadc"
    if "TPS74201"  in fp:                     return "ldo"
    if "NLASB"     in fp or "SC-88"  in fp:   return "switch"
    if any(x in fp for x in ("MTG", "MTBLOCK")): return "mechanical"
    if any(x in fp for x in ("SSW", "0757", "IPL1", "FIDUCIAL")): return "connector"
    if fp.startswith("CC") or "CAPC" in fp:   return "capacitor"
    if fp.startswith("CR") or "RESC" in fp:   return "resistor"
    if fp.startswith("5508"):                 return "inductor"
    if "SOT23" in fp or "SOT-23" in fp:       return "diode"
    return "other"


# ─────────────────────────────────────────────────────────────────────────────
# Parse footprint library → {decal_name: {outline, pads}}
# ─────────────────────────────────────────────────────────────────────────────

def _parse_decal_block(lines):
    """Return (outline_polys, pads) for one PARTDECAL block.

    Two-pass approach handles both inline-PAD and separated-PAD formats.
    outline_polys : list of ([(x,y)…], level_int)   — all mm, local coords
    pads          : list of {pin, x, y, w, h, ori}  — all mm, local coords
    """
    outlines, pads = [], []

    # ── Pass 1: collect all PAD shape definitions ──────────────────────────
    pad_shapes = {}   # idx → {w, h, ori}  (mm)
    for k, l in enumerate(lines):
        pm = re.match(r'^PAD\s+(\d+)\s+(\d+)', l.strip())
        if pm:
            idx = int(pm.group(1))
            for j in range(k + 1, min(k + 4, len(lines))):
                sl = lines[j].strip()
                rfm = re.match(r'-?\d+\s+(\d+)\s+RF\s+([\d.]+)\s+(\d+)', sl)
                if rfm:
                    pad_shapes[idx] = {
                        'w': int(rfm.group(1)) * SCALE,
                        'ori': float(rfm.group(2)),
                        'h': int(rfm.group(3)) * SCALE,
                    }
                    break
                sm = re.match(r'-?\d+\s+(\d+)\s+[SR]\b', sl)
                if sm:
                    sz = int(sm.group(1)) * SCALE
                    pad_shapes[idx] = {'w': sz, 'ori': 0.0, 'h': sz}
                    break

    def _best(d): return next(iter(d.values())) if d else None

    h_shape   = _best({i: s for i, s in pad_shapes.items() if abs(s['ori'] - 90.0) < 1.0})
    v_shape   = _best({i: s for i, s in pad_shapes.items() if abs(s['ori']) < 1.0})
    big_shape = (max(pad_shapes.values(), key=lambda s: s['w'] * s['h'])
                 if pad_shapes else None)
    only_shape = ((h_shape or v_shape)
                  if pad_shapes and len({round(s['ori']) for s in pad_shapes.values()}) == 1
                  else None)

    # ── Pass 2: outlines and terminals ────────────────────────────────────
    i = 0
    while i < len(lines):
        l = lines[i].strip()

        m = re.match(r'^(OPEN|CLOSED)\s+(\d+)\s+\d+\s+\d+\s+(\d+)', l)
        if m:
            n, level = int(m.group(2)), int(m.group(3))
            pts = []
            for _ in range(n):
                i += 1
                if i < len(lines):
                    xy = lines[i].split()
                    if len(xy) >= 2:
                        try: pts.append((int(xy[0]) * SCALE, int(xy[1]) * SCALE))
                        except ValueError: pass
            if pts: outlines.append((pts, level))
            i += 1; continue

        tm = re.match(r'^T(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)', l)
        if tm:
            v1  = int(tm.group(1))
            pin = int(tm.group(5))

            if i + 1 < len(lines) and lines[i + 1].strip().startswith('PAD'):
                tx, ty = v1 * SCALE, int(tm.group(2)) * SCALE
                sl = lines[i + 2].strip() if i + 2 < len(lines) else ""
                rfm = re.match(r'-?\d+\s+(\d+)\s+RF\s+([\d.]+)\s+(\d+)', sl)
                if rfm:
                    pw = int(rfm.group(1)) * SCALE
                    pori = float(rfm.group(2))
                    ph = int(rfm.group(3)) * SCALE
                else:
                    sm = re.match(r'-?\d+\s+(\d+)\s+[SR]\b', sl)
                    pw = ph = int(sm.group(1)) * SCALE if sm else 0.4
                    pori = 0.0
                pads.append({"pin": pin, "x": tx, "y": ty, "w": pw, "h": ph, "ori": pori})
                i += 1; continue

            tx, ty = v1 * SCALE, int(tm.group(2)) * SCALE
            if only_shape:
                shape = only_shape
            elif tx == 0.0 and ty == 0.0:
                shape = big_shape or v_shape or h_shape
            elif abs(tx) >= abs(ty):
                shape = h_shape or v_shape
            else:
                shape = v_shape or h_shape

            if shape: pw, ph, pori = shape['w'], shape['h'], shape['ori']
            else: pw = ph = 0.4; pori = 0.0
            pads.append({"pin": pin, "x": tx, "y": ty, "w": pw, "h": ph, "ori": pori})
        i += 1
    return outlines, pads


def parse_library(lib_path):
    text = lib_path.read_text(errors='replace')
    m = re.search(r'\*PARTDECAL\*.*?\n', text)
    if not m: return {}
    sec = text[m.end():]
    m2  = re.search(r'\*PARTTYPE\*', sec)
    if m2: sec = sec[:m2.start()]
    headers = list(re.finditer(r'^([A-Z0-9_.:/\-]+)\s+[IM]\s+\d+', sec, re.MULTILINE))
    result = {}
    for k, hdr in enumerate(headers):
        name  = hdr.group(1).strip()
        start = hdr.end()
        end   = headers[k+1].start() if k+1 < len(headers) else len(sec)
        outlines, pads = _parse_decal_block(sec[start:end].split('\n'))
        result[name] = {"outline": outlines, "pads": pads}
    return result


def parse_parttypes(pcb_path):
    text = pcb_path.read_text(errors='replace')
    sec  = text[text.index('*PARTTYPE*'):text.index('*PART*')]
    pat  = r'(CAP|RES|DIO|CON|UND|IND|FID|QFP|TTL|HOL)'
    return {m.group(1): m.group(2)
            for m in re.finditer(rf'^(\S+)\s+(\S+)\s+{pat}', sec, re.MULTILINE)}


def parse_parts(pcb_path):
    text  = pcb_path.read_text(errors='replace')
    sec   = text[text.index('*PART*'):text.index('*ROUTE*')]
    parts = []
    for m in re.finditer(
        r'^([A-Za-z][A-Za-z0-9_]*)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+([\d.]+)\s+[UG]\s+([NYM])',
        sec, re.MULTILINE
    ):
        parts.append({
            "refdes":   m.group(1),
            "parttype": m.group(2),
            "x":        int(m.group(3)) * SCALE,
            "y":        int(m.group(4)) * SCALE,
            "ori":      float(m.group(5)),
            "is_back":  m.group(6) in ('Y', 'M'),
        })
    return parts


def parse_pin_nets(kyn_path):
    """KYN netlist → {refdes: {pin_str: net_name}}"""
    text = kyn_path.read_text(errors='replace')
    result, cur_net = {}, None
    for line in text.split('\n'):
        s = line.strip()
        if not s: continue
        if s[0] == '\\':
            m = re.match(r'^\\(.+?)\\', s)
            if m: cur_net = m.group(1)
        for m in re.finditer(r'\\([A-Za-z]\w*)\\-\\(\w+)\\', s):
            refdes, pin = m.group(1), m.group(2)
            result.setdefault(refdes, {})[pin] = cur_net
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Geometry — always computed in FRONT-VIEW world coordinates
# ─────────────────────────────────────────────────────────────────────────────

def _local_to_world(lx, ly, cx, cy, ori_deg, mirror_x):
    if mirror_x: lx = -lx
    r  = math.radians(ori_deg)
    wx = cx + lx * math.cos(r) - ly * math.sin(r)
    wy = cy + lx * math.sin(r) + ly * math.cos(r)
    return wx, wy


def fp_world_pads(fp, cx, cy, ori, is_back):
    out = []
    for p in fp["pads"]:
        wx, wy = _local_to_world(p["x"], p["y"], cx, cy, ori, is_back)
        wori   = p["ori"] + (ori if not is_back else -ori)
        out.append((wx, wy, p["w"], p["h"], wori))
    return out


def fp_world_outlines(fp, cx, cy, ori, is_back, levels=(0, 20, 26)):
    polys = []
    for pts, level in fp["outline"]:
        if level not in levels or len(pts) < 2: continue
        polys.append([_local_to_world(lx, ly, cx, cy, ori, is_back) for lx, ly in pts])
    return polys


def fp_bbox(fp, cx, cy, ori, is_back):
    all_xy = [xy for pts, _ in fp["outline"]
                 for xy in [_local_to_world(lx, ly, cx, cy, ori, is_back) for lx, ly in pts]]
    if not all_xy: return cx-.5, cy-.5, cx+.5, cy+.5
    xs = [p[0] for p in all_xy]; ys = [p[1] for p in all_xy]
    return min(xs), min(ys), max(xs), max(ys)


# ─────────────────────────────────────────────────────────────────────────────
# View transform (post-step for back view)
# ─────────────────────────────────────────────────────────────────────────────

def _flip_pads(pads, board_cx):
    return [(2*board_cx - wx, wy, pw, ph, -wori) for wx, wy, pw, ph, wori in pads]

def _flip_polys(polys, board_cx):
    return [[(2*board_cx - x, y) for x, y in poly] for poly in polys]


# ─────────────────────────────────────────────────────────────────────────────
# Drawing primitives
# ─────────────────────────────────────────────────────────────────────────────

def draw_pad(ax, wx, wy, pw, ph, wori, color, alpha=0.90):
    if pw <= 0 or ph <= 0: pw = ph = 0.25
    r = Rectangle((-pw/2, -ph/2), pw, ph,
                  linewidth=0, facecolor=color, alpha=alpha)
    r.set_transform(Affine2D().rotate_deg(wori).translate(wx, wy) + ax.transData)
    ax.add_patch(r)


def draw_active(ax, pads_v, polys_v, label_xy, color, label_text):
    for poly in polys_v:
        if poly:
            xs, ys = zip(*poly)
            ax.plot(xs, ys, color=color, lw=0.6, alpha=0.6)
    for wx, wy, pw, ph, wori in pads_v:
        draw_pad(ax, wx, wy, pw, ph, wori, color)
    if label_text:
        ax.text(*label_xy, label_text, color='black', fontsize=3.5,
                ha='center', va='center', fontweight='bold', clip_on=True, zorder=10)


def draw_pin_labels(ax, pads_v, fp_pads, pin_nets, cx_v, cy_v, fontsize=1.5):
    for (wx, wy, pw, ph, wori), pad in zip(pads_v, fp_pads):
        pin_s = str(pad["pin"])
        net   = pin_nets.get(pin_s)
        if not net: continue
        lbl = f"{pin_s}:{net}"
        dx, dy = wx - cx_v, wy - cy_v
        dist = math.hypot(dx, dy)
        if dist < 0.01: continue
        off = max(pw, ph) * 0.55 + 0.05
        tx  = wx + (dx / dist) * off
        ty  = wy + (dy / dist) * off
        if abs(dx) >= abs(dy):
            rot = 0; ha = 'left' if dx > 0 else 'right'; va = 'center'
        else:
            rot = 90; ha = 'center'; va = 'bottom' if dy > 0 else 'top'
        ax.text(tx, ty, lbl, fontsize=fontsize, rotation=rot,
                rotation_mode='anchor', ha=ha, va=va,
                color='#222222', clip_on=True, zorder=9)


def draw_ghost(ax, pads_v, polys_v, bbox_v):
    if polys_v:
        for poly in polys_v:
            if poly:
                xs, ys = zip(*poly)
                ax.plot(xs, ys, color=GHOST_C, lw=1.5,
                        linestyle='--', alpha=0.55, dash_capstyle='round')
    else:
        x0, y0, x1, y1 = bbox_v
        ax.add_patch(Rectangle((x0, y0), x1-x0, y1-y0,
                                linewidth=1.5, edgecolor=GHOST_C,
                                facecolor='none', linestyle='--', alpha=0.45))


# ─────────────────────────────────────────────────────────────────────────────
# Build component list
# ─────────────────────────────────────────────────────────────────────────────

PIN_LABEL_CLASSES = {"coldata", "larasic", "coldadc"}


def build_comp_list(lib, ptmap, parts, all_pin_nets=None):
    comps, missing = [], set()
    for p in parts:
        cls = classify(p["parttype"])
        if cls == "mechanical": continue
        fp_key = ptmap.get(p["parttype"], p["parttype"])
        fp = lib.get(fp_key) or lib.get(p["parttype"])
        if fp is None:
            missing.add(p["parttype"]); continue
        prefix   = re.match(r'^([A-Za-z]+)', p["refdes"]).group(1)
        pin_nets = {}
        if all_pin_nets and cls in PIN_LABEL_CLASSES:
            pin_nets = all_pin_nets.get(p["refdes"], {})
        comps.append({**p,
                      "fp":       fp,
                      "prefix":   prefix,
                      "color":    COMP_COLORS.get(cls, COMP_COLORS["other"]),
                      "label":    p["refdes"] if prefix in ("U","J","P") else None,
                      "pin_nets": pin_nets,
                      "cls":      cls})
    if missing:
        print(f"  No footprint for: {missing}")
    return comps


# ─────────────────────────────────────────────────────────────────────────────
# Interactive viewer
# ─────────────────────────────────────────────────────────────────────────────

class PCBViewer:

    def __init__(self, comps):
        self.comps_all  = comps
        self.view_back  = False
        self.active_cats = set(CAT_KEYS)   # all enabled by default
        self._cache     = {}

        xs = [c["x"] for c in comps]
        self._bcx = (min(xs) + max(xs)) / 2
        print(f"  Board X range: {min(xs):.1f} … {max(xs):.1f} mm  "
              f"(centre {self._bcx:.1f} mm)")

        self._build_figure()
        self._redraw()

    # ── Figure ────────────────────────────────────────────────────────────────

    def _build_figure(self):
        self.fig = plt.figure(figsize=(28, 20), facecolor=BG_FIG)

        # Main plot — shifted right to leave room for sidebar
        self.ax = self.fig.add_axes([0.19, 0.09, 0.79, 0.88])
        self.ax.set_facecolor(BG_AXES)
        self.ax.set_aspect('equal')
        for sp in self.ax.spines.values():
            sp.set_edgecolor('#cccccc')
        self.ax.tick_params(colors='#444444')
        self.ax.set_xlabel("X (mm)"); self.ax.set_ylabel("Y (mm)")

        # ── Sidebar: category checkboxes ──────────────────────────────────
        ax_chk = self.fig.add_axes([0.01, 0.20, 0.165, 0.76],
                                   facecolor='#f0f0f0')
        ax_chk.set_title("Components", fontsize=8, pad=4, color='#333333')

        self.checks = CheckButtons(
            ax_chk, CAT_LBLS, [True] * len(CAT_LBLS)
        )
        # Colour each label to match its component colour
        for lbl_obj, (key, _) in zip(self.checks.labels, CATEGORIES):
            lbl_obj.set_color(COMP_COLORS.get(key, '#333333'))
            lbl_obj.set_fontsize(8.5)
        # Make check-mark rectangles a bit more visible
        try:
            for rect in self.checks.rectangles:
                rect.set_edgecolor('#555555')
                rect.set_linewidth(1.2)
        except AttributeError:
            pass
        self.checks.on_clicked(self._on_check)

        # ── All / None buttons ────────────────────────────────────────────
        ax_all  = self.fig.add_axes([0.01,  0.11, 0.078, 0.045])
        ax_none = self.fig.add_axes([0.097, 0.11, 0.078, 0.045])
        self.btn_all  = Button(ax_all,  'All',
                               color='#e0e0e0', hovercolor='#c8e6c9')
        self.btn_none = Button(ax_none, 'None',
                               color='#e0e0e0', hovercolor='#ffcdd2')
        for b in (self.btn_all, self.btn_none):
            b.label.set_fontsize(8); b.label.set_color('#222222')
        self.btn_all.on_clicked(self._on_all)
        self.btn_none.on_clicked(self._on_none)

        # ── Flip button ───────────────────────────────────────────────────
        ax_btn = self.fig.add_axes([0.44, 0.02, 0.14, 0.05])
        self.btn_flip = Button(ax_btn, '⟳  Flip to Back',
                               color='#eeeeee', hovercolor='#dddddd')
        self.btn_flip.label.set_color('#222222')
        self.btn_flip.label.set_fontsize(9)
        self.btn_flip.on_clicked(self._on_flip)

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_flip(self, _):
        self.view_back = not self.view_back
        self.btn_flip.label.set_text(
            '⟳  Flip to Front' if self.view_back else '⟳  Flip to Back')
        self._redraw()

    def _on_check(self, lbl_text):
        for key, display in CATEGORIES:
            if display == lbl_text:
                if key in self.active_cats: self.active_cats.discard(key)
                else:                       self.active_cats.add(key)
                break
        self._cache.clear()
        self._redraw()

    def _on_all(self, _):
        status = self.checks.get_status()
        for i, active in enumerate(status):
            if not active:
                self.checks.set_active(i)
        self.active_cats = set(CAT_KEYS)
        self._cache.clear()
        self._redraw()

    def _on_none(self, _):
        status = self.checks.get_status()
        for i, active in enumerate(status):
            if active:
                self.checks.set_active(i)
        self.active_cats = set()
        self._cache.clear()
        self._redraw()

    # ── Component filter ──────────────────────────────────────────────────────

    def _filtered(self):
        key = frozenset(self.active_cats)
        if key not in self._cache:
            self._cache[key] = [c for c in self.comps_all
                                 if c["cls"] in self.active_cats]
        return self._cache[key]

    # ── Redraw ────────────────────────────────────────────────────────────────

    def _redraw(self):
        ax   = self.ax
        ax.cla()
        ax.set_facecolor(BG_AXES)
        ax.set_aspect('equal')
        ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)")
        ax.tick_params(colors='#444444')

        vback = self.view_back
        bcx   = self._bcx

        for c in self._filtered():
            fp      = c["fp"]
            cx, cy  = c["x"], c["y"]
            ori     = c["ori"]
            is_back = c["is_back"]
            color   = c["color"]
            label   = c["label"]

            pads_fw  = fp_world_pads(fp, cx, cy, ori, is_back)
            polys_fw = fp_world_outlines(fp, cx, cy, ori, is_back)
            bbox_fw  = fp_bbox(fp, cx, cy, ori, is_back)
            lx_fw    = cx

            if vback:
                pads_v  = _flip_pads(pads_fw, bcx)
                polys_v = _flip_polys(polys_fw, bcx)
                bbox_v  = (2*bcx - bbox_fw[2], bbox_fw[1],
                           2*bcx - bbox_fw[0], bbox_fw[3])
                lx_v    = 2*bcx - lx_fw
            else:
                pads_v, polys_v, bbox_v, lx_v = pads_fw, polys_fw, bbox_fw, lx_fw

            active = (is_back == vback)
            if active:
                draw_active(ax, pads_v, polys_v, (lx_v, cy), color, label)
                if c.get("pin_nets"):
                    fs = 1.3 if c["cls"] == "coldata" else 1.5
                    draw_pin_labels(ax, pads_v, fp["pads"],
                                    c["pin_nets"], lx_v, cy, fontsize=fs)
            else:
                draw_ghost(ax, pads_v, polys_v, bbox_v)

        # Title
        side  = "BACK ◀" if vback else "▶ FRONT"
        shown = len(self.active_cats)
        total = len(CAT_KEYS)
        ax.set_title(
            f"FEMB PCB Layout  —  [{side}]  —  {shown}/{total} categories",
            color='#222222', fontsize=11, pad=8)

        # Legend (only shown categories)
        handles = []
        for key, lbl in CATEGORIES:
            if key in self.active_cats:
                handles.append(mpatches.Patch(
                    color=COMP_COLORS.get(key, COMP_COLORS["other"]), label=lbl))
        handles.append(mpatches.Patch(
            facecolor='none', edgecolor=GHOST_C, linestyle='--', linewidth=1.5,
            label="Ghost (other side)"))
        ax.legend(handles=handles, loc='upper right', fontsize=7,
                  facecolor='white', edgecolor='#cccccc',
                  labelcolor='#222222', framealpha=0.95)

        self.fig.canvas.draw_idle()

    # ── Save PNGs ─────────────────────────────────────────────────────────────

    def save_snapshots(self):
        base = Path(__file__).parent
        configs = [
            ("front_All",  False, set(CAT_KEYS)),
            ("back_All",   True,  set(CAT_KEYS)),
            ("front_ICs",  False, {"coldata","larasic","coldadc","ldo","switch","connector"}),
            ("back_ICs",   True,  {"coldata","larasic","coldadc","ldo","switch","connector"}),
        ]
        for tag, back, cats in configs:
            self.view_back   = back
            self.active_cats = cats
            # Sync checkboxes to match active_cats
            for i, key in enumerate(CAT_KEYS):
                cur = self.checks.get_status()[i]
                want = key in cats
                if cur != want:
                    self.checks.set_active(i)
            self._cache.clear()
            self._redraw()
            out = base / f"layout_{tag}.png"
            self.fig.savefig(out, dpi=180, bbox_inches='tight',
                             facecolor=self.fig.get_facecolor())
            print(f"  Saved → {out}")
        # Reset to all
        self.view_back   = False
        self.active_cats = set(CAT_KEYS)
        for i in range(len(CAT_KEYS)):
            if not self.checks.get_status()[i]:
                self.checks.set_active(i)
        self._cache.clear()
        self._redraw()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FEMB PCB Layout Viewer")
    parser.add_argument("--save", action="store_true",
                        help="Save PNG snapshots and exit")
    args = parser.parse_args()

    if args.save:
        matplotlib.use('Agg')

    print("Loading footprint library …")
    lib = parse_library(LIB_FILE)
    print(f"  {len(lib)} footprints")

    print("Loading PARTTYPE map …")
    ptmap = parse_parttypes(PCB_FILE)

    print("Loading component placements …")
    parts = parse_parts(PCB_FILE)
    print(f"  {len(parts)} placements")

    print("Loading pin→net mapping from KYN …")
    all_pin_nets = parse_pin_nets(KYN_FILE)
    print(f"  {len(all_pin_nets)} components with net assignments")

    print("Building component list …")
    comps = build_comp_list(lib, ptmap, parts, all_pin_nets)
    print(f"  {len(comps)} renderable components")

    viewer = PCBViewer(comps)

    if args.save:
        viewer.save_snapshots()
    else:
        plt.show()


if __name__ == "__main__":
    main()
