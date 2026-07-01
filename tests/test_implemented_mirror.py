"""Drift guard: the web Implementation Planner's engineImplemented.ts is
generated from riftbound_engine.implemented_surface. If the engine's registries
change (a new effect, trigger, or choice handler) without regenerating the
mirror, this test fails — keeping the planner honest.

Fix a failure with:  npm run gen:implemented
(or `python -m riftbound_engine.implemented_surface --write <path>`).

If the web app isn't checked out alongside the engine (the .ts file is absent),
the comparison is skipped — generator correctness is still covered below.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from riftbound_engine import implemented_surface as S

# riftbound-engine/tests/ → ../../riftbound/src/utils/engineImplemented.ts
_MIRROR = (
    Path(__file__).resolve().parents[2]
    / "riftbound"
    / "src"
    / "utils"
    / "engineImplemented.ts"
)


class ImplementedMirrorTests(unittest.TestCase):
    def test_mirror_matches_generated(self) -> None:
        if not _MIRROR.exists():
            self.skipTest(f"web mirror not present at {_MIRROR}")
        current = _MIRROR.read_text(encoding="utf-8")
        expected = S.render_typescript()
        self.assertEqual(
            current,
            expected,
            "engineImplemented.ts is stale — run `npm run gen:implemented`",
        )

    def test_render_is_deterministic(self) -> None:
        self.assertEqual(S.render_typescript(), S.render_typescript())

    def test_generated_file_is_marked_generated(self) -> None:
        self.assertIn("AUTO-GENERATED", S.render_typescript())

    def test_derivation_reflects_live_registries(self) -> None:
        # Every exact effect/choice handler the engine registers is mirrored,
        # except the documented unfaithful excludes.
        from riftbound_engine import effects as E

        derived = (set(E._REGISTRY) | set(E._CHOICE_REGISTRY)) - set(S.EFFECT_EXCLUDES)
        self.assertTrue(derived <= S.implemented_effects())
        # YOU_MAY_KILL_GEAR (a choice effect) and the recruit pattern both surface.
        self.assertIn("YOU_MAY_KILL_GEAR", S.implemented_effects())
        self.assertTrue(
            any(re.fullmatch(p, "PLAY_3_RECRUIT_BASE") for p in S.effect_patterns())
        )

    def test_curation_excludes_are_real_and_withheld(self) -> None:
        from riftbound_engine import effects as E
        from riftbound_engine import triggers as T

        # An exclude must actually be registered — as an instant effect OR a
        # choice effect (a half-faithful choice handler, e.g. Emperor's Dais,
        # is a legitimate exclude) — else the note is stale; AND it must not
        # leak into the implemented set.
        for code in S.EFFECT_EXCLUDES:
            self.assertTrue(
                code in E._REGISTRY or code in E._CHOICE_REGISTRY,
                f"{code} exclude no longer registered",
            )
            self.assertNotIn(code, S.implemented_effects())
        for code in S.TRIGGER_EXCLUDES:
            self.assertIn(code, T.TRIGGER_EVENT_MAP, f"{code} exclude no longer mapped")
            self.assertNotIn(code, S.mapped_triggers())

    def test_curation_includes_are_not_already_derived(self) -> None:
        # An include is for codes implemented via a NON-registry path; if one
        # shows up in a registry, the include is redundant and should be dropped.
        from riftbound_engine import effects as E
        from riftbound_engine import triggers as T

        for code in S.EFFECT_INCLUDES:
            self.assertNotIn(code, E._REGISTRY)
            self.assertNotIn(code, E._CHOICE_REGISTRY)
            self.assertIn(code, S.implemented_effects())
        for code in S.TRIGGER_INCLUDES:
            self.assertNotIn(code, T.TRIGGER_EVENT_MAP)
            self.assertIn(code, S.mapped_triggers())


if __name__ == "__main__":
    unittest.main()
