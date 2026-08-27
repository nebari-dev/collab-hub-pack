"""The seam that may create an invitee's account (issue #172).

One protocol, named for the authority it carries. Everything that can write to
the identity provider goes through here, so "what may create accounts" is a
question with a single answer in the source rather than a property diffused
through an issuance path.

Why this is its own module and not a method on the directory client
------------------------------------------------------------------
``KeycloakUserDirectoryClient`` is read-only **by construction** — its only
helpers are ``_admin_get`` — and that is worth keeping true rather than
mostly-true. The credential behind it holds ``query-users``, ``view-users`` and
``query-groups``, and a live probe confirms it: every write it attempts is
refused. Adding a create method there would make the class's shape a matter of
discipline instead of a matter of fact.

The write credential is a different client with a different secret. It cannot
read users at all, which is not an inconvenience but the mechanism: the read
lane and the write lane cannot be confused for one another, because neither can
do the other's job.

What the authority actually is, measured
----------------------------------------
Scoped by Keycloak fine-grained admin permissions V2: ``Groups/manage-members``
on **one staging group**, and nothing else. Measured on the deployment rather
than read off the scope names (#172):

* creating a user requires that single permission, and the create call must
  name the group. Creating with no group, or into another group, is refused;
* over a member of that group the same permission also permits password reset,
  email change and deletion — so membership of the staging group means full
  write control, which is why cleanup drains it and why nothing is ever
  re-provisioned once complete;
* authority is evaluated against **current** membership. An account removed
  from the group is immediately out of reach, with no token refresh involved.

That last property is the one this module's callers must respect: classify on
completion **before** attempting any write, because a completed account may
already have been swept.

The disabled implementation is the default
------------------------------------------
A deployment that has not been given a provisioning credential gets
:class:`DisabledAccountProvisioner`, which refuses. It is not a stub to be
replaced later: it is what "this deployment does not pre-create accounts" looks
like, and it is the correct behaviour on every deployment that has not taken
an internal issue's authority decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AccountProvisioningUnavailableError(RuntimeError):
    """No provisioning credential is configured on this deployment."""


class AccountProvisioningError(RuntimeError):
    """The identity provider refused or failed a provisioning call.

    Deliberately distinct from :class:`AccountProvisioningUnavailableError`:
    "this deployment does not do that" and "it tried and did not work" lead to
    different words on a page and different recovery. Collapsing them is how a
    misconfiguration comes to look like an outage.
    """


@dataclass(frozen=True)
class ProvisionedAccount:
    """The result of creating an account.

    ``already_existed`` distinguishes *created* from *adopted* — a retry that
    finds its own marked account from an earlier attempt has succeeded, and the
    caller advances the same phase either way. Recording which happened keeps
    that from being invisible when someone reads the logs of a recovered run.
    """

    user_id: str
    already_existed: bool = False


class InvitationAccountProvisioner(Protocol):
    """Creates an invitee's account, and asks the provider to set it up.

    ``configured`` is part of the contract, not a courtesy of the disabled
    implementation. Callers are expected to decide on it **before** committing
    anything: a refusal discovered by exception halfway through an issuance is
    worse than one decided while there is still nothing to undo. Declared here
    so an implementation cannot omit it and leave every caller to an
    ``AttributeError`` (raised by the review of #179).

    Two calls rather than one, because they fail differently and are resumed
    differently. Creating an account is at-most-once per address and is settled
    by reading the marker back. Sending the setup action is **at-least-once**:
    the effect is idempotent — the account ends carrying the same required
    action — but each call is a real message in somebody's inbox, so a resumed
    run sends another one and the copy has to survive being read twice.
    """

    configured: bool

    def create_account(self, *, email: str, invitation_id: str) -> ProvisionedAccount:
        """Create the account for ``email``, marked as belonging to ``invitation_id``.

        The marker is written **in the create payload**, so an account can never
        exist un-attributed: there is no window in which a created account looks
        like a stranger's. ``emailVerified`` is deliberately *not* asserted —
        Gate B's guarantee is that the identity provider confirmed the address,
        and pre-setting it would downgrade that to "somebody typed it".
        """
        ...

    def send_setup(self, *, user_id: str) -> None:
        """Ask the provider to email the invitee a password-setup action.

        Requires authority over the target, which for this credential means the
        account must still be in the staging group. Recovery therefore only
        calls this for accounts that are **not** complete — a completed account
        may have been swept, and this call would be refused.
        """
        ...

    def mark_complete(self, *, user_id: str) -> None:
        """Record that the invitee has a usable setup path.

        The last write this credential makes to an account. After it, cleanup
        may remove the account from the staging group, and the credential's
        authority over it ends.
        """
        ...


class DisabledAccountProvisioner:
    """What a deployment with no provisioning credential does: refuse, clearly.

    Every method raises the same error, and the invitation flow is expected to
    check ``configured`` rather than to catch it — a refusal discovered by
    exception mid-issuance is a worse outcome than one decided before anything
    is committed.
    """

    configured = False

    def create_account(self, *, email: str, invitation_id: str) -> ProvisionedAccount:
        raise AccountProvisioningUnavailableError(
            "this deployment cannot create invitee accounts: no provisioning credential is configured"
        )

    def send_setup(self, *, user_id: str) -> None:
        raise AccountProvisioningUnavailableError(
            "this deployment cannot create invitee accounts: no provisioning credential is configured"
        )

    def mark_complete(self, *, user_id: str) -> None:
        raise AccountProvisioningUnavailableError(
            "this deployment cannot create invitee accounts: no provisioning credential is configured"
        )


class ServiceAccessGranter(Protocol):
    """Adds an account to a service group (#180).

    **A different seam from :class:`InvitationAccountProvisioner`, mirroring a
    different authority.** Creating an account needs
    ``Groups/manage-members``, which over a member also permits password reset,
    email change and deletion. Adding an account to a group needs
    ``Groups/manage-membership`` plus ``Users/manage-group-membership``, which
    permits membership changes and *nothing else* -- measured on #172, not read
    off the scope names.

    The two must never hold ``manage-members`` and ``manage-membership`` on the
    same group, because together they compose into account takeover: put anyone
    in the group, then rewrite their password. Keeping them as separate
    protocols is how that rule shows up in the source rather than only in an
    issue -- a caller that wants to grant membership cannot reach a method that
    can create or delete.
    """

    configured: bool

    def grant(self, *, user_id: str, group_path: str) -> None:
        """Add ``user_id`` to ``group_path``.

        Idempotent at the provider: adding an existing member is a no-op, which
        is what makes a retry safe and reconciliation cheap. Raises
        :class:`ServiceAccessError` if the provider refuses -- notably if the
        group does not exist, which is a configuration mistake and should read
        as one rather than as an outage.
        """
        ...


class ServiceAccessError(RuntimeError):
    """The provider refused or failed a membership change.

    Separate from :class:`ServiceAccessUnavailableError` for the same reason
    the provisioning errors are separate: "this deployment does not grant
    service access" and "it tried and could not" lead to different words and
    different recovery.
    """


class ServiceAccessUnavailableError(RuntimeError):
    """No credential with membership authority is configured."""


class DisabledServiceAccessGranter:
    """What a deployment without membership authority does.

    The default, and correct wherever the authority decision on
    an internal issue has not been taken. Callers check
    ``configured`` and skip; they do not catch this.
    """

    configured = False

    def grant(self, *, user_id: str, group_path: str) -> None:
        raise ServiceAccessUnavailableError(
            "this deployment cannot grant service access: no membership credential is configured"
        )
