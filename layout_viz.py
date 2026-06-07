"""
FEMB PCB Layout Visualizer
Reads footprint library + PCB ASC, renders actual-size pad layout.
Usage:
  python layout_viz.py               # all components
  python layout_viz.py --layer UJP   # only U/J/P
  python layout_viz.py --layer RC    # only R/C
  python layout_viz.py --layer all   # all (default)
"""

import re, math, sys, argparse
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, Rectangle, Circle
from matplotlib.transforms import Affine2D
import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path(__file__).parent / "FEMB_PADS_EXPORT"
LIB_FILE = BASE / "07_libraries/footprints/footprints_from_pcb_ascii_source.asc"
PCB_FILE = BASE / "02_pcb/pcb_ascii/pcb_layout_ascii.asc"

SCALE = 1 / 1_500_000   # PADS internal units → mm (1mm = 1,500,000 units)

# ---------------------------------------------------------------------------
# Color map by component class
# ---------------------------------------------------------------------------
COMP_COLORS = {
    "coldata":  "#e53935",   # red
    "larasic":  "#1565c0",   # blue
    "coldadc":  "#2e7d32",   # green
    "ldo":      "#f57f17",   # amber
    "switch":   "#6a1b9a",   # purple
    "connector":"#00838f",   # teal
    "passive":  "#78909c",   # grey-blue
    "other":    "#455a64",
}

def classify(refdes, parttype):
    fp = parttype.upper()
    if fp in ("IC_COLDDATA_P3", "LQFP216L"):
        return "coldata"
    if fp in ("LAR_ASIC_P4",):
        return "larasic"
    if fp in ("COLD_ADC_P2",):
        return "coldadc"
    if fp in ("TPS74201",):
        return "ldo"
    if fp in ("NLASB3157", "SC-88"):
        return "switch"
    if fp in ("SSW-132-21-G-T", "0757830132", "IPL1-108-01-L-D-RE1-K",
              "FIDUCIAL", "MTG125/250", "MTG125/250A", "MTG125/250B", "MTBLOCK"):
        return "connector"
    if fp in ("CC0603","CC0402","CC0805","CC1210","CR0603","CR0402","CR1206",
              "55081803400","SOT23","TESTPAD","QFN20","LQFP128","LQFP216L","SC-88"):
        return "passive"
    return "other"

# ---------------------------------------------------------------------------
# Parse footprint library → {name: {"outline": [...polygons...], "pads": [...]}}
# pad entry: {"pin": int, "x": float_mm, "y": float_mm, "w": float_mm, "h": float_mm, "ori": float_deg}
# ---------------------------------------------------------------------------

def parse_decal_block(block_lines):
    """Parse one PARTDECAL block, return (outline_polys, pads)."""
    outline_polys = []   # list of (points_mm, level)
    pads = []

    i = 0
    lines = block_lines

    # ---- graphics (OPEN/CLOSED/CIRCLE) ----
    while i < len(lines):
        l = lines[i].strip()
        if re.match(r'^(OPEN|CLOSED)\s+\d+', l):
            m = re.match(r'^(OPEN|CLOSED)\s+(\d+)\s+\d+\s+\d+\s+(\d+)', l)
            n_pts = int(m.group(2)) if m else 0
            level = int(m.group(3)) if m else 0
            pts = []
            for j in range(n_pts):
                i += 1
                if i < len(lines):
                    xy = lines[i].split()
                    if len(xy) >= 2:
                        try:
                            pts.append((int(xy[0]) * SCALE, int(xy[1]) * SCALE))
                        except ValueError:
                            pass
            outline_polys.append((pts, level))
        elif re.match(r'^T(-?\d+)\s+(-?\d+)', l):
            # terminal: T XLOC YLOC NMXLOC NMYLOC PINNUMBER
            m = re.match(r'^T(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)', l)
            if m:
                tx = int(m.group(1)) * SCALE
                ty = int(m.group(2)) * SCALE
                pin = int(m.group(5))
                # read following PAD definition
                pad_w, pad_h, pad_ori = 0.5, 0.5, 0.0   # defaults
                if i + 1 < len(lines) and lines[i+1].startswith('PAD'):
                    pad_line = lines[i+2] if i+2 < len(lines) else ""
                    pm = re.match(r'^\s*-?\d+\s+(\d+)\s+RF?\s+([\d.]+)\s+(\d+)', pad_line)
                    if pm:
                        pad_w = int(pm.group(1)) * SCALE
                        pad_ori = float(pm.group(2))
                        pad_h = int(pm.group(3)) * SCALE
                    else:
                        pm2 = re.match(r'^\s*-?\d+\s+(\d+)\s+[SR]', pad_line)
                        if pm2:
                            pad_w = int(pm2.group(1)) * SCALE
                            pad_h = pad_w
                pads.append({"pin": pin, "x": tx, "y": ty,
                             "w": pad_w, "h": pad_h, "ori": pad_ori})
        elif l.startswith("VALUE") or l.startswith("Regular"):
            pass  # skip text labels
        i += 1

    return outline_polys, pads


