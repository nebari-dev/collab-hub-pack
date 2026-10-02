"""A session's life at the realm: kept through failures, ended when replaced, never trusted blindly."""

from __future__ import annotations

import json

import httpx

from collab_hub_cli import credentials, oidc

from .conftest import HUB, ISSUER, OTHER_HUB, jwt


def _saved(tmp_path, profile="default"):
    return credentials.load(tmp_path / "config", profile)


def _expire(monkeypatch):
    monkeypatch.setattr(oidc, "clock", lambda: 1_000_000.0 + 3600)


# --- renewal: only a refused grant ends the session ---------------------------------------------


def test_a_realm_that_fails_during_renewal_keeps_the_session_for_the_next_command(stub, cli, tmp_path, monkeypatch):
    assert cli("--hub", HUB, "login").exit_code == 0
    token = stub.issued[0]["refresh_token"]
    _expire(monkeypatch)
    stub.fail["refresh"] = 503
    result = cli("whoami")
    assert result.exit_code == 1 and "could not renew the session right now (HTTP 503" in result.stderr
    assert _saved(tmp_path).refresh_token == token and stub.alive(token)
    # The realm is back: the same session renews.
    del stub.fail["refresh"]
    assert cli("whoami").exit_code == 0
    assert _saved(tmp_path).refresh_token == stub.issued[1]["refresh_token"]


def test_a_realm_unreachable_during_renewal_is_exit_1_not_a_traceback(stub, cli, tmp_path, monkeypatch):
    assert cli("--hub", HUB, "login").exit_code == 0
    _expire(monkeypatch)
    stub.fail["refresh"] = httpx.ConnectError("connection reset")
    result = cli("whoami")
    assert result.exit_code == 1 and "cannot reach the realm" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _saved(tmp_path) is not None


def test_a_realm_whose_discovery_fails_during_renewal_keeps_the_session(stub, cli, tmp_path, monkeypatch):
    assert cli("--hub", HUB, "login").exit_code == 0
    _expire(monkeypatch)
    stub.fail["discovery"] = 502
    assert cli("whoami").exit_code == 1
    assert _saved(tmp_path) is not None


# --- sign-out: the file goes whatever the network does ---------------------------------------------


def test_logout_deletes_the_file_even_when_the_realm_is_unreachable(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    stub.fail["logout"] = httpx.ConnectError("connection refused")
    result = cli("logout", "--json")
    assert result.exit_code == 1 and "cannot reach the realm" in result.stderr
    assert json.loads(result.stdout)["revoked"] is False
    assert _saved(tmp_path) is None


# --- a new sign-in ends the session it replaces -------------------------------------------------


def test_signing_in_again_ends_the_previous_realm_session(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    first = stub.issued[0]["refresh_token"]
    result = cli("--hub", HUB, "login", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["previous_session_ended"] is True
    assert not stub.alive(first)
    assert stub.alive(stub.issued[1]["refresh_token"]) and _saved(tmp_path).refresh_token == stub.issued[1][
        "refresh_token"]


def test_a_second_sign_in_that_joined_the_same_realm_session_does_not_end_it(stub, cli, tmp_path):
    # A browser still signed in to the realm gives the second sign-in the same realm session:
    # ending the old token's session would end the new one too.
    stub.sso = True
    assert cli("--hub", HUB, "login").exit_code == 0
    result = cli("--hub", HUB, "login", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["previous_session_ended"] is False
    assert stub.ended == [] and stub.revoked == []
    assert stub.alive(_saved(tmp_path).refresh_token)


def test_signing_in_to_another_hub_ends_the_session_the_profile_held(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    first = stub.issued[0]["refresh_token"]
    assert cli("--hub", OTHER_HUB, "login").exit_code == 0
    assert not stub.alive(first)
    assert _saved(tmp_path).hub == OTHER_HUB


def test_a_given_token_replacing_a_browser_session_ends_that_session(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    first = stub.issued[0]["refresh_token"]
    token = jwt({"sid": "ci-session", "exp": 1_000_600})
    stub.users[token] = "ci-bot"
    assert cli("--hub", HUB, "login", "--with-token", input=token).exit_code == 0
    assert not stub.alive(first)
    assert _saved(tmp_path).access_token == token


def test_a_session_whose_realm_session_is_unknown_is_never_ended_from_under_its_replacement(stub, cli):
    assert cli("--hub", HUB, "login").exit_code == 0
    stub.users["opaque-token"] = "ci-bot"  # no sid: it may belong to the same realm session
    assert cli("--hub", HUB, "login", "--with-token", input="opaque-token").exit_code == 0
    assert stub.ended == [] and stub.revoked == []


def test_a_sign_in_the_hub_refuses_is_ended_at_the_realm_and_not_kept(stub, cli, tmp_path):
    stub.fail["me"] = 500
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 1
    assert not stub.alive(stub.issued[0]["refresh_token"])
    assert _saved(tmp_path) is None


def test_a_realm_that_cannot_end_the_previous_session_warns_and_still_signs_in(stub, cli, tmp_path):
    assert cli("--hub", HUB, "login").exit_code == 0
    stub.fail["logout"] = 500
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 0
    assert "the previous session" in result.stderr and "may still be open" in result.stderr
    assert _saved(tmp_path).refresh_token == stub.issued[1]["refresh_token"]


# --- what the CLI trusts ------------------------------------------------------------------------


def test_a_discovery_document_naming_another_issuer_is_refused_before_any_browser_opens(stub, cli):
    stub.discovery = {"issuer": "https://attacker.test/realms/x"}
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 1 and "names another issuer" in result.stderr
    assert stub.authorizations == []


def test_a_realm_endpoint_over_plain_http_off_this_machine_is_refused(stub, cli):
    stub.discovery = {"token_endpoint": "http://id.test/token"}
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 1 and "refusing the realm's token_endpoint" in result.stderr
    assert stub.authorizations == []


def test_plain_http_is_for_this_machine_only_unless_insecure(stub, cli, tmp_path):
    refused = cli("--hub", "http://hub.example.org", "whoami")
    assert refused.exit_code == 2 and "plain http is only for a hub on this machine" in refused.stderr
    for local in ("http://127.0.0.1:8000", "http://localhost:8000", "http://hub.localhost:9080", "http://[::1]:8000"):
        assert "plain http" not in cli("--hub", local, "whoami").stderr, local
    accepted = cli("--insecure", "--hub", "http://hub.example.org", "whoami")
    assert "plain http" not in accepted.stderr


def test_an_issuer_over_plain_http_off_this_machine_is_refused(stub, cli):
    stub.issuer = "http://id.test/realms/nebari"
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 1 and "refusing the issuer" in result.stderr


def test_a_hub_that_does_not_offer_cli_sign_in_says_so_instead_of_asking_to_log_in(stub, cli):
    stub.fail["auth_cli"] = 401
    result = cli("--hub", HUB, "login")
    assert result.exit_code == 1
    assert "does not offer /v1/auth/cli (HTTP 401)" in result.stderr and "collab-hub login" not in result.stderr


def test_the_stub_realm_names_itself(stub):
    # Guards the fixtures above: the unmodified document passes the issuer check.
    with httpx.Client(transport=httpx.MockTransport(stub.handle)) as http:
        assert oidc.discover(http, ISSUER)["issuer"] == ISSUER
