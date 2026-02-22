#!/usr/bin/env python3
"""
Active Match Measurement Recipe

Demonstrates proper vna_library architecture:
- Uses models for configuration (SweepConfig, TraceSpec, etc.)
- Uses E5071C driver for hardware control
- Orchestrates measurement workflow at recipe level

Measurement: Active Match = sqrt(|S11|^2 + |S21|^2)
Frequency Bands: L2 (1.2156-1.2396 GHz), L1 (1.56342-1.58742 GHz)

Primary focus: Active match equation with marker searches
Secondary (optional): S11 limit envelope for return loss verification
"""