def parse_parttypes(pcb_path):
    """Return dict: parttype_name → decal_name from *PARTTYPE* section."""
    text = pcb_path.read_text(errors='replace')
    pt_start = text.index('*PARTTYPE*')
    part_start = text.index('*PART*')
    pt_section = text[pt_start:part_start]
    types = r'(CAP|RES|DIO|CON|UND|IND|FID|QFP|TTL|HOL)'
    mapping = {}
    for m in re.finditer(rf'^(\S+)\s+(\S+)\s+{types}', pt_section, re.MULTILINE):
        mapping[m.group(1)] = m.group(2)
    return mapping


def parse_library(lib_path):
    """Return dict: footprint_name → {"outline": polys, "pads": [pad_dicts]}"""
    text = lib_path.read_text(errors='replace')
    # Find PARTDECAL section
    m = re.search(r'\*PARTDECAL\*.*?\n', text)
    if not m:
        return {}
    decal_section = text[m.end():]
    m2 = re.search(r'\*PARTTYPE\*', decal_section)
    if m2:
        decal_section = decal_section[:m2.start()]

    # Split into per-decal blocks
    header_pat = re.compile(r'^([A-Z0-9_.:/ -]+?)\s+[IM]\s+\d+', re.MULTILINE)
    headers = list(header_pat.finditer(decal_section))

    result = {}
    for k, hdr in enumerate(headers):
        name = hdr.group(1).strip()
        start = hdr.end()
        end = headers[k+1].start() if k+1 < len(headers) else len(decal_section)
        block_lines = decal_section[start:end].split('\n')
        outlines, pads = parse_decal_block(block_lines)
        result[name] = {"outline": outlines, "pads": pads}

    return result


# ---------------------------------------------------------------------------
# Parse *PART* section → list of component placements
# ---------------------------------------------------------------------------

def parse_parts(pcb_path):
    """Return list of dicts: refdes, parttype, x_mm, y_mm, ori_deg, mirror."""
    text = pcb_path.read_text(errors='replace')
    part_start = text.index('*PART*')
    route_start = text.index('*ROUTE*')
    part_section = text[part_start:route_start]

    parts = []
    for m in re.finditer(
        r'^([A-Za-z][A-Za-z0-9_]*)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+([\d.]+)\s+[UG]\s+([NYM])',
        part_section, re.MULTILINE
    ):
        parts.append({
            "refdes":   m.group(1),
            "parttype": m.group(2),
            "x":        int(m.group(3)) * SCALE,
            "y":        int(m.group(4)) * SCALE,
            "ori":      float(m.group(5)),
            "mirror":   m.group(6) in ('Y', 'M'),
        })
    return parts


# ---------------------------------------------------------------------------
# Transform a single pad from local → global coordinates
# ---------------------------------------------------------------------------

def transform_pad(pad, comp_x, comp_y, ori_deg, mirror):
    """Returns (gx, gy, gw, gh, global_ori_deg) for a pad."""
    lx, ly = pad["x"], pad["y"]
    if mirror:
        lx = -lx
    r = math.radians(ori_deg)
    gx = comp_x + lx * math.cos(r) - ly * math.sin(r)
    gy = comp_y + lx * math.sin(r) + ly * math.cos(r)
    pad_ori = pad["ori"] + ori_deg
    return gx, gy, pad["w"], pad["h"], pad_ori


def transform_poly(pts, comp_x, comp_y, ori_deg, mirror):
    """Returns list of (gx, gy) for polygon points."""
    r = math.radians(ori_deg)
    result = []
    for lx, ly in pts:
        if mirror:
            lx = -lx
        gx = comp_x + lx * math.cos(r) - ly * math.sin(r)
        gy = comp_y + lx * math.sin(r) + ly * math.cos(r)
        result.append((gx, gy))
    return result


# ---------------------------------------------------------------------------
# Draw a single pad as a rectangle on ax
# ---------------------------------------------------------------------------

LEVEL_OUTLINE = {0, 20, 26}   # levels to draw as courtyard/outline

def draw_pad(ax, gx, gy, w, h, pad_ori_deg, color, alpha=0.85):
    """Draw a rectangle pad centred at (gx, gy), w×h mm, rotated pad_ori_deg."""
    if w <= 0 or h <= 0:
        w = h = 0.3
    rect = Rectangle((-w/2, -h/2), w, h,
                     linewidth=0, facecolor=color, alpha=alpha)
    t = (Affine2D().rotate_deg(pad_ori_deg)
         .translate(gx, gy) + ax.transData)
    rect.set_transform(t)
    ax.add_patch(rect)


