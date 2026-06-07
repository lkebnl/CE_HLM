"""
FEMB PCB Layout Viewer — interactive dual-side viewer with selection highlighting.

Usage:
  python3 layout_viz.py          # interactive, all components
  python3 layout_viz.py --save   # save PNG snapshots and exit

Controls:
  [⟳ Flip Board]     — toggle front / back view
  Checkboxes (left)  — show/hide component categories
  [All] / [None]     — check all or uncheck all
  Left-click IC/J/P  — select: peer ICs brighten, pathway R/C appear,
                       connected pads highlight in gold/orange
  Left-click empty   — deselect
"""

import re, math, argparse, json
from pathlib import Path
from collections import defaultdict

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle, FancyBboxPatch
from matplotlib.transforms import Affine2D
from matplotlib.widgets import Button, CheckButtons

# ─────────────────────────────────────────────────────────────────────────────
# Paths & scale
# ─────────────────────────────────────────────────────────────────────────────
BASE      = Path(__file__).parent / "FEMB_PADS_EXPORT"
LIB_FILE  = BASE / "07_libraries/footprints/footprints_from_pcb_ascii_source.asc"
PCB_FILE  = BASE / "02_pcb/pcb_ascii/pcb_layout_ascii.asc"
KYN_FILE  = BASE / "03_netlist/keyin_netlist.kyn"
CONN_FILE = Path(__file__).parent / "femb_u_connections.json"

SCALE = 1 / 1_500_000

# ─────────────────────────────────────────────────────────────────────────────
# Component categories
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

# Passive-type prefixes (used for pathway R/C detection)
PASSIVE_PREFIXES = {'R', 'C', 'L', 'D'}

# Nets to always skip — too global to be useful in selection context
SKIP_NETS    = {'GND'}
SKIP_FUNCS   = {'pwr_gnd', 'bypass_cap', 'nc'}   # per-JSON func field
SKIP_NET_PFX = ('$',)                              # internal single-cap nets

# Hard-coded LDO → (power_rail_nets, downstream_ASICs).
# KYN doesn't capture VOUT→copper-pour connections; derived from schematic.
LDO_POWER_MAP = {
    'U8':  (['VDDP_FE'],              ['U3','U7','U11','U17']),   # FE-Left  VDDP
    'U9':  (['VDDA_FE'],              ['U3','U7','U11','U17']),   # FE-Left  VDDA
    'U16': (['VDDP_FE'],              ['U19','U21','U23','U25']), # FE-Right VDDP
    'U15': (['VDDA_FE'],              ['U19','U21','U23','U25']), # FE-Right VDDA
    'U27': (['ADC_VDDA2P5'],          ['U6','U10','U13','U18']),  # ADC-Left  2.5V
    'U5':  (['ADC_VDDD1P2'],          ['U6','U10','U13','U18']),  # ADC-Left  1.1V
    'U12': (['ADC_VDDA2P5'],          ['U20','U22','U24','U26']), # ADC-Right 2.5V
    'U14': (['ADC_VDDD1P2'],          ['U20','U22','U24','U26']), # ADC-Right 1.1V
    'U4':  (['CD_VDDIO'],             ['U1','U2']),               # COLDATA 2.25V IO
    'U28': (['CD_VDDA'],              ['U1','U2']),               # COLDATA 1.2V analog
    'U29': (['CD_VDDD','CD_VDDCORE'], ['U1','U2']),               # COLDATA 1.1V digital
}

# GND pad colour (light green — no connection lines drawn)
GND_PAD_C = "#a5d6a7"

# ─────────────────────────────────────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────────────────────────────────────
BG_FIG  = "#ffffff"
BG_AXES = "#f5f5f5"
GHOST_C = "#aaaaaa"

# Selection highlight colours
SEL_BOX_C   = "#FFD700"   # gold border around selected component
SEL_PAD_C   = "#FFD700"   # gold pad on selected component
PEER_PAD_C  = "#FF8C00"   # orange pad on peer IC

