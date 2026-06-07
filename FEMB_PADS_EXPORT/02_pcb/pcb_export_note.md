PCB export status

Main PCB structured file:
- 02_pcb/pcb_ascii/pcb_layout_ascii.asc

This PADS Layout ASCII file will be used as the first-version PCB data source.

Expected useful sections inside the ASC:
- *PCB*       board parameters
- *LINES*     board outline / drawings / mechanical lines
- *VIA*       via definitions
- *PARTDECAL* footprint / decal definitions
- *PARTTYPE*  part type definitions
- *PART*      component placement
- *ROUTE*     routing data
- *SIGNAL*    net routing / signal data
- *RULES*     design rules, if included

Separate exports not available yet:
- placement_xy
- layer_stackup
- Gerber
- Drill
- standalone footprint library

Temporary plan:
- Parse component placement from *PART*.
- Parse footprints/pads from *PARTDECAL*.
- Parse vias/routes/signals from *VIA*, *ROUTE*, and *SIGNAL*.
- Parse board outline from *LINES* if available.
- Add Gerber/Drill/Layer stackup later if export permissions become available.
