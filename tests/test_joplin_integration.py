"""
Focused tests for the Joplin read-after-write regression fix.

Background: find_note_by_title() relied solely on Joplin's /search FTS
endpoint, which lags well behind note creation (observed several seconds and
up against the live sidecar). A note Loki had just created via note_create
could not be found by note_read moments later. find_note_in_folder() already
existed as an immediately-consistent alternative but was never used as a
fallback.

Nothing here touches the real Joplin sidecar: _request/_paginated are
replaced with canned responses.

Run:  venv/bin/python -m unittest tests.test_joplin_integration -v
"""

import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("JOPLIN_API_TOKEN", "test-token-not-real")

import joplin_integration as jp


def run(coro):
    return asyncio.run(coro)


FOLDERS = [
    {"id": "loki-root", "title": "Loki", "parent_id": ""},
    {"id": "loki-inbox", "title": "Inbox", "parent_id": "loki-root"},
    {"id": "loki-docs", "title": "Documentation", "parent_id": "loki-root"},
    {"id": "loki-goals", "title": "Loki Goals", "parent_id": ""},
    {"id": "personal-root", "title": "Personal", "parent_id": ""},
    {"id": "personal-recipes", "title": "Recipes", "parent_id": "personal-root"},
]


class DescendantFolders(unittest.TestCase):
    def test_root_plus_nested_children_only(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            ids = run(jp._descendant_folder_ids("loki-root"))
        self.assertEqual(set(ids), {"loki-root", "loki-inbox", "loki-docs"})

    def test_unrelated_branches_excluded(self):
        with mock.patch.object(jp, "get_folder_tree", new=mock.AsyncMock(return_value=FOLDERS)):
            ids = run(jp._descendant_folder_ids("loki-root"))
        self.assertNotIn("personal-root", ids)
        self.assertNotIn("personal-recipes", ids)
        self.assertNotIn("loki-goals", ids)


class FindNoteByTitleImmediateConsistency(unittest.TestCase):
    """The bug: a note created moments ago was invisible to /search, so
    find_note_by_title (and therefore note_read) failed right after write."""

    def test_notebook_scoped_lookup_never_touches_search(self):
        note_stub = {"id": "n1", "title": "Test Note", "parent_id": "loki-docs"}
        full_note = {**note_stub, "body": "the body", "updated_time": 1}
        with mock.patch.object(jp, "resolve_notebook_path",
                              new=mock.AsyncMock(return_value="loki-docs")), \
             mock.patch.object(jp, "find_note_in_folder",
                              new=mock.AsyncMock(return_value=note_stub)), \
             mock.patch.object(jp, "get_note",
                              new=mock.AsyncMock(return_value=full_note)), \
             mock.patch.object(jp, "search_notes",
                              new=mock.AsyncMock(side_effect=AssertionError(
                                  "search_notes must not be called when the "
                                  "immediate folder lookup already succeeded"))):
            result = run(jp.find_note_by_title("Test Note", notebook="Loki/Documentation"))
        self.assertEqual(result, full_note)

    def test_unscoped_lookup_searches_the_loki_namespace_first(self):
        note_stub = {"id": "n2", "title": "Just Created", "parent_id": "loki-inbox"}
        full_note = {**note_stub, "body": "fresh body", "updated_time": 2}
        with mock.patch.object(jp, "resolve_notebook_path",
                              new=mock.AsyncMock(return_value="loki-root")), \
             mock.patch.object(jp, "_find_note_under_folder",
                              new=mock.AsyncMock(return_value=note_stub)), \
             mock.patch.object(jp, "get_note",
                              new=mock.AsyncMock(return_value=full_note)), \
             mock.patch.object(jp, "search_notes",
                              new=mock.AsyncMock(side_effect=AssertionError(
                                  "search_notes must not be called when the "
                                  "Loki-namespace fast path already succeeded"))):
            result = run(jp.find_note_by_title("Just Created"))
        self.assertEqual(result, full_note)

    def test_falls_back_to_search_outside_the_loki_namespace(self):
        """A note the Boss filed by hand elsewhere (e.g. Personal/Recipes)
        isn't reachable by the fast path, so search is still needed there."""
        recipe_hit = {"id": "r1", "title": "Gumbo", "parent_id": "personal-recipes",
                      "body": "roux first"}
        with mock.patch.object(jp, "resolve_notebook_path",
                              new=mock.AsyncMock(return_value="loki-root")), \
             mock.patch.object(jp, "_find_note_under_folder",
                              new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(jp, "search_notes",
                              new=mock.AsyncMock(return_value=[recipe_hit])):
            result = run(jp.find_note_by_title("Gumbo"))
        self.assertEqual(result, recipe_hit)

    def test_fast_path_result_always_includes_the_body(self):
        """find_note_in_folder only requests id/title/parent_id for speed;
        find_note_by_title must enrich it before returning, or note_read
        silently renders an empty body."""
        note_stub = {"id": "n3", "title": "No Body Yet", "parent_id": "loki-docs"}
        full_note = {**note_stub, "body": "the real content", "updated_time": 3}
        with mock.patch.object(jp, "resolve_notebook_path",
                              new=mock.AsyncMock(return_value="loki-docs")), \
             mock.patch.object(jp, "find_note_in_folder",
                              new=mock.AsyncMock(return_value=note_stub)), \
             mock.patch.object(jp, "get_note",
                              new=mock.AsyncMock(return_value=full_note)) as get_note_mock:
            result = run(jp.find_note_by_title("No Body Yet", notebook="Loki/Documentation"))
        get_note_mock.assert_awaited_once_with("n3")
        self.assertEqual(result["body"], "the real content")

    def test_no_match_anywhere_returns_none(self):
        with mock.patch.object(jp, "resolve_notebook_path",
                              new=mock.AsyncMock(return_value="loki-root")), \
             mock.patch.object(jp, "_find_note_under_folder",
                              new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(jp, "search_notes", new=mock.AsyncMock(return_value=[])):
            result = run(jp.find_note_by_title("Does Not Exist"))
        self.assertIsNone(result)


class TokenNeverLeaks(unittest.TestCase):
    def test_request_error_never_includes_the_token_value(self):
        import inspect
        src = inspect.getsource(jp._request)
        # the token is added to params and sent, never interpolated into a
        # log line or exception message
        self.assertNotIn("JOPLIN_API_TOKEN}", src)


if __name__ == "__main__":
    unittest.main()
