"""
FEMB PCB Layout Viewer — interactive dual-side viewer.

Usage:
  python3 layout_viz.py               # interactive, all components
  python3 layout_viz.py --layer UJP   # only U/J/P
  python3 layout_viz.py --layer RC    # only R/C
  python3 layout_viz.py --save        # save PNG snapshots and exit

Controls:
  [⟳ Flip Board] button  — toggle front / back view
  Radio buttons          — All / U·J·P / R·C filter
"""

import re, math, argparse
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle
from matplotlib.transforms import Affine2D
from matplotlib.widgets import Button, RadioButtons

# ─────────────────────────────────────────────────────────────────────────────
# Paths & scale
# ─────────────────────────────────────────────────────────────────────────────
BASE     = Path(__file__).parent / "FEMB_PADS_EXPORT"
LIB_FILE = BASE / "07_libraries/footprints/footprints_from_pcb_ascii_source.asc"
PCB_FILE = BASE / "02_pcb/pcb_ascii/pcb_layout_ascii.asc"

SCALE = 1 / 1_500_000          # PADS internal units → mm  (1 mm = 1 500 000 units)

# ─────────────────────────────────────────────────────────────────────────────
# Colours  (white-background theme)
# ─────────────────────────────────────────────────────────────────────────────
BG_FIG   = "#ffffff"
BG_AXES  = "#f5f5f5"
GHOST_C  = "#aaaaaa"           # ghost outline colour

COMP_COLORS = {
    "coldata":   "#c62828",    # deep red
    "larasic":   "#1565c0",    # deep blue
    "coldadc":   "#8e24aa",    # purple
    "ldo":       "#e65100",    # burnt orange
    "switch":    "#6a1b9a",    # purple
    "connector": "#00695c",    # dark teal
    "passive":   "#90a4ae",    # blue-grey
    "other":     "#78909c",
}

def classify(parttype):
    fp = parttype.upper()
    if "COLDDATA"  in fp or "LQFP216" in fp: return "coldata"
    if "LAR_ASIC"  in fp:                     return "larasic"
    if "COLD_ADC"  in fp:                     return "coldadc"
    if "TPS74201"  in fp:                     return "ldo"
    if "NLASB"     in fp or "SC-88" in fp:    return "switch"
    if any(x in fp for x in ("MTG","MTBLOCK")): return "mechanical"
    if any(x in fp for x in
           ("SSW","0757","IPL1","FIDUCIAL")): return "connector"
    return "passive"

# ─────────────────────────────────────────────────────────────────────────────
# Parse footprint library → {decal_name: {outline, pads}}
# ─────────────────────────────────────────────────────────────────────────────

def _parse_decal_block(lines):
    """Return (outline_polys, pads) for one PARTDECAL block.

    outline_polys : list of ([(x,y)…], level_int)   — all mm, local coords
    pads          : list of {pin, x, y, w, h, ori}  — all mm, local coords
    """
    outlines, pads = [], []
    i = 0
    while i < len(lines):
        l = lines[i].strip()

        # OPEN / CLOSED polygon
        m = re.match(r'^(OPEN|CLOSED)\s+(\d+)\s+\d+\s+\d+\s+(\d+)', l)
        if m:
            n, level = int(m.group(2)), int(m.group(3))
            pts = []
            for _ in range(n):
                i += 1
                if i < len(lines):
                    xy = lines[i].split()
                    if len(xy) >= 2:
                        try:
                            pts.append((int(xy[0]) * SCALE, int(xy[1]) * SCALE))
                        except ValueError:
                            pass
            if pts:
                outlines.append((pts, level))
            i += 1
            continue

        # Terminal  →  T<XLOC> <YLOC> <NMXLOC> <NMYLOC> <PIN>
        tm = re.match(r'^T(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)', l)
        if tm:
            tx, ty, pin = int(tm.group(1))*SCALE, int(tm.group(2))*SCALE, int(tm.group(5))
            pw, ph, pori = 0.4, 0.4, 0.0
            if i+1 < len(lines) and lines[i+1].strip().startswith('PAD'):
                sl = lines[i+2].strip() if i+2 < len(lines) else ""
                rfm = re.match(r'-?\d+\s+(\d+)\s+RF\s+([\d.]+)\s+(\d+)', sl)
                if rfm:
                    pw, pori, ph = int(rfm.group(1))*SCALE, float(rfm.group(2)), int(rfm.group(3))*SCALE
                else:
                    sm = re.match(r'-?\d+\s+(\d+)\s+[SR]', sl)
                    if sm:
                        pw = ph = int(sm.group(1)) * SCALE
            pads.append({"pin": pin, "x": tx, "y": ty, "w": pw, "h": ph, "ori": pori})
        i += 1
    return outlines, pads


