"""The rolling autosave slot: upsert_saved_game create-or-overwrites a fixed id,
so saving after every move (forward or back) keeps ONE entry that always mirrors
the current branch — instead of piling up a new saved game per move.
"""

import tempfile
import unittest
from pathlib import Path

from riftbound_engine import saved_games


class UpsertSavedGameTests(unittest.TestCase):
    def setUp(self):
        # Isolate the store to a temp file so tests never touch real saves.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = saved_games.SAVED_GAMES_PATH
        saved_games.SAVED_GAMES_PATH = Path(self._tmp.name) / "saved_games.json"

    def tearDown(self):
        saved_games.SAVED_GAMES_PATH = self._orig
        self._tmp.cleanup()

    def _moves(self, n):
        return [{"actor": "player_1", "action": f"m{i}", "label": "", "chain": []} for i in range(n)]

    def _autosaves(self):
        return [g for g in saved_games.list_saved_games() if g["id"] == "autosave"]

    def test_creates_then_overwrites_same_slot(self):
        saved_games.upsert_saved_game("autosave", "Autosave", "", {"deck": "x"}, self._moves(1))
        first = self._autosaves()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(first[0]["moves"]), 1)

        # A later move overwrites the SAME slot (still exactly one autosave).
        saved_games.upsert_saved_game("autosave", "Autosave", "", {"deck": "x"}, self._moves(3))
        second = self._autosaves()
        self.assertEqual(len(second), 1)               # no duplicate slot
        self.assertEqual(len(second[0]["moves"]), 3)   # reflects the newer branch

    def test_reflects_rewind_shorter_branch(self):
        saved_games.upsert_saved_game("autosave", "Autosave", "", {}, self._moves(5))
        # Rewind → fewer moves; autosave overwrites with the trimmed branch.
        saved_games.upsert_saved_game("autosave", "Autosave", "", {}, self._moves(2))
        slots = self._autosaves()
        self.assertEqual(len(slots), 1)
        self.assertEqual(len(slots[0]["moves"]), 2)

    def test_autosave_coexists_with_manual_saves(self):
        saved_games.add_saved_game("My Game", "", {}, self._moves(1))
        saved_games.upsert_saved_game("autosave", "Autosave", "", {}, self._moves(1))
        ids = {g["id"] for g in saved_games.list_saved_games()}
        self.assertIn("autosave", ids)
        self.assertIn("my-game", ids)  # manual save untouched


if __name__ == "__main__":
    unittest.main()
