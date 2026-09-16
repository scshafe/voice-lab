# Recording contracts

Python `recording_contracts.py` owns model definitions and generated JSON schema.
Swift validates the same schema before accepting transport data. Framing uses
four unsigned big-endian length bytes and a bounded UTF-8 JSON payload; telemetry
has a lower independent bound. Canonical evidence serialization remains Python's
responsibility.

The Swift schema validator rejects unknown object fields recursively, preserves
strict integer/number/boolean distinctions, and checks correlated take IDs,
generations, counts, timestamps, paths, legal transitions, and frame recipes.
Python-generated fixture schemas are the shape authority. Swift encodes both
positive and negative fixtures back to Python, which checks all evidence rules.
Canonical catalog/reservation hash recomputation is explicitly Python-owned;
those negative cases are reported as deferred by Swift and verified by Python.
