#!/usr/bin/env python3
"""es_api.py: transport failure modes (ES down / timeout / bad JSON), the
System/Game/MediaRef parsing against REAL captured device JSON, missing
media, local-path resolution (and its "the file is gone" case), the TTL
cache, running_game()/selected_game(), and a guard that this module never
builds a request to a mutating endpoint.

Fixtures in tests/fixtures/, captured 2026-09-23 from a live RP5 (root
`/systems`, `/runningGame`, `/caps`, and two systems - gb, sfc - each with
3 games chosen for having the most media types scraped) via
`GET http://127.0.0.1:1234/...`, run ON the device over SSH (rk.py), never
over the network from this PC - see ES-API-NOTES.md for the exact commands.
The `*-localpaths-*` fixtures are a JSON array of 3 individual
`/systems/{s}/games/{id}?localpaths=true` response bodies, batched into one
file by the capture script for convenience - each element is byte-for-byte
what that one endpoint returned, not something ES itself would emit as a
single response.
"""
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import es_api  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def load_fixture_json(name):
    return json.loads(load_fixture_bytes(name).decode("utf-8"))


class FakeResponse:
    """Stands in for the object urllib.request.urlopen() returns: a context
    manager whose __enter__ has .read()."""

    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def opener_returning(data: bytes):
    """An `opener` for ESClient that always answers with `data`, whatever
    the request. Records every call for assertions."""
    calls = []

    def opener(req, timeout=None):
        calls.append((req.full_url, req.get_method(), timeout))
        return FakeResponse(data)

    opener.calls = calls
    return opener


def opener_raising(exc):
    calls = []

    def opener(req, timeout=None):
        calls.append((req.full_url, req.get_method(), timeout))
        raise exc

    opener.calls = calls
    return opener


def opener_by_path(mapping: dict, default=None):
    """Routes each request's path+query to a canned bytes payload, or
    `default` (bytes, or an Exception instance to raise) if unmatched."""
    calls = []

    def opener(req, timeout=None):
        calls.append((req.full_url, req.get_method(), timeout))
        import urllib.parse as up
        path = up.urlsplit(req.full_url).path
        query = up.urlsplit(req.full_url).query
        key = path + ("?" + query if query else "")
        for k, v in mapping.items():
            if key == k or path == k:
                if isinstance(v, Exception):
                    raise v
                return FakeResponse(v)
        if isinstance(default, Exception):
            raise default
        if default is not None:
            return FakeResponse(default)
        raise AssertionError("unexpected request: %s" % key)

    opener.calls = calls
    return opener


# ----------------------------------------------------------------------------
# Transport failure modes - "ES down" must never raise into the caller.
# ----------------------------------------------------------------------------

class TestEsDown(unittest.TestCase):
    """Connection refused - the exact exception urllib raises when nothing
    is listening on 127.0.0.1:1234, i.e. ES is not running at all."""

    def _client(self):
        import urllib.error
        err = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return es_api.ESClient(opener=opener_raising(err))

    def test_systems_is_empty_not_raise(self):
        self.assertEqual(self._client().systems(), [])

    def test_games_is_empty_not_raise(self):
        self.assertEqual(self._client().games("gb"), [])

    def test_running_game_is_none_not_raise(self):
        self.assertIsNone(self._client().running_game())

    def test_game_detail_is_none_not_raise(self):
        self.assertIsNone(self._client().game_detail("gb", "deadbeef"))

    def test_media_for_is_empty_dict_not_raise(self):
        self.assertEqual(self._client().media_for("gb", "deadbeef"), {})

    def test_caps_is_none_not_raise(self):
        self.assertIsNone(self._client().caps())

    def test_selected_game_is_none_always(self):
        # No transport call at all is even expected to happen for this one.
        self.assertIsNone(self._client().selected_game())


class TestTimeout(unittest.TestCase):
    def _client(self):
        return es_api.ESClient(opener=opener_raising(TimeoutError("timed out")))

    def test_systems_is_empty(self):
        self.assertEqual(self._client().systems(), [])

    def test_running_game_is_none(self):
        self.assertIsNone(self._client().running_game())

    def test_timeout_value_is_at_most_one_second(self):
        # The task's hard requirement: short timeouts, <=1s.
        self.assertLessEqual(es_api.DEFAULT_TIMEOUT, 1.0)
        c = es_api.ESClient()
        self.assertLessEqual(c.timeout, 1.0)