def draw_outline(ax, pts, color, lw=0.5):
    if len(pts) < 2:
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ax.plot(xs, ys, color=color, linewidth=lw, alpha=0.6)


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render(layer="all"):
    print("Parsing footprint library …")
    lib = parse_library(LIB_FILE)
    print(f"  {len(lib)} footprints loaded")

    print("Parsing PARTTYPE → DECAL mapping …")
    parttype_map = parse_parttypes(PCB_FILE)
    print(f"  {len(parttype_map)} part types mapped")

    print("Parsing PCB part placements …")
    parts = parse_parts(PCB_FILE)
    print(f"  {len(parts)} components loaded")

    # Determine which RefDes prefixes to show
    prefix_filter = None
    if layer == "UJP":
        prefix_filter = {"U", "J", "P"}
    elif layer == "RC":
        prefix_filter = {"R", "C"}
    elif layer == "all":
        prefix_filter = None

    fig, ax = plt.subplots(figsize=(28, 24))
    ax.set_aspect('equal')
    ax.set_facecolor('#1a1a2e')
    fig.patch.set_facecolor('#0f0f1a')

    comp_count = 0
    pad_count = 0
    missing_fp = set()

    for comp in parts:
        ref = comp["refdes"]
        prefix = re.match(r'^([A-Za-z]+)', ref).group(1)

        if prefix_filter and prefix not in prefix_filter:
            continue

        fp_name = comp["parttype"]
        # Resolve via PARTTYPE→DECAL map, fallback to direct name
        decal_name = parttype_map.get(fp_name, fp_name)
        fp = lib.get(decal_name) or lib.get(fp_name)
        if fp is None:
            missing_fp.add(fp_name)
            continue

        cls = classify(ref, fp_name)
        color = COMP_COLORS.get(cls, COMP_COLORS["other"])

        cx, cy = comp["x"], comp["y"]
        ori = comp["ori"]
        mir = comp["mirror"]

        # Draw courtyard outline (level 0 or 26)
        for poly_pts, level in fp["outline"]:
            if level in (0, 20, 26) and len(poly_pts) >= 2:
                gpts = transform_poly(poly_pts, cx, cy, ori, mir)
                draw_outline(ax, gpts, color, lw=0.4)

        # Draw pads
        for pad in fp["pads"]:
            gx, gy, pw, ph, p_ori = transform_pad(pad, cx, cy, ori, mir)
            draw_pad(ax, gx, gy, pw, ph, p_ori, color)
            pad_count += 1

        comp_count += 1

        # Label large components
        if prefix in ("U", "J", "P"):
            ax.text(cx, cy, ref, color='white', fontsize=4,
                    ha='center', va='center',
                    fontweight='bold', clip_on=True)

    print(f"  Rendered {comp_count} components, {pad_count} pads")
    if missing_fp:
        print(f"  Missing footprints: {missing_fp}")

    # Legend
    legend_items = [
        mpatches.Patch(color=COMP_COLORS["coldata"],   label="COLDATA (U1,U2)"),
        mpatches.Patch(color=COMP_COLORS["larasic"],   label="LArASIC ×8"),
        mpatches.Patch(color=COMP_COLORS["coldadc"],   label="ColdADC ×8"),
        mpatches.Patch(color=COMP_COLORS["ldo"],       label="TPS74201 LDO ×11"),
        mpatches.Patch(color=COMP_COLORS["switch"],    label="NLASB3157 ×13"),
        mpatches.Patch(color=COMP_COLORS["connector"], label="J/P Connectors"),
        mpatches.Patch(color=COMP_COLORS["passive"],   label="R/C/L/D Passives"),
    ]
    ax.legend(handles=legend_items, loc='upper right',
              facecolor='#1a1a2e', edgecolor='white',
              labelcolor='white', fontsize=7)

    ax.set_xlabel("X (mm)", color='white')
    ax.set_ylabel("Y (mm)", color='white')
    ax.tick_params(colors='white')
    for spine in ax.spines.values():
        spine.set_edgecolor('#444')

    title_map = {"UJP": "U / J / P", "RC": "R / C", "all": "All"}
    ax.set_title(f"FEMB PCB Layout — {title_map.get(layer,'?')} (actual pad positions, mm scale)",
                 color='white', fontsize=11)

    plt.tight_layout()

    out = Path(__file__).parent / f"layout_{layer}.png"
    fig.savefig(out, dpi=200, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f"Saved → {out}")
    plt.show()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=["UJP", "RC", "all"], default="all")
    args = parser.parse_args()
    render(args.layer)
