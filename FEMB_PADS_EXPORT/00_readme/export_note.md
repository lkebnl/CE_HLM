FEMB PADS export package

Source folder:
C:\Users\LKE\Desktop\SLM_1865_1F

Schematic files collected:
- 03_netlist/keyin_netlist.kyn
- 03_netlist/edif_netlist.edn
- 01_schematic/schematic_exchange/edif_or_edxd_schematic.eds
- 01_schematic/schematic_exchange/connection_exchange.cce
- 01_schematic/schematic_pdf/schematic_full.pdf
- 01_schematic/schematic_graphics/schematic.emf
- 01_schematic/schematic_graphics/schematic.hpgl
- 01_schematic/schematic_reports/schematic_part_list.xlsx
- 01_schematic/schematic_reports/schematic_report.txt
- 01_schematic/schematic_reports/cross_reference_or_report.frs

Native PCB files collected:
- 02_pcb/pads_layout_native/*.pcb

Notes:
- schematic_part_list.xlsx is the Part Lister output.
- keyin_netlist.kyn is the first-choice source for net/refdes/pin connectivity.
- schematic_full.pdf is for human review and page-level checking.
- Next PCB exports needed: PCB ASCII, Placement XY, Layer Stackup, Gerber, Drill.

PCB files added:
- 02_pcb/pcb_ascii/pcb_layout_ascii.asc
- 07_libraries/footprints/footprints_from_pcb_ascii_source.asc

Note:
Only PCB ASCII is available for now. This is enough for first-version component, footprint, via, route, and signal parsing.