def parse_library(lib_path):
    text = lib_path.read_text(errors='replace')
    m = re.search(r'\*PARTDECAL\*.*?\n', text)
    if not m:
        return {}
    sec = text[m.end():]
    m2 = re.search(r'\*PARTTYPE\*', sec)
    if m2:
        sec = sec[:m2.start()]
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
    text = pcb_path.read_text(errors='replace')
    sec  = text[text.index('*PART*'):text.index('*ROUTE*')]
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

# ─────────────────────────────────────────────────────────────────────────────
# Geometry — always computed in FRONT-VIEW world coordinates
# ─────────────────────────────────────────────────────────────────────────────

def _local_to_world(lx, ly, cx, cy, ori_deg, mirror_x):
    """Rotate (lx,ly) [mirror first if mirror_x] then translate to (cx,cy)."""
    if mirror_x:
        lx = -lx
    r   = math.radians(ori_deg)
    wx  = cx + lx * math.cos(r) - ly * math.sin(r)
    wy  = cy + lx * math.sin(r) + ly * math.cos(r)
    return wx, wy


def fp_world_pads(fp, cx, cy, ori, is_back):
    """Front-view world positions for all pads: [(wx,wy,pw,ph,wori), …]"""
    out = []
    for p in fp["pads"]:
        wx, wy = _local_to_world(p["x"], p["y"], cx, cy, ori, is_back)
        wori   = p["ori"] + (ori if not is_back else -ori)
        out.append((wx, wy, p["w"], p["h"], wori))
    return out


def fp_world_outlines(fp, cx, cy, ori, is_back, levels=(0, 20, 26)):
    """Front-view world polygons for selected levels: [[(wx,wy),…],…]"""
    polys = []
    for pts, level in fp["outline"]:
        if level not in levels or len(pts) < 2:
            continue
        polys.append([_local_to_world(lx, ly, cx, cy, ori, is_back)
                      for lx, ly in pts])
    return polys


def fp_bbox(fp, cx, cy, ori, is_back):
    """Bounding box of all outline points (front-view)."""
    all_xy = [xy for pts, _ in fp["outline"]
                  for xy in [_local_to_world(lx, ly, cx, cy, ori, is_back)
                              for lx, ly in pts]]
    if not all_xy:
        return cx-.5, cy-.5, cx+.5, cy+.5
    xs = [p[0] for p in all_xy]; ys = [p[1] for p in all_xy]
    return min(xs), min(ys), max(xs), max(ys)

# ─────────────────────────────────────────────────────────────────────────────
# View transform  (apply AFTER computing front-view world coords)
# When viewing_back: mirror all world X around the board centre
# ─────────────────────────────────────────────────────────────────────────────

def _flip_pads(pads, board_cx):
    """Mirror pad world positions in X and negate pad orientation."""
    return [(2*board_cx - wx, wy, pw, ph, -wori)
            for wx, wy, pw, ph, wori in pads]

def _flip_polys(polys, board_cx):
    return [[(2*board_cx - x, y) for x, y in poly] for poly in polys]

def _flip_xy(x, y, board_cx):
    return 2*board_cx - x, y

# ─────────────────────────────────────────────────────────────────────────────
# Drawing primitives
# ─────────────────────────────────────────────────────────────────────────────