COMP_COLORS = {
    "coldata":   "#c62828",
    "larasic":   "#1565c0",
    "coldadc":   "#8e24aa",
    "ldo":       "#e65100",
    "switch":    "#6a1b9a",
    "connector": "#00695c",
    "capacitor": "#37474f",
    "resistor":  "#78909c",
    "inductor":  "#795548",
    "diode":     "#2e7d32",
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
# Parse footprint library
# ─────────────────────────────────────────────────────────────────────────────

def _parse_decal_block(lines):
    outlines, pads = [], []
    pad_shapes = {}
    for k, l in enumerate(lines):
        pm = re.match(r'^PAD\s+(\d+)\s+(\d+)', l.strip())
        if pm:
            idx = int(pm.group(1))
            for j in range(k + 1, min(k + 4, len(lines))):
                sl = lines[j].strip()
                rfm = re.match(r'-?\d+\s+(\d+)\s+RF\s+([\d.]+)\s+(\d+)', sl)
                if rfm:
                    pad_shapes[idx] = {'w': int(rfm.group(1))*SCALE,
                                       'ori': float(rfm.group(2)),
                                       'h': int(rfm.group(3))*SCALE}
                    break
                sm = re.match(r'-?\d+\s+(\d+)\s+[SR]\b', sl)
                if sm:
                    sz = int(sm.group(1))*SCALE
                    pad_shapes[idx] = {'w': sz, 'ori': 0.0, 'h': sz}
                    break

    def _best(d): return next(iter(d.values())) if d else None
    h_shape   = _best({i: s for i, s in pad_shapes.items() if abs(s['ori']-90.0) < 1.0})
    v_shape   = _best({i: s for i, s in pad_shapes.items() if abs(s['ori']) < 1.0})
    big_shape = (max(pad_shapes.values(), key=lambda s: s['w']*s['h'])
                 if pad_shapes else None)
    only_shape = ((h_shape or v_shape)
                  if pad_shapes and len({round(s['ori']) for s in pad_shapes.values()}) == 1
                  else None)

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
                        try: pts.append((int(xy[0])*SCALE, int(xy[1])*SCALE))
                        except ValueError: pass
            if pts: outlines.append((pts, level))
            i += 1; continue

        tm = re.match(r'^T(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)', l)
        if tm:
            v1 = int(tm.group(1)); pin = int(tm.group(5))
            if i+1 < len(lines) and lines[i+1].strip().startswith('PAD'):
                tx, ty = v1*SCALE, int(tm.group(2))*SCALE
                sl = lines[i+2].strip() if i+2 < len(lines) else ""
                rfm = re.match(r'-?\d+\s+(\d+)\s+RF\s+([\d.]+)\s+(\d+)', sl)
                if rfm:
                    pw=int(rfm.group(1))*SCALE; pori=float(rfm.group(2)); ph=int(rfm.group(3))*SCALE
                else:
                    sm = re.match(r'-?\d+\s+(\d+)\s+[SR]\b', sl)
                    pw=ph=int(sm.group(1))*SCALE if sm else 0.4; pori=0.0
                pads.append({"pin":pin,"x":tx,"y":ty,"w":pw,"h":ph,"ori":pori})
                i += 1; continue
            tx, ty = v1*SCALE, int(tm.group(2))*SCALE
            if only_shape: shape = only_shape
            elif tx==0.0 and ty==0.0: shape = big_shape or v_shape or h_shape
            elif abs(tx) >= abs(ty): shape = h_shape or v_shape
            else: shape = v_shape or h_shape
            if shape: pw,ph,pori = shape['w'],shape['h'],shape['ori']
            else: pw=ph=0.4; pori=0.0
            pads.append({"pin":pin,"x":tx,"y":ty,"w":pw,"h":ph,"ori":pori})
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


def build_net_to_refs(all_pin_nets):
    """Invert {refdes:{pin:net}} → {net:[refdes,...]}"""
    d = defaultdict(list)
    for ref, pins in all_pin_nets.items():
        for pin, net in pins.items():
            if net:
                d[net].append(ref)
    return dict(d)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry
# ─────────────────────────────────────────────────────────────────────────────

def _local_to_world(lx, ly, cx, cy, ori_deg, mirror_x):
    if mirror_x: lx = -lx
    r  = math.radians(ori_deg)
    wx = cx + lx*math.cos(r) - ly*math.sin(r)
    wy = cy + lx*math.sin(r) + ly*math.cos(r)
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


def _flip_pads(pads, board_cx):
    return [(2*board_cx-wx, wy, pw, ph, -wori) for wx,wy,pw,ph,wori in pads]

def _flip_polys(polys, board_cx):
    return [[(2*board_cx-x, y) for x,y in poly] for poly in polys]


# ─────────────────────────────────────────────────────────────────────────────
# Drawing primitives
# ─────────────────────────────────────────────────────────────────────────────

def draw_pad(ax, wx, wy, pw, ph, wori, color, alpha=0.90):
    if pw <= 0 or ph <= 0: pw = ph = 0.25
    r = Rectangle((-pw/2, -ph/2), pw, ph,
                  linewidth=0, facecolor=color, alpha=alpha)
    r.set_transform(Affine2D().rotate_deg(wori).translate(wx, wy) + ax.transData)
    ax.add_patch(r)


def draw_active(ax, pads_v, polys_v, label_xy, color, label_text,
                fp_pads=None, hl_pins=None, hl_color=None,
                gnd_pins=None, pad_alpha=0.90):
    """Draw active-side component.
    fp_pads  : footprint pad list [{pin,…}] — needed for hl_pins / gnd_pins
    hl_pins  : set of pin ints to draw in hl_color (signal highlight)
    gnd_pins : set of pin ints connected to GND → draw in GND_PAD_C (light green)
    """
    for poly in polys_v:
        if poly:
            xs, ys = zip(*poly)
            ax.plot(xs, ys, color=color, lw=0.6, alpha=0.6)
    for idx, (wx, wy, pw, ph, wori) in enumerate(pads_v):
        pin = fp_pads[idx]["pin"] if fp_pads else None
        if hl_pins and pin in hl_pins:
            draw_pad(ax, wx, wy, pw, ph, wori, hl_color, alpha=1.0)
        elif gnd_pins and pin in gnd_pins:
            draw_pad(ax, wx, wy, pw, ph, wori, GND_PAD_C, alpha=0.75)
        else:
            draw_pad(ax, wx, wy, pw, ph, wori, color, alpha=pad_alpha)
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
        tx  = wx + (dx/dist)*off
        ty  = wy + (dy/dist)*off
        if abs(dx) >= abs(dy):
            rot=0; ha='left' if dx>0 else 'right'; va='center'
        else:
            rot=90; ha='center'; va='bottom' if dy>0 else 'top'
        ax.text(tx, ty, lbl, fontsize=fontsize, rotation=rot,
                rotation_mode='anchor', ha=ha, va=va,
                color='#222222', clip_on=True, zorder=9)


def draw_ghost(ax, pads_v, polys_v, bbox_v, alpha=0.55):
    if polys_v:
        for poly in polys_v:
            if poly:
                xs, ys = zip(*poly)
                ax.plot(xs, ys, color=GHOST_C, lw=1.5,
                        linestyle='--', alpha=alpha, dash_capstyle='round')
    else:
        x0, y0, x1, y1 = bbox_v
        ax.add_patch(Rectangle((x0,y0), x1-x0, y1-y0,
                                linewidth=1.5, edgecolor=GHOST_C,
                                facecolor='none', linestyle='--', alpha=alpha*0.8))


def draw_dimmed(ax, polys_v):
    """Very faint outline only — for non-peer components during selection."""
    for poly in polys_v:
        if poly:
            xs, ys = zip(*poly)
            ax.plot(xs, ys, color='#cccccc', lw=0.4, alpha=0.25)


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
        prefix = re.match(r'^([A-Za-z]+)', p["refdes"]).group(1)
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

    def __init__(self, comps, all_pin_nets, net_to_refs, conn_data):
        self.comps_all    = comps
        self.all_pin_nets = all_pin_nets   # {refdes: {pin_str: net}}
        self.net_to_refs  = net_to_refs    # {net: [refdes, ...]}
        self.conn_data    = conn_data      # JSON from femb_u_connections.json

        self.view_back   = False
        self.active_cats = set(CAT_KEYS)
        self._cache      = {}

        # Selection state
        self.selected_ref  = None
        self.peer_ics      = set()   # directly connected U/J/P devices
        self.pathway_rcs   = set()   # R/C/L/D on selected nets
        self.selected_nets = set()   # signal nets of selected component
        self._hl_pins      = {}      # {refdes: set(pin_int)}

        # Click detection: bbox of each visible active-side component
        self._bbox_cache  = {}       # {refdes: (x0,y0,x1,y1)}
        self._renderable  = {c["refdes"] for c in comps}

        xs = [c["x"] for c in comps]
        self._bcx = (min(xs) + max(xs)) / 2
        print(f"  Board X range: {min(xs):.1f} … {max(xs):.1f} mm  "
              f"(centre {self._bcx:.1f} mm)")

        self._build_figure()
        self._redraw()

    # ── Figure ────────────────────────────────────────────────────────────────

    def _build_figure(self):
        self.fig = plt.figure(figsize=(28, 20), facecolor=BG_FIG)

        self.ax = self.fig.add_axes([0.19, 0.09, 0.79, 0.88])
        self.ax.set_facecolor(BG_AXES)
        self.ax.set_aspect('equal')
        for sp in self.ax.spines.values(): sp.set_edgecolor('#cccccc')
        self.ax.tick_params(colors='#444444')
        self.ax.set_xlabel("X (mm)"); self.ax.set_ylabel("Y (mm)")

        # Checkboxes
        ax_chk = self.fig.add_axes([0.01, 0.20, 0.165, 0.76], facecolor='#f0f0f0')
        ax_chk.set_title("Components", fontsize=8, pad=4, color='#333333')
        self.checks = CheckButtons(ax_chk, CAT_LBLS, [True]*len(CAT_LBLS))
        for lbl_obj, (key, _) in zip(self.checks.labels, CATEGORIES):
            lbl_obj.set_color(COMP_COLORS.get(key, '#333333'))
            lbl_obj.set_fontsize(8.5)
        try:
            for rect in self.checks.rectangles:
                rect.set_edgecolor('#555555'); rect.set_linewidth(1.2)
        except AttributeError:
            pass
        self.checks.on_clicked(self._on_check)

        # All / None buttons
        ax_all  = self.fig.add_axes([0.01,  0.11, 0.078, 0.045])
        ax_none = self.fig.add_axes([0.097, 0.11, 0.078, 0.045])
        self.btn_all  = Button(ax_all,  'All',  color='#e0e0e0', hovercolor='#c8e6c9')
        self.btn_none = Button(ax_none, 'None', color='#e0e0e0', hovercolor='#ffcdd2')
        for b in (self.btn_all, self.btn_none):
            b.label.set_fontsize(8); b.label.set_color('#222222')
        self.btn_all.on_clicked(self._on_all)
        self.btn_none.on_clicked(self._on_none)

        # Flip button
        ax_btn = self.fig.add_axes([0.44, 0.02, 0.14, 0.05])
        self.btn_flip = Button(ax_btn, '⟳  Flip to Back',
                               color='#eeeeee', hovercolor='#dddddd')
        self.btn_flip.label.set_color('#222222'); self.btn_flip.label.set_fontsize(9)
        self.btn_flip.on_clicked(self._on_flip)

        # Click-to-select
        self.fig.canvas.mpl_connect('button_press_event', self._on_click)

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_flip(self, _):
        self.view_back = not self.view_back
        self.btn_flip.label.set_text(
            '⟳  Flip to Front' if self.view_back else '⟳  Flip to Back')
        self._cache.clear()
        self._bbox_cache.clear()
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
        for i, active in enumerate(self.checks.get_status()):
            if not active: self.checks.set_active(i)
        self.active_cats = set(CAT_KEYS)
        self._cache.clear(); self._redraw()

    def _on_none(self, _):
        for i, active in enumerate(self.checks.get_status()):
            if active: self.checks.set_active(i)
        self.active_cats = set()
        self._cache.clear(); self._redraw()

    def _on_click(self, event):
        # Only respond to left-click inside the main axes
        if event.inaxes is not self.ax or event.button != 1:
            return
        x, y = event.xdata, event.ydata

        # Find smallest-bbox component whose bbox contains the click point
        hits = []
        for ref, (x0, y0, x1, y1) in self._bbox_cache.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                hits.append(((x1-x0)*(y1-y0), ref))

        if hits:
            hits.sort()
            new_ref = hits[0][1]
            self.selected_ref = None if new_ref == self.selected_ref else new_ref
        else:
            self.selected_ref = None

        self._update_selection()
        self._cache.clear()
        self._redraw()

    # ── Selection logic ───────────────────────────────────────────────────────

    def _update_selection(self):
        ref = self.selected_ref
        if not ref:
            self.peer_ics = set(); self.pathway_rcs = set()
            self.selected_nets = set(); self._hl_pins = {}
            return

        pin_map = self.all_pin_nets.get(ref, {})  # {pin_str: net}

        # ── Step 1: direct nets of selected component ─────────────────────
        # Skip only GND and single-component bypass ($xxx) nets.
        # Power supply nets (VOUT, VDDA, etc.) are KEPT — they carry topology.
        if ref in self.conn_data:
            direct_nets = {
                pdata['net']
                for pdata in self.conn_data[ref]['pins'].values()
                if pdata.get('net')
                and pdata.get('func') not in SKIP_FUNCS
                and not any(pdata['net'].startswith(p) for p in SKIP_NET_PFX)
            }
        else:
            direct_nets = {
                net for net in pin_map.values()
                if net and net not in SKIP_NETS
                and not any(net.startswith(p) for p in SKIP_NET_PFX)
            }

        # ── Step 2: hop-1 — direct neighbours on selected component's nets ─
        hop1_peers = set()   # U/J/P directly on direct_nets
        hop1_rcs   = set()   # R/C/L/D directly on direct_nets
        for net in direct_nets:
            for r in self.net_to_refs.get(net, []):
                if r == ref or r not in self._renderable: continue
                pfx = re.match(r'^([A-Za-z]+)', r)
                if not pfx: continue
                pfx = pfx.group(1)
                if pfx in ('U', 'J', 'P'):
                    hop1_peers.add(r)
                elif pfx in PASSIVE_PREFIXES:
                    hop1_rcs.add(r)

        # ── Step 2b: J/P connectors via hop-1 IC peers ───────────────────
        # Reaches J1/J2 through: LArASIC→COLDATA(hop-1)→SEROUT_R→J connector.
        # Only traverses U-type hop-1 peers (not J/P already found).
        ic_peer_connectors = set()
        for peer_ic in hop1_peers:
            pic_pfx = re.match(r'^([A-Za-z]+)', peer_ic)
            if not pic_pfx or pic_pfx.group(1) != 'U':
                continue
            for net in self.all_pin_nets.get(peer_ic, {}).values():
                if (not net or net in direct_nets or net in SKIP_NETS
                        or any(net.startswith(p) for p in SKIP_NET_PFX)):
                    continue
                for r in self.net_to_refs.get(net, []):
                    if r == ref or r not in self._renderable: continue
                    pfx = re.match(r'^([A-Za-z]+)', r)
                    if not pfx: continue
                    pfx_s = pfx.group(1)
                    if pfx_s in ('J', 'P'):
                        ic_peer_connectors.add(r)
                    elif pfx_s in PASSIVE_PREFIXES:
                        for net2 in self.all_pin_nets.get(r, {}).values():
                            if not net2 or net2 in SKIP_NETS or net2 in direct_nets:
                                continue
                            for r2 in self.net_to_refs.get(net2, []):
                                if r2 not in self._renderable: continue
                                pfx2 = re.match(r'^([A-Za-z]+)', r2)
                                if pfx2 and pfx2.group(1) in ('J', 'P'):
                                    ic_peer_connectors.add(r2)

        # ── Step 3: hop-2 — follow through passives to next ICs ───────────
        # For every hop-1 passive, look at its OTHER nets (not already in
        # direct_nets) and find ICs and further passives on those nets.
        hop2_peers = set()
        hop2_rcs   = set()
        hop2_nets  = set()   # nets reached through hop-1 passives
        for rc in hop1_rcs:
            for net in self.all_pin_nets.get(rc, {}).values():
                if (not net or net in direct_nets or net in SKIP_NETS
                        or any(net.startswith(p) for p in SKIP_NET_PFX)):
                    continue
                hop2_nets.add(net)
                for r in self.net_to_refs.get(net, []):
                    if r == ref or r not in self._renderable: continue
                    pfx = re.match(r'^([A-Za-z]+)', r)
                    if not pfx: continue
                    pfx = pfx.group(1)
                    if pfx in ('U', 'J', 'P') and r not in hop1_peers:
                        hop2_peers.add(r)
                    elif pfx in PASSIVE_PREFIXES and r not in hop1_rcs:
                        hop2_rcs.add(r)

        # ── Step 4: hop-3 — from hop-2 passives, find ICs/connectors only ──
        # Targets J1/J2 connectors that are 3 hops from LArASIC analog inputs
        # (U3 → inductor → filter cap → J1).  Don't collect hop-3 passives
        # (hundreds of ESD diodes would explode the pathway_rcs count).
        hop3_peers = set()
        seen_nets3 = direct_nets | hop2_nets   # avoid re-traversing
        for rc in hop2_rcs:
            for net in self.all_pin_nets.get(rc, {}).values():
                if (not net or net in seen_nets3 or net in SKIP_NETS
                        or any(net.startswith(p) for p in SKIP_NET_PFX)):
                    continue
                for r in self.net_to_refs.get(net, []):
                    if r == ref or r not in self._renderable: continue
                    pfx = re.match(r'^([A-Za-z]+)', r)
                    if pfx and pfx.group(1) in ('U', 'J', 'P'):
                        if r not in hop1_peers and r not in hop2_peers:
                            hop3_peers.add(r)

        # ── Commit ────────────────────────────────────────────────────────
        self.selected_nets = direct_nets | hop2_nets   # all nets in the subgraph
        self.peer_ics      = hop1_peers | hop2_peers | hop3_peers | ic_peer_connectors
        self.pathway_rcs   = hop1_rcs   | hop2_rcs

        # ── Step 5: LDO power delivery (schematic hard-coded map) ─────────
        # KYN misses LDO VOUT→power-rail connections (PCB copper pour plane).
        if ref in LDO_POWER_MAP:
            pwr_nets, asics = LDO_POWER_MAP[ref]
            for asic in asics:
                if asic in self._renderable:
                    self.peer_ics.add(asic)
            self.selected_nets |= set(pwr_nets)
            for net in pwr_nets:
                for r in self.net_to_refs.get(net, []):
                    if r in self._renderable:
                        pfx = re.match(r'^([A-Za-z]+)', r)
                        if pfx and pfx.group(1) in PASSIVE_PREFIXES:
                            self.pathway_rcs.add(r)

        # ── Build highlight-pin sets ───────────────────────────────────────
        self._hl_pins = {}

        # Selected component: pins on its direct nets
        sel_hl = {int(p) for p, net in pin_map.items()
                  if net in direct_nets and p.isdigit()}
        if sel_hl: self._hl_pins[ref] = sel_hl

        # Peer ICs/connectors: pins shared with selected_nets
        for peer in self.peer_ics:
            peer_map = self.all_pin_nets.get(peer, {})
            hl = {int(p) for p, net in peer_map.items()
                  if net in self.selected_nets and p.isdigit()}
            if hl: self._hl_pins[peer] = hl

    # ── Redraw ────────────────────────────────────────────────────────────────

    def _redraw(self):
        ax    = self.ax
        ax.cla()
        ax.set_facecolor(BG_AXES); ax.set_aspect('equal')
        ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)")
        ax.tick_params(colors='#444444')

        vback  = self.view_back
        bcx    = self._bcx
        has_sel = self.selected_ref is not None
        self._bbox_cache.clear()

        for c in self.comps_all:
            ref     = c["refdes"]
            cls     = c["cls"]
            prefix  = c["prefix"]
            fp      = c["fp"]
            cx, cy  = c["x"], c["y"]
            ori     = c["ori"]
            is_back = c["is_back"]
            color   = c["color"]
            label   = c["label"]

            # ── Determine visual state ────────────────────────────────────
            if has_sel:
                if ref == self.selected_ref:            state = 'selected'
                elif ref in self.peer_ics:              state = 'peer'
                elif ref in self.pathway_rcs:           state = 'pathway'
                elif cls in self.active_cats:           state = 'dimmed'
                else:                                   continue
            else:
                if cls not in self.active_cats:         continue
                state = 'normal'

            # ── Compute view geometry ─────────────────────────────────────
            pads_fw  = fp_world_pads(fp, cx, cy, ori, is_back)
            polys_fw = fp_world_outlines(fp, cx, cy, ori, is_back)
            bbox_fw  = fp_bbox(fp, cx, cy, ori, is_back)
            lx_fw    = cx

            if vback:
                pads_v  = _flip_pads(pads_fw, bcx)
                polys_v = _flip_polys(polys_fw, bcx)
                bbox_v  = (2*bcx-bbox_fw[2], bbox_fw[1],
                           2*bcx-bbox_fw[0], bbox_fw[3])
                lx_v    = 2*bcx - lx_fw
            else:
                pads_v, polys_v, bbox_v, lx_v = pads_fw, polys_fw, bbox_fw, lx_fw

            active = (is_back == vback)

            # GND pins for this component (light green, no lines)
            gnd_pins = {int(p) for p, net in
                        self.all_pin_nets.get(ref, {}).items()
                        if net == 'GND' and p.isdigit()}

            # ── Draw ──────────────────────────────────────────────────────
            if state == 'selected':
                if active:
                    hl = self._hl_pins.get(ref, set())
                    draw_active(ax, pads_v, polys_v, (lx_v, cy), color, label,
                                fp_pads=fp["pads"], hl_pins=hl, hl_color=SEL_PAD_C,
                                gnd_pins=gnd_pins)
                    # Gold selection border
                    x0,y0,x1,y1 = bbox_v; m = 1.2
                    ax.add_patch(Rectangle((x0-m, y0-m), x1-x0+2*m, y1-y0+2*m,
                                           linewidth=2.0, edgecolor=SEL_BOX_C,
                                           facecolor='none', zorder=8, alpha=0.9))
                    self._bbox_cache[ref] = bbox_v
                    if c.get("pin_nets"):
                        fs = 1.3 if cls == "coldata" else 1.5
                        draw_pin_labels(ax, pads_v, fp["pads"],
                                        c["pin_nets"], lx_v, cy, fontsize=fs)
                else:
                    draw_ghost(ax, pads_v, polys_v, bbox_v)

            elif state == 'peer':
                hl = self._hl_pins.get(ref, set())
                if active:
                    draw_active(ax, pads_v, polys_v, (lx_v, cy), color, label,
                                fp_pads=fp["pads"], hl_pins=hl, hl_color=PEER_PAD_C,
                                gnd_pins=gnd_pins)
                    self._bbox_cache[ref] = bbox_v
                else:
                    # Back-side peer: show through with reduced alpha + dashed orange border
                    draw_active(ax, pads_v, polys_v, (lx_v, cy), color, label,
                                fp_pads=fp["pads"], hl_pins=hl, hl_color=PEER_PAD_C,
                                gnd_pins=gnd_pins, pad_alpha=0.42)
                    x0, y0, x1, y1 = bbox_v; m = 0.8
                    ax.add_patch(Rectangle((x0-m, y0-m), x1-x0+2*m, y1-y0+2*m,
                                           linewidth=1.2, edgecolor=PEER_PAD_C,
                                           facecolor='none', linestyle='--',
                                           zorder=7, alpha=0.65))
                    self._bbox_cache[ref] = bbox_v

            elif state == 'pathway':
                if active:
                    draw_active(ax, pads_v, polys_v, (lx_v, cy),
                                color, None, fp_pads=fp["pads"],
                                gnd_pins=gnd_pins, pad_alpha=0.55)
                # pathway RC on back side: skip (too noisy)

            elif state == 'dimmed':
                if active:
                    draw_dimmed(ax, polys_v)
                # No pads; no ghost on back side

            else:  # normal
                if active:
                    draw_active(ax, pads_v, polys_v, (lx_v, cy), color, label,
                                fp_pads=fp["pads"], gnd_pins=gnd_pins)
                    if c.get("pin_nets"):
                        fs = 1.3 if cls == "coldata" else 1.5
                        draw_pin_labels(ax, pads_v, fp["pads"],
                                        c["pin_nets"], lx_v, cy, fontsize=fs)
                    self._bbox_cache[ref] = bbox_v
                else:
                    draw_ghost(ax, pads_v, polys_v, bbox_v)

        # ── Info panel ────────────────────────────────────────────────────
        if has_sel:
            self._draw_info_panel(ax)

        # ── Title & legend ────────────────────────────────────────────────
        side  = "BACK ◀" if vback else "▶ FRONT"
        title = (f"FEMB PCB Layout  —  [{side}]  —  Selected: {self.selected_ref}"
                 if has_sel else
                 f"FEMB PCB Layout  —  [{side}]  —  {len(self.active_cats)}/{len(CAT_KEYS)} categories")
        ax.set_title(title, color='#222222', fontsize=11, pad=8)

        handles = []
        if has_sel:
            handles += [
                mpatches.Patch(color=SEL_BOX_C,  label=f"Selected: {self.selected_ref}"),
                mpatches.Patch(color=PEER_PAD_C, label=f"Peer ICs ({len(self.peer_ics)})"),
                mpatches.Patch(facecolor='none', edgecolor='#aaaaaa',
                               linestyle='--', linewidth=1.5, label="Other (dimmed)"),
            ]
            if self.pathway_rcs:
                handles.append(mpatches.Patch(
                    color=COMP_COLORS["resistor"], alpha=0.55,
                    label=f"Pathway R/C ({len(self.pathway_rcs)})"))
        else:
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

    # ── Info panel ────────────────────────────────────────────────────────────

    def _draw_info_panel(self, ax):
        ref = self.selected_ref
        if not ref: return

        cd   = self.conn_data.get(ref, {})
        role = cd.get('role', '—')
        pos  = (f"({cd.get('x_mm','?'):.1f}, {cd.get('y_mm','?'):.1f}) mm"
                if cd else "—")

        # Split peers into front-side / back-side
        back_lookup = {c["refdes"]: c["is_back"] for c in self.comps_all}
        front_peers = sorted(p for p in self.peer_ics if not back_lookup.get(p, False))
        back_peers  = sorted(p for p in self.peer_ics if     back_lookup.get(p, False))

        def _fmt(lst, maxlen=60):
            s = ', '.join(lst)
            return (s[:maxlen-1] + '…') if len(s) > maxlen else (s or '—')

        lines = [
            f"  Selected   : {ref}  [{role}]  {pos}",
            f"  Nets: {len(self.selected_nets)}   Pathway R/C: {len(self.pathway_rcs)}   Hl.pads: {sum(len(v) for v in self._hl_pins.values())}",
            f"  Front peers: {_fmt(front_peers)}",
            f"  Back peers : {_fmt(back_peers)}",
        ]
        txt = '\n'.join(lines)
        ax.text(0.995, 0.005, txt, transform=ax.transAxes,
                fontsize=8.0, va='bottom', ha='right',
                fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.5', facecolor='#fffde7',
                          edgecolor=SEL_BOX_C, linewidth=1.5, alpha=0.93),
                color='#222222', zorder=15)

    # ── Checkbox filter (no-selection mode) ───────────────────────────────────

    def _filtered(self):
        key = frozenset(self.active_cats)
        if key not in self._cache:
            self._cache[key] = [c for c in self.comps_all
                                 if c["cls"] in self.active_cats]
        return self._cache[key]

    # ── Save PNGs ─────────────────────────────────────────────────────────────

    def save_snapshots(self):
        base = Path(__file__).parent
        configs = [
            ("front_All", False, set(CAT_KEYS)),
            ("back_All",  True,  set(CAT_KEYS)),
            ("front_ICs", False, {"coldata","larasic","coldadc","ldo","switch","connector"}),
            ("back_ICs",  True,  {"coldata","larasic","coldadc","ldo","switch","connector"}),
        ]
        for tag, back, cats in configs:
            self.view_back   = back
            self.active_cats = cats
            for i, key in enumerate(CAT_KEYS):
                cur  = self.checks.get_status()[i]
                want = key in cats
                if cur != want: self.checks.set_active(i)
            self._cache.clear()
            self._redraw()
            out = base / f"layout_{tag}.png"
            self.fig.savefig(out, dpi=180, bbox_inches='tight',
                             facecolor=self.fig.get_facecolor())
            print(f"  Saved → {out}")
        self.view_back   = False
        self.active_cats = set(CAT_KEYS)
        for i in range(len(CAT_KEYS)):
            if not self.checks.get_status()[i]: self.checks.set_active(i)
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

    print("Building net index …")
    net_to_refs = build_net_to_refs(all_pin_nets)
    print(f"  {len(net_to_refs)} nets indexed")

    print("Loading connection data …")
    conn_data = {}
    if CONN_FILE.exists():
        conn_data = json.loads(CONN_FILE.read_text())
        print(f"  {len(conn_data)} U devices loaded from {CONN_FILE.name}")
    else:
        print(f"  (femb_u_connections.json not found — selection will use fallback)")

    print("Building component list …")
    comps = build_comp_list(lib, ptmap, parts, all_pin_nets)
    print(f"  {len(comps)} renderable components")

    viewer = PCBViewer(comps, all_pin_nets, net_to_refs, conn_data)

    if args.save:
        viewer.save_snapshots()
    else:
        plt.show()


if __name__ == "__main__":
    main()
