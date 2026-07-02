"""Test-suite bootstrap.

The trigger system reads the card taxonomy authored by the front-end
(``riftbound/data/card_taxonomy.json``). That file lives in a sibling project
and changes as cards are tagged, so letting it bleed into the engine tests
would make them depend on external, mutable data — playing a real unit could
fire a trigger and open a chain, breaking tests about unrelated mechanics.

To keep the suite hermetic we point the taxonomy loader at a path that
doesn't exist by default, so no abilities load and the engine behaves exactly
as it did before triggers existed. Tests that exercise triggers either set
``RIFTBOUND_TAXONOMY_PATH`` themselves or monkeypatch
``abilities.triggered_abilities_for`` directly. Set the env var before running
pytest to opt into the real taxonomy.
"""

import os

os.environ.setdefault(
    "RIFTBOUND_TAXONOMY_PATH",
    os.path.join(os.path.dirname(__file__), "_hermetic_no_taxonomy.json"),
)

# Drop any memoized lookup so the hermetic path takes effect even if something
# touched the loader during collection.
try:  # pragma: no cover - defensive
    from riftbound_engine import abilities as _abilities

    _abilities.reset_caches()
except Exception:  # pragma: no cover
    pass
