"""The ``collab-hub`` command line.

Exit codes: 0 success; 1 the hub refused or failed the request, or could not
be reached; 2 a usage error; 5 not signed in, or the session cannot be used.
Messages go to stderr, results to stdout, and ``--json`` makes stdout one JSON
document a script can hand to ``jq``.
"""

from __future__ import annotations

import base64
import json
import sys
import webbrowser
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Any

import typer

from . import config, credentials, oidc
from .credentials import Credentials
from .hub import Hub, HubError, http_client
from .oidc import AuthError, RealmError

EXIT_HUB = 1
EXIT_USAGE = 2
EXIT_AUTH = 5

app = typer.Typer(
    name="collab-hub",
    help="A command-line client for the Collab Hub: sign in, then call the hub's REST API.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
cog_app = typer.Typer(help="The Cogs the hub offers.", no_args_is_help=True)
app.add_typer(cog_app, name="cog")

HubOption = Annotated[
    str | None, typer.Option("--hub", envvar="COLLAB_HUB_URL", help="The hub's URL; overrides the profile's.")
]
ProfileOption = Annotated[
    str | None, typer.Option("--profile", envvar="COLLAB_HUB_PROFILE", help="The profile to use.")
]
InsecureOption = Annotated[bool, typer.Option(
    "--insecure", help="Accept plain http for a hub or realm off this machine: tokens then cross in clear.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON document for scripts.")]


class State:
    hub: str | None = None
    profile: str | None = None
    insecure: bool = False


state = State()


@app.callback()
def main(hub: HubOption = None, profile: ProfileOption = None, insecure: InsecureOption = False) -> None:
    state.hub, state.profile, state.insecure = hub, profile, insecure


def _err(message: str) -> None:
    typer.echo(message, err=True)


def handled(command: Callable) -> Callable:
    """Turn the CLI's own failures into a message on stderr and their exit code."""

    @wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return command(*args, **kwargs)
        except config.UsageError as exc:
            _err(f"error: {exc}")
            raise typer.Exit(EXIT_USAGE) from None
        except AuthError as exc:
            _err(f"error: {exc}")
            raise typer.Exit(EXIT_AUTH) from None
        except (HubError, RealmError) as exc:
            _err(f"error: {exc}")
            raise typer.Exit(EXIT_HUB) from None

    return wrapper


def _target() -> config.Target:
    return config.resolve(state.hub, state.profile, insecure=state.insecure)


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def _table(headers: list[str], rows: Iterable[list[Any]]) -> None:
    rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    widths = [max([len(h)] + [len(row[i]) for row in rows]) for i, h in enumerate(headers)]
    for row in [headers, *rows]:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def _when(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token_expiry(token: str) -> float | None:
    """A JWT's ``exp``, read without verifying it: only to know when to stop sending it."""

    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


# --- signing in ------------------------------------------------------------------------------


def _end(session: Credentials, insecure: bool) -> str | None:
    """End a session at its realm, best effort: ``None`` when it ended, else why it may still be open."""

    if not (session.refresh_token and session.issuer and session.client_id):
        return None
    http = http_client()
    try:
        oidc.sign_out(http, oidc.discover(http, session.issuer, insecure), session.client_id, session.refresh_token)
        return None
    except (AuthError, RealmError) as exc:
        return str(exc)
    finally:
        http.close()


def _same_realm_session(old: Credentials, new: Credentials) -> bool:
    """Whether two sessions are one realm session, which ending the old one would end for the new one too.

    A browser that is still signed in to the realm hands a second sign-in the
    same realm session (the same ``sid``). When either ``sid`` is unknown the
    answer is yes, so a session is never ended from under the one replacing it.
    """

    if old.issuer != new.issuer:
        return False
    old_sid = oidc.session_id(old.access_token) or oidc.session_id(old.refresh_token)
    new_sid = oidc.session_id(new.access_token) or oidc.session_id(new.refresh_token)
    return old_sid is None or new_sid is None or old_sid == new_sid


@app.command()
@handled
def login(
    with_token: Annotated[bool, typer.Option(
        "--with-token", help="Read a bearer token from stdin instead of signing in through the browser.")] = False,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Print the sign-in URL without opening a browser.")] = False,
    port: Annotated[int, typer.Option(
        "--port", min=0, max=65535,
        help="The 127.0.0.1 port the sign-in returns to; 0 lets the system pick. Fix it to forward it over SSH "
             "(ssh -L PORT:127.0.0.1:PORT) when the CLI runs on a remote host.")] = 0,
    as_json: JsonOption = False,
) -> None:
    """Sign in to the hub through its realm, the way the Collab desktop does.

    Opens the realm's sign-in page in a browser on this machine, which sends
    you back to a listener on 127.0.0.1. The session is kept for this profile
    and renewed as it expires. On a hub running dev auth there is nothing to
    sign in to, and the command says so.
    """

    target = _target()
    url = target.require_hub()
    previous = credentials.load(target.directory, target.profile)
    with Hub(target) as hub:
        auth = hub.get_json("/v1/auth/cli", authenticate=False)
        issuer, client_id = auth.get("issuer"), auth["client_id"]

        if with_token:
            token = sys.stdin.read().strip()
            if not token:
                raise config.UsageError("--with-token reads the token from stdin, and stdin was empty")
            session = Credentials(hub=url, access_token=token, issuer=issuer, client_id=client_id,
                                  expires_at=_token_expiry(token), obtained_by="token")
        elif issuer is None:
            if not auth.get("dev_auth"):
                raise HubError(f"{url} verifies no bearer tokens and runs no dev auth: there is no way to sign in")
            config.remember(target)
            _err(f"{url} runs dev auth: there is nothing to sign in to, and requests are answered as its "
                 "development user without a token.")
            if as_json:
                _print_json({"hub": url, "profile": target.profile, "signed_in": False, "dev_auth": True})
            return
        else:
            metadata = oidc.discover(hub.http, issuer, target.insecure)

            def show(sign_in_url: str) -> None:
                _err(f"Sign in to {url} in your browser:\n\n  {sign_in_url}\n")
                if no_browser:
                    _err("Open it in a browser on this machine: it returns to a listener on 127.0.0.1.")

            tokens = oidc.browser_login(hub.http, metadata, client_id,
                                        open_browser=(lambda _url: None) if no_browser else webbrowser.open,
                                        show=show, port=port)
            session = Credentials(hub=url, access_token=tokens.access_token, issuer=issuer, client_id=client_id,
                                  refresh_token=tokens.refresh_token, expires_at=tokens.expires_at)

        # Keep the session only once the hub accepts it; a browser session it
        # refused is ended rather than left open at the realm with nothing holding it.
        hub.session = session
        try:
            me = hub.get_json("/v1/me")
        except (AuthError, HubError, RealmError):
            if session.obtained_by == "browser":
                _end(session, target.insecure)
            raise
        credentials.save(target.directory, target.profile, session)
        config.remember(target)
    # The session this profile held before is replaced, so end it at its realm, unless it is
    # the same realm session the new one belongs to. A failure warns and never undoes the sign-in.
    ended_previous, warning = False, None
    if previous is not None and previous.refresh_token and not _same_realm_session(previous, session):
        warning = _end(previous, target.insecure)
        ended_previous = warning is None
        if warning:
            warning = f"the previous session ({previous.hub}) may still be open at its realm: {warning}"
            _err(f"warning: {warning}")
    organization = f" in {me['org_id']}" if me.get("org_id") else ""
    _err(f"Signed in to {url} as {me['user']}{organization}.")
    if as_json:
        _print_json({**me, "hub": url, "profile": target.profile, "signed_in": True, "dev_auth": False,
                     "obtained_by": session.obtained_by, "token_expires_at": _when(session.expires_at),
                     "previous_session_ended": ended_previous, "warning": warning})


@app.command()
@handled
def logout(as_json: JsonOption = False) -> None:
    """Sign out: end the realm session, revoke its refresh token, and forget the stored token."""

    target = _target()
    session = credentials.load(target.directory, target.profile)
    if session is None:
        _err(f"Not signed in (profile {target.profile}).")
        if as_json:
            _print_json({"hub": target.hub, "profile": target.profile, "was_signed_in": False, "revoked": False,
                         "warning": None})
        return
    problem = None
    revoked = False
    if session.refresh_token and session.issuer and session.client_id:
        failure = _end(session, target.insecure)
        revoked = failure is None
        if failure:
            problem = f"{failure}; the stored token is deleted, but the realm session may still be open"
    elif session.obtained_by == "token":
        problem = "the token was given to `login --with-token`; it is forgotten here and stays valid until it expires"
    credentials.delete(target.directory, target.profile)
    _err(f"Signed out of {session.hub}.")
    if problem:
        _err(f"warning: {problem}")
    if as_json:
        _print_json({"hub": session.hub, "profile": target.profile, "was_signed_in": True, "revoked": revoked,
                     "warning": problem})
    if problem and session.obtained_by != "token":
        raise typer.Exit(EXIT_HUB)


@app.command()
@handled
def whoami(as_json: JsonOption = False) -> None:
    """Who the hub says you are: the user, the organization and its roles, and how you signed in."""

    target = _target()
    with Hub(target) as hub:
        me = hub.get_json("/v1/me")
        session = hub.session
    signed_in = me["authenticated_by"] == "token"
    expires_at = session.expires_at if session is not None and signed_in else None
    if as_json:
        _print_json({**me, "hub": hub.url, "profile": target.profile, "signed_in": signed_in,
                     "token_expires_at": _when(expires_at)})
        return
    organization = me.get("org_id") or "none"
    if me.get("org_role"):
        organization += f" ({me['org_role']})"
    rows = [["hub", f"{hub.url}  (profile {target.profile})"], ["user", me["user"]]]
    if me.get("name") or me.get("email"):
        rows.append(["name", " ".join(part for part in (me.get("name"), f"<{me['email']}>" if me.get("email")
                                                          else None) if part)])
    rows.append(["organization", organization])
    if me.get("platform_role"):
        rows.append(["platform role", me["platform_role"]])
    if signed_in:
        rows.append(["signed in", f"yes, token expires {_when(expires_at)}" if expires_at else "yes"])
    else:
        rows.append(["signed in", "no: unauthenticated dev auth, the hub answers every request as this user"])
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")


# --- the Cogs the hub offers ------------------------------------------------------------------


@cog_app.command("list")
@handled
def cog_list(
    kind: Annotated[str | None, typer.Option(help="Only Cogs of this kind, e.g. complete or context.")] = None,
    publisher: Annotated[str | None, typer.Option(help="Only this publisher's Cogs.")] = None,
    provides: Annotated[str | None, typer.Option(help="Only Cogs that provide this.")] = None,
    requires: Annotated[str | None, typer.Option(help="Only Cogs that require this capability.")] = None,
    accepts: Annotated[str | None, typer.Option(help="Only Cogs that accept this io type.")] = None,
    produces: Annotated[str | None, typer.Option(help="Only Cogs that produce this io type.")] = None,
    source_id: Annotated[str | None, typer.Option(
        "--source-id", help="Only this registry source; the newest version is chosen within it.")] = None,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Text in the name or description.")] = None,
    as_json: JsonOption = False,
) -> None:
    """List the Cogs in the hub's catalog, each at its newest version, following every page."""

    filters = {"kind": kind, "publisher": publisher, "provides": provides, "requires": requires,
               "accepts": accepts, "produces": produces, "source_id": source_id, "q": query}
    with Hub(_target()) as hub:
        items = list(hub.pages("/v1/cogs", {key: value for key, value in filters.items() if value is not None}))
    if as_json:
        _print_json(items)
        return
    if not items:
        _err("The catalog has no Cogs.")
        return
    _table(["COG", "VERSION", "KIND", "DESCRIPTION"], [
        [item["cog_id"], item.get("version") or item["digest"][:19], item["card"].get("kind"),
         _clip(item["card"].get("description"))]
        for item in items
    ])


def _clip(text: Any, width: int = 60) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


@cog_app.command("show")
@handled
def cog_show(cog_id: Annotated[str, typer.Argument(help="The Cog's id, <publisher>/<name>.")],
             as_json: JsonOption = False) -> None:
    """Show one Cog: its card at the newest version, and every indexed version."""

    with Hub(_target()) as hub:
        cog = hub.get_json(f"/v1/cogs/{cog_id}")
    if as_json:
        _print_json(cog)
        return
    card = cog.get("card", {})
    rows = [["cog", cog["cog_id"]], ["name", card.get("name")], ["kind", card.get("kind")],
            ["publisher", card.get("publisher")], ["version", cog.get("version")], ["digest", cog["digest"]],
            ["reference", cog["reference"]], ["description", _clip(card.get("description"), 100)]]
    rows = [row for row in rows if row[1]]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")
    typer.echo("")
    _table(["VERSION", "DIGEST", "TAGS", "REMOVED"], [
        [version.get("version"), version["digest"][:19], ",".join(version.get("tags", [])),
         "yes" if version.get("removed_at") else ""]
        for version in cog.get("versions", [])
    ])


def run() -> None:
    app()


if __name__ == "__main__":
    run()
