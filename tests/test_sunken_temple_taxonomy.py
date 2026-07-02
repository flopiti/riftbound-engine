"""Guards Sunken Temple's full taxonomy shape so it stays faithful:

    "When you conquer here with one or more [Mighty] units, you may pay 1 energy
     to draw 1."

  trigger   WHEN_CONQUER_HERE
  condition IF_1+_UNIT_MIGHTY   (5+ Might conqueror; engine._condition_met)
  cost      PAY_1_ENERGY        (optional — triggered-ability costs are "may pay")
  effect    DRAW_1

Every component must be implemented, so the card is fully faithful end-to-end.
"""

from __future__ import annotations

import unittest

from riftbound_engine import abilities as ab
from riftbound_engine import effects as eff
from riftbound_engine.csv_data import parse_pay_cost
from riftbound_engine import implemented_surface as isf


class SunkenTempleTaxonomyTests(unittest.TestCase):
    def _ability(self):
        abilities = ab.triggered_abilities_for("Sunken Temple")
        self.assertEqual(len(abilities), 1, "Sunken Temple should have one ability")
        return abilities[0]

    def test_shape(self) -> None:
        a = self._ability()
        self.assertEqual(a.triggers, ("WHEN_CONQUER_HERE",))
        self.assertEqual(a.conditions, ("IF_1+_UNIT_MIGHTY",))
        self.assertEqual(a.costs, ("PAY_1_ENERGY",))
        self.assertEqual(a.active_effects, ("DRAW_1",))

    def test_every_component_is_implemented(self) -> None:
        a = self._ability()
        surface = isf.surface_data()
        self.assertIn("IF_1+_UNIT_MIGHTY", surface["conditions"])
        self.assertTrue(all(eff.is_implemented(e) for e in a.active_effects))
        # PAY_1_ENERGY is a chargeable Energy cost (no unsupported remainder).
        _, unsupported = parse_pay_cost(tuple(a.costs), ())
        self.assertEqual(unsupported, ())


if __name__ == "__main__":
    unittest.main()
