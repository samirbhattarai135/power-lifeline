"""Who may do what. Every signed-in person has exactly one role; each role grants a fixed
set of permissions, and the HTTP layer checks a permission on every staff request.
Signing in proves who someone is; this module decides what they can see and do."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Role(Enum):
    ADMIN = "admin"      # runs the agency's Lifeline: patients, staff, everything below
    NURSE = "nurse"      # on call: watches the board, responds, reads patient details
    VIEWER = "viewer"    # dispatcher or supervisor: sees the board and history, changes nothing
    COUNTY = "county"    # county emergency management: only the county view, no phone numbers

    @property
    def label(self) -> str:
        return {"admin": "Admin", "nurse": "Nurse", "viewer": "Viewer", "county": "County emergency management"}[self.value]


class Permission(Enum):
    VIEW_BOARD = "view_board"
    RESPOND = "respond"
    VIEW_HISTORY = "view_history"
    VIEW_PATIENTS = "view_patients"        # names, call lists and phone numbers
    MANAGE_PATIENTS = "manage_patients"    # add, edit, remove; unit tokens and family links
    MANAGE_STAFF = "manage_staff"
    VIEW_COUNTY = "view_county"


_STAFF_BOARD = {Permission.VIEW_BOARD, Permission.VIEW_HISTORY}
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.NURSE: frozenset(_STAFF_BOARD | {Permission.RESPOND, Permission.VIEW_PATIENTS}),
    Role.VIEWER: frozenset(_STAFF_BOARD),
    Role.COUNTY: frozenset({Permission.VIEW_COUNTY}),
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254


class InvalidStaffChange(ValueError):
    """A role change refused for a reason staff can act on; the message is shown to them."""


@dataclass(frozen=True)
class StaffAccount:
    email: str
    role: Role

    def __post_init__(self) -> None:
        if self.email != self.email.strip().lower() or len(self.email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(self.email):
            raise InvalidStaffChange("Enter a valid email address")

    def can(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]


class StaffStore(Protocol):
    def list_all(self) -> list[StaffAccount]: ...
    def save(self, account: StaffAccount) -> None: ...
    def delete(self, email: str) -> None: ...


class StaffDirectory:
    """Roles held in memory and written through to the store, so the per-request check costs
    no database call. `bootstrap_admin` (LIFELINE_BOOTSTRAP_ADMIN) is an admin whenever it has
    no stored role, so a fresh install has someone who can add everyone else."""

    def __init__(self, store: StaffStore, accounts: list[StaffAccount], bootstrap_admin: str | None = None):
        self._store = store
        self._accounts = {a.email: a for a in accounts}
        self._bootstrap_admin = bootstrap_admin.strip().lower() if bootstrap_admin else None
        self._lock = threading.Lock()

    def account(self, email: str) -> StaffAccount | None:
        email = email.strip().lower()
        with self._lock:
            found = self._accounts.get(email)
        if found is None and email == self._bootstrap_admin:
            return StaffAccount(email, Role.ADMIN)
        return found

    def accounts(self) -> list[StaffAccount]:
        with self._lock:
            listed = dict(self._accounts)
        if self._bootstrap_admin and self._bootstrap_admin not in listed:
            listed[self._bootstrap_admin] = StaffAccount(self._bootstrap_admin, Role.ADMIN)
        return sorted(listed.values(), key=lambda a: (list(Role).index(a.role), a.email))

    def set_role(self, changed_by: str, email: str, role: Role) -> StaffAccount:
        account = StaffAccount(email.strip().lower(), role)
        with self._lock:
            if account.email == changed_by and role is not Role.ADMIN:
                raise InvalidStaffChange("You can't take away your own admin role. Ask another admin.")
            self._store.save(account)
            self._accounts[account.email] = account
        return account

    def remove(self, removed_by: str, email: str) -> None:
        email = email.strip().lower()
        with self._lock:
            if email == removed_by:
                raise InvalidStaffChange("You can't remove yourself. Ask another admin.")
            if email == self._bootstrap_admin:
                raise InvalidStaffChange("This admin is set in the server's LIFELINE_BOOTSTRAP_ADMIN and can't be removed here.")
            self._store.delete(email)
            self._accounts.pop(email, None)
