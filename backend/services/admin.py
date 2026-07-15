"""
admin.py — The admin role, and the dependency that enforces it.

How someone becomes an admin
----------------------------
By being listed in `ADMIN_EMAILS` (see config.Settings.admin_emails) with a
*verified* address. That is the whole mechanism. There is deliberately no role
column, no grant table, and no promote-user endpoint:

  * privilege can't be escalated by writing to the database, because the grant
    isn't in the database;
  * there is no privileged-write endpoint to find a bug in, because there is no
    endpoint;
  * a stolen admin session can act as that admin until it expires, but cannot
    mint another one.

The cost is that changing the admin list needs a config change and a restart.
For the one role that can read every account on the instance, that is a feature.

Why the email must be verified is argued in services.auth.is_admin — briefly, an
unverified address proves nothing about who controls it.
"""

import logging

from fastapi import Depends, HTTPException

from config import settings
from services.auth import require_user, is_admin

logger = logging.getLogger(__name__)


async def require_admin(ctx: dict = Depends(require_user)) -> dict:
    """FastAPI dependency: authenticate, then demand the admin role.

    Chains off require_user, so an admin route gets the session checks (valid
    token, live account, not suspended) before this adds the role check on top.

    Returns the auth context unchanged. Put it on every /api/admin route — the
    client's `is_admin` flag is a UI hint and is never trusted here.
    """
    user = ctx["user"]
    if not is_admin(user):
        # 404, not 403. A 403 would confirm that /api/admin/users exists and
        # that this account simply lacks the role, which tells an attacker who
        # has already stolen a session exactly what to go after next. To a
        # non-admin the console should look like it isn't there at all.
        #
        # Logged at warning: a signed-in user probing admin routes is worth
        # seeing, even though it's usually just a stale tab.
        logger.warning(
            f"Non-admin {user['id']} attempted admin access "
            f"(email={user.get('email')!r}, admin_configured={settings.admin_enabled})"
        )
        raise HTTPException(404, "Not found.")
    return ctx
