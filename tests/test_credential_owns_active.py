"""The live credential, not the label on it, decides which account is active.

Claude Desktop rewrites ``oauthAccount`` in ``~/.claude.json`` with its own login when a
Code session starts, so the file can name one slot while the live credential belongs to
another.
"""

import json

from tests.test_autoswitch import EngineHarness


def _creds(num: int, refresh: str | None = None) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": f"sk-{num}", "refreshToken": refresh or f"rt-{num}"}})


def _harness(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    return h


def _live(h: EngineHarness, credential: str, file_names: int) -> None:
    """A live credential, and a ``.claude.json`` that names slot ``file_names``."""
    (h.temp_home / ".claude" / ".credentials.json").write_text(credential)
    (h.temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": f"{'ab'[file_names - 1]}@example.com", "accountUuid": f"uuid-{file_names}"},
        "projects": {"keep": "me"},
    }))


def test_the_credential_decides_when_the_file_names_another_slot(temp_home):
    h = _harness(temp_home)
    _live(h, _creds(2), file_names=1)
    assert h.switcher.live_credential_owner() == "2"
    assert h.switcher.current_account_number() == "2"


def test_the_file_stays_the_answer_when_the_credential_matches_no_slot(temp_home):
    h = _harness(temp_home)
    _live(h, _creds(9), file_names=1)
    assert h.switcher.live_credential_owner() is None
    assert h.switcher.current_account_number() == "1"


def test_the_file_stays_the_answer_when_its_slot_has_nothing_stored(temp_home):
    h = EngineHarness(temp_home)
    h.seed(2, "b@example.com")
    # Slot 1 is known, but holds no credential: nothing contradicts the file.
    h.switcher._write_account_config("1", "a@example.com", json.dumps({"oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"}}))
    data = h.switcher._get_sequence_data()
    data["accounts"]["1"] = {"email": "a@example.com", "uuid": "uuid-1", "organizationUuid": "", "organizationName": ""}
    h.switcher._write_json(h.switcher.sequence_file, data)
    _live(h, _creds(2), file_names=1)
    assert h.switcher.current_account_number() == "1"


def test_a_credential_that_two_slots_share_proves_nothing(temp_home):
    h = _harness(temp_home)
    h.switcher._write_account_credentials("1", "a@example.com", _creds(2))  # same lineage as slot 2
    _live(h, _creds(2), file_names=1)
    assert h.switcher.live_credential_owner() is None
    assert h.switcher.current_account_number() == "1"


def test_reconcile_rewrites_the_identity_and_the_active_slot(temp_home):
    h = _harness(temp_home)
    _live(h, _creds(2), file_names=1)
    assert h.switcher.reconcile_identity() == ("1", "2")
    config = json.loads((temp_home / ".claude.json").read_text())
    assert config["oauthAccount"]["emailAddress"] == "b@example.com"
    assert config["projects"] == {"keep": "me"}  # nothing else in the file is touched
    assert h.active_number() == 2
    assert h.switcher.reconcile_identity() is None  # already consistent


def test_reconcile_leaves_a_consistent_file_alone(temp_home):
    h = _harness(temp_home)
    _live(h, _creds(1), file_names=1)
    before = (temp_home / ".claude.json").read_text()
    assert h.switcher.reconcile_identity() is None
    assert (temp_home / ".claude.json").read_text() == before


def test_reconcile_asks_the_profile_when_the_refresh_token_rotated(temp_home, monkeypatch):
    h = _harness(temp_home)
    _live(h, _creds(2, refresh="rt-2-rotated"), file_names=1)  # no stored credential has this lineage
    monkeypatch.setattr("claude_swap.oauth.fetch_oauth_profile",
                        lambda token: {"uuid": "uuid-2", "email": "b@example.com", "organizationUuid": ""})
    assert h.switcher.reconcile_identity() == ("1", "2")
    assert json.loads((temp_home / ".claude.json").read_text())["oauthAccount"]["emailAddress"] == "b@example.com"


def test_an_unanswered_profile_changes_nothing_and_is_not_asked_again_soon(temp_home, monkeypatch):
    h = _harness(temp_home)
    _live(h, _creds(2, refresh="rt-2-rotated"), file_names=1)
    asked = []
    monkeypatch.setattr("claude_swap.oauth.fetch_oauth_profile", lambda token: asked.append(token))
    before = (temp_home / ".claude.json").read_text()
    assert h.switcher.reconcile_identity() is None
    assert h.switcher.reconcile_identity() is None
    assert (temp_home / ".claude.json").read_text() == before
    assert len(asked) == 1


def test_reconcile_never_raises(temp_home, monkeypatch):
    h = _harness(temp_home)
    _live(h, _creds(2), file_names=1)
    monkeypatch.setattr(h.switcher, "_read_credentials", lambda: (_ for _ in ()).throw(OSError("locked")))
    assert h.switcher.reconcile_identity() is None