class TestBadJson(unittest.TestCase):
    def test_truncated_json_gives_empty_systems(self):
        c = es_api.ESClient(opener=opener_returning(b'[{"name": "gb", "totalG'))
        self.assertEqual(c.systems(), [])

    def test_html_error_page_gives_empty_games(self):
        # "404 system not found" (text/html) is exactly what ES sends for an
        # unknown system name - not JSON at all.
        c = es_api.ESClient(opener=opener_returning(b"404 system not found"))
        self.assertEqual(c.games("no-such-system"), [])

    def test_non_utf8_bytes_do_not_raise(self):
        c = es_api.ESClient(opener=opener_returning(b"\xff\xfe\x00\x01not utf8"))
        self.assertEqual(c.systems(), [])
        self.assertIsNone(c.running_game())

    def test_valid_json_wrong_shape_gives_empty_not_a_crash(self):
        # /systems returning an object instead of an array (e.g. an error
        # body shaped like {"msg": "..."}) must not blow up list comprehension.
        c = es_api.ESClient(opener=opener_returning(b'{"msg": "unexpected"}'))
        self.assertEqual(c.systems(), [])


# ----------------------------------------------------------------------------
# Parsing against real captured device JSON.
# ----------------------------------------------------------------------------

# TestSystemsParsing, TestGamesParsingBulkIsUrlForm, and TestSfcGamesParsing
# removed entirely: each class's setUp() loaded (respectively)
# es-systems-real-capture-2026-09-23.json, es-games-gb-real-capture-
# 2026-09-23.json, and es-games-sfc-real-capture-2026-09-23.json - real
# on-device ES API captures dropped from the public release (see
# DROPPED-FIXTURES-list.txt) - so every test in all three classes errored at
# setUp. No synthetic equivalent exists for this shape.


# ----------------------------------------------------------------------------
# Local-path resolution: the ?localpaths=true detail lookup.
# ----------------------------------------------------------------------------

class TestMediaPathMapping(unittest.TestCase):
    """game_detail()/media_for() against the REAL localpaths capture. The
    files these paths point to only exist on the device, not on this PC, so
    os.path.exists is patched to tell the truth the device already told us
    (every one of these was confirmed present by `ls` during capture - see
    ES-API-NOTES.md) - this test is about the PARSING/mapping logic, not
    about re-proving the device's filesystem state."""

    # setUp() and all three test methods below (test_local_paths_resolved_
    # when_file_exists, test_media_for_is_the_same_dict_as_game_detail_media,
    # test_local_path_none_when_file_does_not_exist_on_this_machine) removed:
    # setUp() loaded es-games-gb-localpaths-real-capture-2026-09-23.json, a
    # real on-device ES API capture dropped from the public release (see
    # DROPPED-FIXTURES-list.txt), so every test in this class errored at
    # setUp. No synthetic equivalent exists for this shape.


