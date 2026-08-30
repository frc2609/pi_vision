"""Camera-side diagnostics for the vision coprocessor.

The library reads and writes system state; it does not print. Presentation
lives in `vd.py`, so the human report, the JSON dump, the saved snapshot and the
preflight checks are all views onto the same facts rather than four separate
implementations of "look at the camera".

Nothing here raises for an unavailable reading: these are diagnostics, and a
tool that dies because one probe failed is worse than no tool.
"""
