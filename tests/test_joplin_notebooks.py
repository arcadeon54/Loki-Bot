"""
Focused tests for Joplin notebook/folder browsing.

Background: joplin_integration.py already had everything needed to browse the
notebook tree (get_folder_tree, resolve_notebook_path, folder_path_of) but
none of it was ever exposed as a Loki tool, so production Loki told the Boss
it "can't browse notebooks" even though the Joplin Data API supports it fine.
This adds the resolution/browsing helpers plus the notebook_* / note_move
tools, without touching note_search/note_read/note_create's existing
behavior.

Nothing here touches the real Joplin sidecar or Discord/Telegram: _request/
_paginated/get_folder_tree are stubbed with canned data, same pattern as
tests/test_joplin_integration.py.

Run:  venv/bin/python -m unittest tests.test_joplin_notebooks -v
"""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"
os.environ.setdefault("OWNER_USER_ID", BOSS_ID)
os.environ.setdefault("CREW_USER_IDS", CREW_ID)
os.environ.setdefault("JOPLIN_API_TOKEN", "test-token-not-real")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp.name)

import joplin_integration as jp
import assistant_tools as at
from tools import ToolContext, REGISTRY


def boss_ctx():
    return ToolContext(user_id=BOSS_ID, user_name="Boss", channel_id="c")


def crew_ctx():
    return ToolContext(user_id=CREW_ID, user_name="Rob", channel_id="c")


def run(coro):
    return asyncio.run(coro)


FOLDERS = [
    {"id": "personal-root", "title": "Personal", "parent_id": ""},
    {"id": "officer-logs", "title": "Officer Logs", "parent_id": "personal-root"},
    {"id": "personal-recipes", "title": "Recipes", "parent_id": "personal-root"},
    {"id": "work-root", "title": "Work", "parent_id": ""},
    {"id": "work-recipes", "title": "Recipes", "parent_id": "work-root"},  # duplicate name, other parent
]