class TestMediaPathMappingMissingFileSynthetic(unittest.TestCase):
    """SYNTHETIC: a gamelist entry whose media path was never real on ANY
    machine (guards against the real fixture's paths someday accidentally
    existing on a test runner and hiding this branch)."""

    def test_missing_file_gives_url_fallback(self):
        data = load_fixture_bytes("es-game-localpaths-missing-file-SYNTHETIC.json")
        client = es_api.ESClient(opener=opener_returning(data))
        game = client.game_detail("gb", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        self.assertIsNone(game.media["image"].local_path)
        self.assertIsNone(game.media["manual"].local_path)
        self.assertFalse(game.has_manual() is True and game.media["manual"].is_local)
        # has_manual() is about the media SLOT existing, not the file on disk
        self.assertTrue(game.has_manual())


class TestMissingMedia(unittest.TestCase):
    """SYNTHETIC: an unscraped game with no media fields at all - ES omits
    an empty MD_PATH field entirely rather than sending "" (HttpApi.cpp
    getFileDataJson: `if (!value.empty())`)."""

    def setUp(self):
        data = load_fixture_bytes("es-game-no-media-SYNTHETIC.json")
        self.client = es_api.ESClient(opener=opener_returning(data))
        self.game = self.client.game_detail("gb", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_media_dict_is_empty(self):
        self.assertEqual(self.game.media, {})

    def test_has_manual_false(self):
        self.assertFalse(self.game.has_manual())
        self.assertIsNone(self.game.manual())

    def test_name_and_core_fields_still_present(self):
        self.assertEqual(self.game.name, "Unscraped Game (No Media)")
        self.assertEqual(self.game.system, "gb")


# ----------------------------------------------------------------------------
# running_game() and selected_game().
# ----------------------------------------------------------------------------

# TestRunningGameNoneRunning and TestRunningGameActive removed entirely:
# their tests loaded es-runningGame-real-capture-2026-09-23.json and/or
# es-games-gb-real-capture-2026-09-23.json /
# es-games-gb-localpaths-real-capture-2026-09-23.json - real on-device ES
# API captures dropped from the public release (see
# DROPPED-FIXTURES-list.txt). No synthetic equivalent exists for this shape.


class TestSelectedGame(unittest.TestCase):
    """SOURCED finding: no endpoint in HttpServerThread.cpp exposes the
    highlighted-but-not-launched game. selected_game() always returns None,
    and must do so WITHOUT making any HTTP call at all."""

    def test_always_none_and_makes_no_request(self):
        opener = opener_raising(AssertionError("selected_game() must not call the transport"))
        client = es_api.ESClient(opener=opener)
        self.assertIsNone(client.selected_game())
        self.assertEqual(es_api.selected_game.__module__, "es_api")  # module fn exists too
        self.assertIsNone(es_api.ESClient(opener=opener).selected_game())


# ----------------------------------------------------------------------------
# TTL cache: systems()/games() only, never running_game().
# ----------------------------------------------------------------------------

class TestTTLCache(unittest.TestCase):
    def test_systems_is_cached_within_ttl(self):
        # Previously loaded tests/fixtures/es-caps-real-capture-2026-09-23.json
        # here ("any valid-ish JSON") but never actually used the result -
        # opener_returning(b"[]") below is what the client is built with.
        # That fixture is dropped from the public release (see
        # DROPPED-FIXTURES-list.txt); removing the dead load changes nothing
        # about what this test exercises.
        opener = opener_returning(b"[]")
        client = es_api.ESClient(opener=opener, cache_ttl=10.0)
        client.systems()
        client.systems()
        client.systems()
        self.assertEqual(len(opener.calls), 1)

    def test_games_cached_per_system_independently(self):
        opener = opener_returning(b"[]")
        client = es_api.ESClient(opener=opener, cache_ttl=10.0)
        client.games("gb")
        client.games("gb")
        client.games("sfc")
        self.assertEqual(len(opener.calls), 2)   # gb once (cached 2nd time), sfc once

    def test_cache_expires_after_ttl(self):
        opener = opener_returning(b"[]")
        client = es_api.ESClient(opener=opener, cache_ttl=0.05)
        client.systems()
        with mock.patch("es_api.time.monotonic", return_value=__import__("time").monotonic() + 999):
            client.systems()
        self.assertEqual(len(opener.calls), 2)

    def test_running_game_is_never_cached(self):
        opener = opener_returning(b'{"msg":"NO GAME RUNNING"}')
        client = es_api.ESClient(opener=opener, cache_ttl=999.0)
        client.running_game()
        client.running_game()
        client.running_game()
        self.assertEqual(len(opener.calls), 3)   # NOT deduped, unlike systems()/games()

    def test_clear_cache_forces_refetch(self):
        opener = opener_returning(b"[]")
        client = es_api.ESClient(opener=opener, cache_ttl=999.0)
        client.systems()
        client.clear_cache()
        client.systems()
        self.assertEqual(len(opener.calls), 2)


# ----------------------------------------------------------------------------
# Never touches a mutating endpoint.
# ----------------------------------------------------------------------------

class TestNeverMutates(unittest.TestCase):
    def test_every_used_path_is_marked_non_mutating_in_the_route_table(self):
        by_method_path = {(e["method"], e["path"]): e for e in es_api.ES_HTTP_ENDPOINTS}
        for used in es_api._USED_ENDPOINT_PATHS:
            self.assertIn(used, by_method_path, used)
            self.assertFalse(by_method_path[used]["mutating"], used)

    # test_all_requests_are_GET removed: depended on
    # tests/fixtures/es-systems-real-capture-2026-09-23.json, dropped from
    # the public release (see DROPPED-FIXTURES-list.txt).

    def test_used_endpoint_set_excludes_every_mutating_path(self):
        mutating = {(e["method"], e["path"]) for e in es_api.ES_HTTP_ENDPOINTS if e["mutating"]}
        self.assertEqual(es_api._USED_ENDPOINT_PATHS & mutating, set())


class TestRuntimeAllowlistGuard(unittest.TestCase):
    """_get() must refuse anything not shaped like one of the 6 allowed
    routes BEFORE the network is touched - independent of what the calling
    code intended, and independent of _USED_ENDPOINT_PATHS/ES_HTTP_ENDPOINTS
    (which only document intent and are checked statically by
    TestNeverMutates above). Every assertion here is on the injected
    opener's call count, not just the return value, so a guard that let a
    request through but then (say) got a None back from a broken opener
    could not accidentally look like a pass."""

    def _guarded_client(self, **kw):
        opener = opener_raising(AssertionError("guard failed: the transport was reached"))
        return es_api.ESClient(opener=opener, **kw), opener

    def test_every_mutating_route_is_refused(self):
        # ONE path is deliberately excluded here: POST
        # /systems/{system}/games/{id} (overwrites game metadata) is
        # registered on the EXACT SAME path string as the GET route this
        # client legitimately uses for the localpaths detail lookup -
        # ES's own router tells them apart by HTTP METHOD, not path, and a
        # path-shape allowlist structurally cannot do that split. This
        # collision is safe ONLY because this client's requests are always
        # GET, which is a SEPARATE, already-tested invariant
        # (TestNeverMutates.test_all_requests_are_GET) - see
        # test_the_one_shared_path_is_only_ever_requested_as_GET below,
        # which ties the two guarantees together explicitly.
        shared_with_a_safe_get_route = {"/systems/{system}/games/{id}"}

        client, opener = self._guarded_client()
        for entry in es_api.ES_HTTP_ENDPOINTS:
            if not entry["mutating"] or entry["path"] in shared_with_a_safe_get_route:
                continue
            concrete = re.sub(r"\{[^}]+\}", "x", entry["path"])
            self.assertIsNone(client._get(concrete), concrete)
        self.assertEqual(len(opener.calls), 0)

    def test_the_one_shared_path_is_only_ever_requested_as_GET(self):
        # The path this client sends for game_detail()/media_for() is
        # IDENTICAL to the mutating POST route's path. Prove the two
        # guarantees that together make this safe: (1) the guard allows
        # this exact path shape (by design - it's the localpaths lookup),
        # and (2) urllib.request.Request defaults to GET here, never POST,
        # for this exact call.
        data = json.dumps({"id": "x", "name": "n", "systemName": "gb"}).encode("utf-8")
        opener = opener_returning(data)
        client = es_api.ESClient(opener=opener)
        client.game_detail("gb", "x")
        self.assertEqual(len(opener.calls), 1)
        _url, method, _timeout = opener.calls[0]
        self.assertEqual(method, "GET")

    def test_every_allowed_route_shape_is_still_reachable(self):
        # Sanity check: the guard must not be so strict it blocks the
        # legitimate routes too.
        client = es_api.ESClient(opener=opener_returning(b"{}"))
        for method, path in es_api._USED_ENDPOINT_PATHS:
            concrete = path.replace("{system}", "gb").replace("{id}", "abc123")
            self.assertIsNotNone(client._get(concrete), concrete)

    def test_traversal_via_system_name_is_refused(self):
        client, opener = self._guarded_client()
        self.assertEqual(client.games("../quit"), [])
        self.assertIsNone(client.game_detail("../quit", "id"))
        self.assertEqual(client.media_for("../quit", "id"), {})
        self.assertEqual(len(opener.calls), 0)

    def test_traversal_via_game_id_is_refused(self):
        client, opener = self._guarded_client()
        self.assertIsNone(client.game_detail("gb", "1/../../shutdown"))
        self.assertEqual(client.media_for("gb", "1/../../shutdown"), {})
        self.assertEqual(len(opener.calls), 0)

    def test_preencoded_slash_traversal_is_refused(self):
        # A literal "%2F" reaching _get() directly (bypassing this module's
        # own quote() calls) must still be caught as-a-substring, not
        # decoded-then-checked.
        client, opener = self._guarded_client()
        self.assertIsNone(client._get("/systems/gb/games/foo%2F..%2Fshutdown"))
        self.assertEqual(len(opener.calls), 0)

    def test_query_string_does_not_bypass_the_guard(self):
        client, opener = self._guarded_client()
        self.assertIsNone(client._get("/quit?confirm=menu"))
        self.assertEqual(len(opener.calls), 0)

    def test_catchall_and_resource_paths_are_refused(self):
        client, opener = self._guarded_client()
        for path in ("/resources/theme.xml", "/some/random/path", "/index.html", "/"):
            self.assertIsNone(client._get(path), path)
        self.assertEqual(len(opener.calls), 0)


class TestPathIsAllowedUnit(unittest.TestCase):
    """Direct tests of _path_is_allowed()/_is_safe_segment(), independent of
    ESClient/transport - kept separate so the break/restore proof can target
    the guard function precisely."""

    def test_allowed_shapes(self):
        self.assertTrue(es_api._path_is_allowed("/caps"))
        self.assertTrue(es_api._path_is_allowed("/systems"))
        self.assertTrue(es_api._path_is_allowed("/runningGame"))
        self.assertTrue(es_api._path_is_allowed("/systems/gb"))
        self.assertTrue(es_api._path_is_allowed("/systems/gb/games"))
        self.assertTrue(es_api._path_is_allowed("/systems/gb/games/abc123"))

    def test_mutating_routes_refused(self):
        for p in ("/quit", "/shutdown", "/restart", "/emukill", "/reloadgames",
                  "/messagebox", "/notify", "/storage/event", "/launch",
                  "/addgames/gb", "/removegames/gb",
                  "/systems/gb/games/abc/media/image"):
            self.assertFalse(es_api._path_is_allowed(p), p)

    def test_traversal_segments_refused(self):
        self.assertFalse(es_api._path_is_allowed("/systems/..%2Fquit"))
        self.assertFalse(es_api._path_is_allowed("/systems/gb/games/1%2F..%2F..%2Fshutdown"))
        self.assertFalse(es_api._path_is_allowed("/systems/gb/../../shutdown"))

    def test_is_safe_segment(self):
        self.assertTrue(es_api._is_safe_segment("gb"))
        self.assertTrue(es_api._is_safe_segment("3634b46a9e5f4906c8af487c10268f3d"))
        for bad in ("", "..", "a/b", "a..b", "a?b", "a#b", "a%2fb", "A%2FB"):
            self.assertFalse(es_api._is_safe_segment(bad), bad)


# ----------------------------------------------------------------------------
# Small pure helpers.
# ----------------------------------------------------------------------------

class TestBoolStr(unittest.TestCase):
    def test_true_false_strings(self):
        self.assertIs(es_api._bool_str("true"), True)
        self.assertIs(es_api._bool_str("false"), False)

    def test_real_bool_passthrough(self):
        self.assertIs(es_api._bool_str(True), True)
        self.assertIs(es_api._bool_str(False), False)

    def test_garbage_is_none_not_a_guess(self):
        self.assertIsNone(es_api._bool_str(None))
        self.assertIsNone(es_api._bool_str(""))
        self.assertIsNone(es_api._bool_str("TRUE"))
        self.assertIsNone(es_api._bool_str(1))


class TestIntOr(unittest.TestCase):
    def test_parses_numeric_strings(self):
        self.assertEqual(es_api._int_or("42"), 42)

    def test_falls_back_on_garbage(self):
        self.assertEqual(es_api._int_or("not a number", default=-1), -1)
        self.assertEqual(es_api._int_or(None, default=7), 7)


class TestMediaRef(unittest.TestCase):
    def test_best_prefers_local(self):
        ref = es_api.MediaRef(kind="image", url="http://x/y", local_path="/a/b.png")
        self.assertEqual(ref.best, "/a/b.png")
        self.assertTrue(ref.is_local)

    def test_best_falls_back_to_url(self):
        ref = es_api.MediaRef(kind="image", url="http://x/y", local_path=None)
        self.assertEqual(ref.best, "http://x/y")
        self.assertFalse(ref.is_local)


if __name__ == "__main__":
    unittest.main()
