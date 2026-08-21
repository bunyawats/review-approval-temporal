"""
Framework-agnostic Redis-backed per-session UI memory: a resilience
fallback for the BFF's own pagination, plus bulk-selection state --
see docs/SESSION_MEMORY_PLAN.md for the full design.

Deliberately no FastAPI/Starlette imports -- takes a plain redis.Redis
client and a session id string, never a Request -- mirroring
workflow/keycloak_auth.py's placement (shared, reusable infrastructure
that bff/ wraps with the Starlette-specific plumbing, exactly as
bff/keycloak_session.py already wraps keycloak_auth.py's functions).
Not used by api/ today -- REST API callers are stateless bearer-token
clients with no login session to hang this state off of; only bff/ has
a session to key this by.

SESSION_TTL_SECONDS lives here, not in bff/session_store.py, even
though it's really "the auth session's idle timeout" in meaning --
workflow/ must never import from bff/ (see CLAUDE.md's Architecture
section; the same one-way rule workflow/task_queues.py's docstring
describes), so the shared constant has to live at this lower layer for
bff/session_store.py to import upward from, the same direction every
other workflow/->bff/ dependency already runs. Both this module's
ui-memory:<id> blob and session_store.py's ui-session:<id> blob use the
identical value so a session's memory and its auth both expire on the
same idle-timeout horizon -- redeclaring the same number twice in two
places is exactly how those two would silently drift apart after a
future edit to only one.

Deliberately a *separate* Redis key from ui-session:<id>, not a field
merged into that blob: ui-session:<id> is written rarely (login, and
access-token refresh roughly every 5 minutes); ui-memory:<id> is written
far more often (every bulk-select checkbox click). Neither this module
nor session_store.py does locking -- every write is a full
get-mutate-set of the whole blob -- so merging both concerns into one
key would let a token refresh and a selection write silently clobber
each other. Two independent keys can't collide.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import redis.asyncio as redis

SESSION_TTL_SECONDS = 1800
_KEY_PREFIX = "ui-memory:"


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


@dataclass
class PaginationMemory:
    """A session's last-known list-view context for whichever screen
    (operator/manager) it's allowed to see -- a resilience fallback,
    never authoritative: the request's own page/query_id stay the
    source of truth for what to render. Only consulted when those can't
    be resolved another way (an expired/unknown query_id, possibly
    because it landed on a different replica than the one that minted
    it) -- see docs/SESSION_MEMORY_PLAN.md's "Resolution".
    """

    query_id: str
    filter: dict[str, Optional[str]]
    total: int
    cached_at: float

    def is_stale(self, max_age_s: float) -> bool:
        return time.time() - self.cached_at >= max_age_s


@dataclass
class SessionMemory:
    """Everything a login session remembers about its own ephemeral UI
    state, besides identity (which lives in bff/session_store.py's
    separate ui-session:<id> blob instead). One instance per session id,
    stored as one JSON blob under ui-memory:<session id>.
    """

    pagination: Optional[PaginationMemory] = None
    bulk_selection: list[str] = field(default_factory=list)

    # ------------------------------------------------------- serialization
    def to_json(self) -> str:
        return json.dumps(
            {
                "pagination": asdict(self.pagination) if self.pagination else None,
                "bulk_selection": self.bulk_selection,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "SessionMemory":
        data = json.loads(raw)
        pagination_data = data.get("pagination")
        pagination = PaginationMemory(**pagination_data) if pagination_data else None
        return cls(pagination=pagination, bulk_selection=list(data.get("bulk_selection", [])))

    # ----------------------------------------------------------- Redis I/O
    @classmethod
    async def load(cls, r: redis.Redis, session_id: str) -> "SessionMemory":
        """Never returns None -- an unknown/expired session id just
        yields a fresh, empty SessionMemory(), so callers never need a
        None-check before reading .bulk_selection/.pagination."""
        raw = await r.get(_key(session_id))
        if raw is None:
            return cls()
        return cls.from_json(raw)

    async def save(self, r: redis.Redis, session_id: str) -> None:
        await r.set(_key(session_id), self.to_json(), ex=SESSION_TTL_SECONDS)

    @staticmethod
    async def delete(r: redis.Redis, session_id: str) -> None:
        await r.delete(_key(session_id))

    # ---------------------------------------------------- mutation helpers
    def select(self, request_id: str) -> None:
        if request_id not in self.bulk_selection:
            self.bulk_selection.append(request_id)

    def deselect(self, request_id: str) -> None:
        if request_id in self.bulk_selection:
            self.bulk_selection.remove(request_id)

    def clear_selection(self) -> None:
        self.bulk_selection = []

    def set_pagination(self, query_id: str, filter_: dict[str, Optional[str]], total: int) -> None:
        self.pagination = PaginationMemory(query_id=query_id, filter=filter_, total=total, cached_at=time.time())
