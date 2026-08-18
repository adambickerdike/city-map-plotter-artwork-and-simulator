"""Local framing web UI for the city map plotter.

The web UI is a deliberate split between two different rendering problems:

- *Framing* is interactive: a browser slippy map (vector tiles) lets the
  operator drag and zoom anywhere in the world in real time, behind a
  viewfinder whose aspect ratio is taken from the exact plate contract the
  export will use.  Tile detail follows zoom natively, so the screen answers
  "what is here?" immediately.
- *Rendering* is authoritative: the chosen frame is compiled by the ordinary
  ``mapplot export`` pipeline, run as a subprocess job, so every cartographic,
  physical, and provenance guarantee of the CLI applies unchanged.  Tiles are
  never traced into the plot; they are only the viewfinder's background.
"""

from .server import build_ui_config, main, validate_export_request

__all__ = ["build_ui_config", "main", "validate_export_request"]