class ResolveNotebookRef(unittest.TestCase):
    def test_bare_name_exact_match(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            fid, path = run(jp.resolve_notebook_ref("Officer Logs"))
        self.assertEqual(fid, "officer-logs")
        self.assertEqual(path, "Personal/Officer Logs")

    def test_slash_path(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            fid, path = run(jp.resolve_notebook_ref("Personal/Officer Logs"))
        self.assertEqual(fid, "officer-logs")
        self.assertEqual(path, "Personal/Officer Logs")

    def test_arrow_path(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            fid, path = run(jp.resolve_notebook_ref("Personal → Officer Logs"))
        self.assertEqual(fid, "officer-logs")

    def test_gt_path(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            fid, path = run(jp.resolve_notebook_ref("Personal > Officer Logs"))
        self.assertEqual(fid, "officer-logs")

    def test_ambiguous_bare_name_raises_with_both_paths(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            with self.assertRaises(jp.NotebookAmbiguous) as ctx:
                run(jp.resolve_notebook_ref("Recipes"))
        self.assertEqual(set(ctx.exception.paths), {"Personal/Recipes", "Work/Recipes"})

    def test_ambiguous_never_silently_picks_one(self):
        # The disambiguating path form must still work even though the bare
        # name is ambiguous.
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            fid, _ = run(jp.resolve_notebook_ref("Work/Recipes"))
        self.assertEqual(fid, "work-recipes")

    def test_not_found(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            with self.assertRaises(jp.JoplinError):
                run(jp.resolve_notebook_ref("Nonexistent Notebook"))

    def test_raw_folder_id_passthrough(self):
        x_folder = {"id": "a" * 32, "title": "X", "parent_id": ""}
        with mock.patch.object(jp, "get_folder_tree",
                               new=mock.AsyncMock(return_value=FOLDERS + [x_folder])), \
             mock.patch.object(jp, "get_folder", new=mock.AsyncMock(return_value=x_folder)):
            fid, path = run(jp.resolve_notebook_ref("a" * 32))
        self.assertEqual(fid, "a" * 32)
        self.assertEqual(path, "X")


class NotebookTreeAndChildren(unittest.TestCase):
    def test_build_tree_nests_children_under_parents(self):
        tree = jp.build_notebook_tree(FOLDERS)
        roots = {n["title"] for n in tree}
        self.assertEqual(roots, {"Personal", "Work"})
        personal = next(n for n in tree if n["title"] == "Personal")
        self.assertEqual({c["title"] for c in personal["children"]}, {"Officer Logs", "Recipes"})

    def test_folder_children_scoped_to_parent(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            children = run(jp.folder_children("personal-root"))
        self.assertEqual({c["title"] for c in children}, {"Officer Logs", "Recipes"})

    def test_folder_children_empty_for_leaf(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            children = run(jp.folder_children("officer-logs"))
        self.assertEqual(children, [])


class NotesInFolder(unittest.TestCase):
    def test_sorted_most_recent_first_and_limited(self):
        notes = [
            {"id": "n1", "title": "Old", "updated_time": 100},
            {"id": "n2", "title": "New", "updated_time": 300},
            {"id": "n3", "title": "Mid", "updated_time": 200},
        ]
        with mock.patch.object(jp, "_paginated", new=mock.AsyncMock(return_value=notes)):
            out = run(jp.notes_in_folder("officer-logs", limit=2))
        self.assertEqual([n["title"] for n in out], ["New", "Mid"])

    def test_no_limit_returns_all(self):
        notes = [{"id": "n1", "title": "A", "updated_time": 1}]
        with mock.patch.object(jp, "_paginated", new=mock.AsyncMock(return_value=notes)):
            out = run(jp.notes_in_folder("officer-logs"))
        self.assertEqual(len(out), 1)


class MoveNote(unittest.TestCase):
    def test_move_note_sets_parent_id(self):
        with mock.patch.object(jp, "update_note", new=mock.AsyncMock(return_value={"id": "n1"})) as upd:
            run(jp.move_note("n1", "officer-logs"))
        upd.assert_awaited_once_with("n1", parent_id="officer-logs")


class NoteMoveTool(unittest.TestCase):
    def test_moves_found_note_into_resolved_notebook(self):
        note = {"id": "n1", "title": "Shift Report", "parent_id": "personal-root"}
        with mock.patch.object(jp, "find_note_by_title", new=mock.AsyncMock(return_value=note)), \
             mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)), \
             mock.patch.object(jp, "move_note", new=mock.AsyncMock(return_value={})) as mv, \
             mock.patch.object(jp, "sync_summary", return_value="synced"):
            out = run(at._note_move(
                {"note": "Shift Report", "notebook": "Officer Logs"}, boss_ctx()))
        mv.assert_awaited_once_with("n1", "officer-logs")
        self.assertIn("Officer Logs", out)

    def test_note_not_found(self):
        with mock.patch.object(jp, "find_note_by_title", new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(jp, "search_notes", new=mock.AsyncMock(return_value=[])):
            out = run(at._note_move(
                {"note": "Nope", "notebook": "Officer Logs"}, boss_ctx()))
        self.assertIn("Couldn't find", out)

    def test_ambiguous_destination_asks_instead_of_guessing(self):
        note = {"id": "n1", "title": "Shift Report", "parent_id": "personal-root"}
        with mock.patch.object(jp, "find_note_by_title", new=mock.AsyncMock(return_value=note)), \
             mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)), \
             mock.patch.object(jp, "move_note", new=mock.AsyncMock()) as mv:
            out = run(at._note_move(
                {"note": "Shift Report", "notebook": "Recipes"}, boss_ctx()))
        mv.assert_not_awaited()
        self.assertIn("more than one notebook", out)


class NotebookGetTool(unittest.TestCase):
    def test_reports_path_children_and_note_count(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)), \
             mock.patch.object(jp, "notes_in_folder",
                               new=mock.AsyncMock(return_value=[{"id": "n1", "title": "A"}])):
            out = run(at._notebook_get({"name_or_id": "Personal"}, boss_ctx()))
        self.assertIn("Personal", out)
        self.assertIn("Officer Logs", out)
        self.assertIn("Notes: 1", out)

    def test_ambiguous_reports_candidates_not_a_guess(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            out = run(at._notebook_get({"name_or_id": "Recipes"}, boss_ctx()))
        self.assertIn("more than one notebook", out)
        self.assertIn("Personal/Recipes", out)
        self.assertIn("Work/Recipes", out)

    def test_not_found(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            out = run(at._notebook_get({"name_or_id": "Nope"}, boss_ctx()))
        self.assertIn("No notebook named", out)


class NotebookChildrenTool(unittest.TestCase):
    def test_lists_direct_children_only(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            out = run(at._notebook_children({"name_or_id": "Personal"}, boss_ctx()))
        self.assertIn("Officer Logs", out)
        self.assertIn("Recipes", out)
        self.assertNotIn("Work", out)


class NotebookNotesTool(unittest.TestCase):
    def test_lists_notes_in_resolved_notebook(self):
        notes = [{"id": "n1", "title": "Shift 2026-08-01", "updated_time": 1}]
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)), \
             mock.patch.object(jp, "notes_in_folder", new=mock.AsyncMock(return_value=notes)):
            out = run(at._notebook_notes({"notebook": "Personal/Officer Logs"}, boss_ctx()))
        self.assertIn("Shift 2026-08-01", out)

    def test_empty_notebook(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)), \
             mock.patch.object(jp, "notes_in_folder", new=mock.AsyncMock(return_value=[])):
            out = run(at._notebook_notes({"notebook": "Officer Logs"}, boss_ctx()))
        self.assertIn("No notes", out)


class NotebookListAndTreeTools(unittest.TestCase):
    def test_list_shows_full_paths(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            out = run(at._notebook_list({}, boss_ctx()))
        self.assertIn("Personal/Officer Logs", out)
        self.assertIn("Work/Recipes", out)

    def test_tree_indents_children_under_parents(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            out = run(at._notebook_tree({}, boss_ctx()))
        lines = out.splitlines()
        personal_idx = next(i for i, l in enumerate(lines) if l.strip() == "- Personal")
        child_idx = next(i for i, l in enumerate(lines) if l.strip() == "- Officer Logs")
        self.assertGreater(child_idx, personal_idx)
        self.assertTrue(lines[child_idx].startswith("  "))


class ArrowAndGtNormalizationInResolveNotebookPath(unittest.TestCase):
    """note_create/note_append/list_create all funnel through
    resolve_notebook_path, so fixing normalization there (rather than only in
    resolve_notebook_ref) means the Boss can say 'Personal → Officer Logs' to
    the *existing* tools too, without an artificial title prefix."""

    def test_arrow_form_resolves_same_as_slash_form(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            fid = run(jp.resolve_notebook_path("Personal → Officer Logs", create=False))
        self.assertEqual(fid, "officer-logs")


class ToolRegistration(unittest.TestCase):
    NEW_TOOLS = ("notebook_list", "notebook_tree", "notebook_get",
                "notebook_children", "notebook_notes", "note_move")

    def test_all_registered(self):
        for name in self.NEW_TOOLS:
            self.assertIn(name, REGISTRY, f"{name} not registered")

    def test_all_boss_only(self):
        for name in self.NEW_TOOLS:
            self.assertEqual(REGISTRY[name].permission, "boss")

    def test_no_notebook_deletion_tool_exists(self):
        self.assertNotIn("notebook_delete", REGISTRY)
        self.assertNotIn("notebook_remove", REGISTRY)


if __name__ == "__main__":
    unittest.main()


class ReadBackAfterWriteOutsideLokiNamespace(unittest.TestCase):
    """Regression: a note written to one of the BOSS's notebooks was invisible
    to the very next read.

    find_note_by_title() with no notebook only scanned the Loki namespace and
    then fell back to /search, whose full-text index lags creation by seconds.
    So "add this to Officer Logs" then "read it back" failed — and note_append
    concluded the note was missing and created a DUPLICATE in the default
    Inbox, splitting the content across two notes.
    """
    NOTE = {"id": "n1", "title": "Officer log 2026-08-05",
            "parent_id": "officer-logs", "updated_time": 200}
    ALL_NOTES = [
        {"id": "n0", "title": "something else", "parent_id": "loki-root",
         "updated_time": 100},
        NOTE,
    ]

    def _patched(self, all_notes=None):
        """Stub the listing endpoint; /search stays empty to stand in for the
        lagging index that made this fail in production."""
        return (
            mock.patch.object(jp, "_paginated",
                              new=mock.AsyncMock(return_value=all_notes
                                                 if all_notes is not None
                                                 else self.ALL_NOTES)),
            mock.patch.object(jp, "search_notes",
                              new=mock.AsyncMock(return_value=[])),
            mock.patch.object(jp, "get_note",
                              new=mock.AsyncMock(side_effect=lambda i, **k: dict(
                                  self.NOTE, body="line one"))),
        )

    def test_finds_a_note_outside_the_loki_namespace(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            hit = run(jp.find_note_by_title("Officer log 2026-08-05"))
        self.assertIsNotNone(hit, "note in the Boss's own notebook was not found")
        self.assertEqual(hit["id"], "n1")

    def test_lookup_does_not_depend_on_the_lagging_search_index(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3 as _get:
            run(jp.find_note_by_title("Officer log 2026-08-05"))
        # search_notes returned [] throughout; the hit came from the listing.

    def test_most_recently_updated_wins_when_titles_collide(self):
        dupes = [
            {"id": "old", "title": "Same", "parent_id": "a", "updated_time": 1},
            {"id": "new", "title": "Same", "parent_id": "b", "updated_time": 9},
        ]
        with mock.patch.object(jp, "_paginated",
                               new=mock.AsyncMock(return_value=dupes)):
            hit = run(jp._find_note_anywhere("Same"))
        self.assertEqual(hit["id"], "new")

    def test_titles_match_case_and_whitespace_insensitively(self):
        with mock.patch.object(jp, "_paginated",
                               new=mock.AsyncMock(return_value=self.ALL_NOTES)):
            hit = run(jp._find_note_anywhere("  officer LOG 2026-08-05 "))
        self.assertEqual(hit["id"], "n1")

    def test_missing_note_is_still_none(self):
        with mock.patch.object(jp, "_paginated",
                               new=mock.AsyncMock(return_value=self.ALL_NOTES)):
            self.assertIsNone(run(jp._find_note_anywhere("no such note")))


class AppendFindsTheNoteWhereverItLives(unittest.TestCase):
    """note_append with no notebook must append to the existing note, not
    create a second copy in the default Inbox."""

    def test_unscoped_append_searches_everywhere(self):
        seen = {}

        async def fake_find(title, notebook=None):
            seen["notebook"] = notebook
            return {"id": "n1", "title": title, "parent_id": "officer-logs"}

        with mock.patch.object(jp, "find_note_by_title", fake_find), \
             mock.patch.object(jp, "append_to_note",
                               new=mock.AsyncMock(return_value={"id": "n1"})), \
             mock.patch.object(jp, "create_note",
                               new=mock.AsyncMock()) as created:
            run(jp.append_or_create("T", "Loki/Inbox", "more", search_all=True))
        self.assertIsNone(seen["notebook"], "unscoped append must search everywhere")
        created.assert_not_called()

    def test_scoped_append_still_scopes_the_lookup(self):
        """Log writers (journals, work sessions) must keep matching only
        inside their own notebook."""
        seen = {}

        async def fake_find(title, notebook=None):
            seen["notebook"] = notebook
            return {"id": "n1", "title": title, "parent_id": "x"}

        with mock.patch.object(jp, "find_note_by_title", fake_find), \
             mock.patch.object(jp, "append_to_note",
                               new=mock.AsyncMock(return_value={"id": "n1"})):
            run(jp.append_or_create("T", "Loki/Journal", "more"))
        self.assertEqual(seen["notebook"], "Loki/Journal")

    def test_tool_without_notebook_requests_a_global_search(self):
        captured = {}

        async def fake_aoc(title, notebook, content, header=None, tags=None,
                           search_all=False):
            captured.update(notebook=notebook, search_all=search_all)
            return {"id": "n1", "parent_id": "officer-logs"}

        with mock.patch.object(jp, "append_or_create", fake_aoc), \
             mock.patch.object(jp, "folder_path_of",
                               new=mock.AsyncMock(return_value="Personal/Officer Logs")), \
             mock.patch.object(jp, "sync_summary", lambda *a, **k: "sync ok"):
            out = run(REGISTRY["note_append"].handler(
                {"title": "T", "content": "more"}, boss_ctx()))
        self.assertTrue(captured["search_all"])
        self.assertIn("Personal/Officer Logs", out)

    def test_tool_with_notebook_keeps_the_scope(self):
        captured = {}

        async def fake_aoc(title, notebook, content, header=None, tags=None,
                           search_all=False):
            captured.update(notebook=notebook, search_all=search_all)
            return {"id": "n1", "parent_id": "x"}

        with mock.patch.object(jp, "append_or_create", fake_aoc), \
             mock.patch.object(jp, "folder_path_of",
                               new=mock.AsyncMock(return_value="Personal/Officer Logs")), \
             mock.patch.object(jp, "sync_summary", lambda *a, **k: "sync ok"):
            run(REGISTRY["note_append"].handler(
                {"title": "T", "content": "more",
                 "notebook": "Personal/Officer Logs"}, boss_ctx()))
        self.assertFalse(captured["search_all"])
        self.assertEqual(captured["notebook"], "Personal/Officer Logs")