def draw_pad(ax, wx, wy, pw, ph, wori, color, alpha=0.90):
    if pw <= 0 or ph <= 0:
        pw = ph = 0.25
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


def draw_ghost(ax, pads_v, polys_v, bbox_v):
    """Grey dashed courtyard outline — no pads."""
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

def build_comp_list(lib, ptmap, parts):
    comps, missing = [], set()
    for p in parts:
        cls = classify(p["parttype"])
        if cls == "mechanical":
            continue
        fp_key = ptmap.get(p["parttype"], p["parttype"])
        fp = lib.get(fp_key) or lib.get(p["parttype"])
        if fp is None:
            missing.add(p["parttype"]); continue
        prefix = re.match(r'^([A-Za-z]+)', p["refdes"]).group(1)
        comps.append({**p,
                      "fp":     fp,
                      "prefix": prefix,
                      "color":  COMP_COLORS[cls],
                      "label":  p["refdes"] if prefix in ("U","J","P") else None})
    if missing:
        print(f"  No footprint for: {missing}")
    return comps

# ─────────────────────────────────────────────────────────────────────────────
# Interactive viewer
# ─────────────────────────────────────────────────────────────────────────────

class PCBViewer:
    LAYERS = {"All": None, "U/J/P": {"U","J","P"}, "R/C": {"R","C"}}

    def __init__(self, comps):
        self.comps_all   = comps
        self.view_back   = False
        self.layer_label = "All"
        self._cache      = {}

        # board X-centre (for flip transform)
        xs = [c["x"] for c in comps]
        self._bcx = (min(xs) + max(xs)) / 2
        print(f"  Board X range: {min(xs):.1f} … {max(xs):.1f} mm  "
              f"(centre {self._bcx:.1f} mm)")

        self._build_figure()
        self._redraw()

    # ── Figure ────────────────────────────────────────────────────────────────

    def _build_figure(self):
        self.fig = plt.figure(figsize=(26, 20), facecolor=BG_FIG)
        self.ax  = self.fig.add_axes([0.04, 0.10, 0.92, 0.87])
        self.ax.set_facecolor(BG_AXES)
        self.ax.set_aspect('equal')
        for sp in self.ax.spines.values():
            sp.set_edgecolor('#cccccc')
        self.ax.tick_params(colors='#444444')
        self.ax.xaxis.label.set_color('#444444')
        self.ax.yaxis.label.set_color('#444444')
        self.ax.set_xlabel("X (mm)")
        self.ax.set_ylabel("Y (mm)")

        # Flip button
        ax_btn = self.fig.add_axes([0.43, 0.01, 0.14, 0.055])
        self.btn = Button(ax_btn, '⟳  Flip to Back',
                          color='#eeeeee', hovercolor='#dddddd')
        self.btn.label.set_color('#222222')
        self.btn.label.set_fontsize(9)
        self.btn.on_clicked(self._on_flip)

        # Layer radio
        ax_rad = self.fig.add_axes([0.01, 0.01, 0.12, 0.08],
                                   facecolor='#eeeeee')
        self.radio = RadioButtons(ax_rad, list(self.LAYERS.keys()),
                                  activecolor='#1565c0')
        for lbl in self.radio.labels:
            lbl.set_color('#222222')
            lbl.set_fontsize(8)
        self.radio.on_clicked(self._on_layer)

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_flip(self, _):
        self.view_back = not self.view_back
        self.btn.label.set_text(
            '⟳  Flip to Front' if self.view_back else '⟳  Flip to Back')
        self._redraw()

    def _on_layer(self, lbl):
        self.layer_label = lbl
        self._redraw()

    # ── Component filter ──────────────────────────────────────────────────────

    def _filtered(self):
        lbl = self.layer_label
        if lbl not in self._cache:
            pf = self.LAYERS[lbl]
            self._cache[lbl] = (self.comps_all if pf is None
                                else [c for c in self.comps_all
                                      if c["prefix"] in pf])
        return self._cache[lbl]

    # ── Redraw ────────────────────────────────────────────────────────────────

    def _redraw(self):
        ax   = self.ax
        ax.cla()
        ax.set_facecolor(BG_AXES)
        ax.set_aspect('equal')
        ax.set_xlabel("X (mm)")
        ax.set_ylabel("Y (mm)")
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

            # ── Compute FRONT-VIEW world coords (no flip yet) ──────────────
            pads_fw   = fp_world_pads(fp, cx, cy, ori, is_back)
            polys_fw  = fp_world_outlines(fp, cx, cy, ori, is_back)
            bbox_fw   = fp_bbox(fp, cx, cy, ori, is_back)
            lx_fw, ly = cx, cy

            # ── Apply view transform for back view ─────────────────────────
            if vback:
                pads_v  = _flip_pads(pads_fw, bcx)
                polys_v = _flip_polys(polys_fw, bcx)
                bbox_v  = (2*bcx - bbox_fw[2], bbox_fw[1],
                           2*bcx - bbox_fw[0], bbox_fw[3])
                lx_v    = 2*bcx - lx_fw
            else:
                pads_v, polys_v, bbox_v, lx_v = pads_fw, polys_fw, bbox_fw, lx_fw

            # ── Draw: active side = colour, other side = ghost ─────────────
            active = (is_back == vback)   # component is on the side we're viewing
            if active:
                draw_active(ax, pads_v, polys_v, (lx_v, ly), color, label)
            else:
                draw_ghost(ax, pads_v, polys_v, bbox_v)

        # Title + legend
        side = "BACK ◀" if vback else "▶ FRONT"
        ax.set_title(f"FEMB PCB Layout  —  [{side}]  —  {self.layer_label}",
                     color='#222222', fontsize=11, pad=8)

        handles = [
            mpatches.Patch(color=COMP_COLORS["coldata"],   label="COLDATA"),
            mpatches.Patch(color=COMP_COLORS["larasic"],   label="LArASIC ×8"),
            mpatches.Patch(color=COMP_COLORS["coldadc"],   label="ColdADC ×8"),
            mpatches.Patch(color=COMP_COLORS["ldo"],       label="LDO ×11"),
            mpatches.Patch(color=COMP_COLORS["switch"],    label="Switch ×13"),
            mpatches.Patch(color=COMP_COLORS["connector"], label="Connector"),
            mpatches.Patch(color=COMP_COLORS["passive"],   label="Passive"),
            mpatches.Patch(facecolor='none', edgecolor=GHOST_C,
                           linestyle='--', linewidth=1.5,
                           label="Ghost (other side)"),
        ]
        ax.legend(handles=handles, loc='upper right', fontsize=7,
                  facecolor='white', edgecolor='#cccccc',
                  labelcolor='#222222', framealpha=0.95)

        self.fig.canvas.draw_idle()

    # ── Save PNGs ─────────────────────────────────────────────────────────────

    def save_snapshots(self):
        base = Path(__file__).parent
        for tag, back, layer in [
            ("front", False, "All"),
            ("back",  True,  "All"),
            ("front", False, "U/J/P"),
            ("back",  True,  "U/J/P"),
        ]:
            self.view_back   = back
            self.layer_label = layer
            self._redraw()
            out = base / f"layout_{tag}_{layer.replace('/','')}.png"
            self.fig.savefig(out, dpi=180, bbox_inches='tight',
                             facecolor=self.fig.get_facecolor())
            print(f"  Saved → {out}")
        self.view_back   = False
        self.layer_label = "All"
        self._redraw()

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=["all","UJP","RC"], default="all")
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

    print("Building component list …")
    comps = build_comp_list(lib, ptmap, parts)
    print(f"  {len(comps)} renderable components")

    viewer = PCBViewer(comps)

    layer_map = {"all": "All", "UJP": "U/J/P", "RC": "R/C"}
    viewer.layer_label = layer_map.get(args.layer, "All")
    viewer._redraw()

    if args.save:
        viewer.save_snapshots()
    else:
        plt.show()


if __name__ == "__main__":
    main()
