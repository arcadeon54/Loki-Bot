"""Nextcloud WebDAV + OCS Share API integration for Loki.

Two URLs, deliberately separate:

  NEXTCLOUD_URL             where Loki talks to Nextcloud (LAN, fast, no TLS
                            round trip). Never shown to a user.
  NEXTCLOUD_PUBLIC_BASE_URL what a recipient's browser must be able to reach.

They are not interchangeable, and conflating them is what broke this feature:
recipients were sent `http://192.168.1.63:8082/s/<token>`, which nobody outside
the LAN can open. Note that using the OCS response's own `url` field is NOT
sufficient on its own — this server generates `https://192.168.1.63:8082/s/...`
there too, because its `overwritehost`/`overwrite.cli.url` are unset behind the
reverse proxy. So the token is always taken from the API (never invented) and
only its ORIGIN is re-based onto the public base.

If Nextcloud's own config is corrected later, the returned URL will already be
public and re-basing becomes a no-op — this stays correct either way.

A share URL is only ever returned when Nextcloud actually created the share.
There is no constructed-guess fallback, and `_assert_public` refuses to hand
back a link pointing at a private address.
"""
import asyncio
import datetime
import ipaddress
import logging
import os
import secrets
import urllib.parse

import requests

log = logging.getLogger(__name__)

NC_URL = os.getenv("NEXTCLOUD_URL", "http://192.168.1.247:8082")
NC_USER = os.getenv("NEXTCLOUD_USER", "admin")
NC_PASS = os.getenv("NEXTCLOUD_PASS", "")
NC_BASE = "Loki Downloads"

# Public origin for recipient-facing links. Falls back to NC_URL so a fresh
# deployment still works on a LAN, but _assert_public then refuses to emit a
# private-address link rather than handing out one that cannot be opened.
NC_PUBLIC_BASE = (os.getenv("NEXTCLOUD_PUBLIC_BASE_URL") or NC_URL).rstrip("/")

# Public links expire after this many days. 0 disables expiry entirely.
try:
    SHARE_EXPIRY_DAYS = int(os.getenv("NEXTCLOUD_SHARE_EXPIRY_DAYS", "3"))
except ValueError:
    log.warning("NEXTCLOUD_SHARE_EXPIRY_DAYS is not an integer — using 3")
    SHARE_EXPIRY_DAYS = 3

# "Keep" historically meant the link stayed usable indefinitely. That behaviour
# is preserved by default: keeping a file clears the expiry the share was
# created with. Set false to leave the original expiry in place.
KEEP_CLEARS_EXPIRY = (
    os.getenv("NEXTCLOUD_KEEP_CLEARS_EXPIRY", "true").strip().lower()
    in ("1", "true", "yes", "on")
)

SHARE_TYPE_PUBLIC_LINK = 3
PERM_READ_ONLY = 1

_OCS_SHARES = "/ocs/v2.php/apps/files_sharing/api/v1/shares"


def _dav(path: str) -> str:
    return f"{NC_URL}/remote.php/dav/files/{NC_USER}/{path.lstrip('/')}"


def _auth():
    return (NC_USER, NC_PASS)


def _ocs_headers() -> dict:
    return {"OCS-APIRequest": "true",
            "Content-Type": "application/x-www-form-urlencoded"}


def _is_private_host(host: str) -> bool:
    """True for anything a stranger on the internet cannot reach."""
    if not host:
        return True
    host = host.split("@")[-1]
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        pass  # a hostname, not a literal IP
    return host in ("localhost",) or host.endswith((".local", ".internal", ".lan"))


def _assert_public(url: str) -> str | None:
    """Refuse to hand a recipient a link they provably cannot open.

    Returning None here is correct: the caller reports an upload failure rather
    than sending a dead address that looks like success.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    if _is_private_host(host):
        log.error(
            "Refusing to emit share link on private host %r — set "
            "NEXTCLOUD_PUBLIC_BASE_URL to the externally reachable origin", host)
        return None
    return url


def _public_share_url(share: dict) -> str | None:
    """Re-base the share Nextcloud created onto the public origin.

    The token always comes from the API response — it is never constructed.
    Only the scheme/host/port are replaced, so a server whose overwritehost is
    misconfigured still yields a usable link.
    """
    token = share.get("token")
    raw = share.get("url") or ""
    path = urllib.parse.urlsplit(raw).path if raw else ""
    if not path:
        if not token:
            log.error("Share response carried neither url nor token")
            return None
        path = f"/s/{token}"
    return _assert_public(f"{NC_PUBLIC_BASE}{path}")


def _expiry_date(days: int | None = None) -> str | None:
    days = SHARE_EXPIRY_DAYS if days is None else days
    if days <= 0:
        return None
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _ensure_folder_sync(path: str) -> bool:
    parts = path.strip("/").split("/")
    built = ""
    for part in parts:
        built = f"{built}/{part}" if built else part
        url = _dav(built)
        try:
            r = requests.request("MKCOL", url, auth=_auth(), timeout=10)
            if r.status_code not in (200, 201, 405):  # 405 = already exists
                log.error(f"MKCOL {url} → {r.status_code}")
                return False
        except Exception as e:
            log.error(f"MKCOL error: {e}")
            return False
    return True


def _upload_file_sync(local_path: str, nc_path: str) -> bool:
    url = _dav(nc_path)
    try:
        with open(local_path, "rb") as f:
            r = requests.put(url, data=f, auth=_auth(), timeout=300)
        if r.status_code in (200, 201, 204):
            return True
        log.error(f"Upload {nc_path} → {r.status_code}")
        return False
    except Exception as e:
        log.error(f"Upload error {local_path}: {e}")
        return False


def _create_share_sync(nc_path: str, expiry_days: int | None = None) -> dict | None:
    """Create a read-only public link for exactly one path.

    Returns the share record (id/token/url/expiration) or None. Never returns a
    URL that Nextcloud did not actually mint.
    """
    payload = {
        "path": f"/{nc_path.lstrip('/')}",
        "shareType": SHARE_TYPE_PUBLIC_LINK,
        # Read-only: a public link must never accept uploads or edits.
        "permissions": PERM_READ_ONLY,
        "publicUpload": "false",
    }
    expires = _expiry_date(expiry_days)
    if expires:
        payload["expireDate"] = expires
    try:
        r = requests.post(
            f"{NC_URL}{_OCS_SHARES}",
            data=payload,
            params={"format": "json"},
            headers=_ocs_headers(),
            auth=_auth(),
            timeout=15,
        )
        if r.status_code != 200:
            log.error(f"Share {nc_path} → HTTP {r.status_code}: {r.text[:200]}")
            return None
        body = r.json()["ocs"]
        if body["meta"].get("statuscode") not in (100, 200):
            log.error(f"Share {nc_path} → OCS {body['meta']}")
            return None
        data = body["data"]
        return {"id": str(data.get("id")), "token": data.get("token"),
                "url": data.get("url"), "expiration": data.get("expiration"),
                "path": nc_path}
    except Exception as e:
        log.error(f"Share error: {e}")
        return None


def _delete_share_sync(share_id: str) -> bool:
    """Revoke a public link. A share that is already gone counts as revoked."""
    try:
        r = requests.delete(
            f"{NC_URL}{_OCS_SHARES}/{share_id}",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
            auth=_auth(),
            timeout=15,
        )
        if r.status_code == 404:
            return True
        if r.status_code != 200:
            log.error(f"Unshare {share_id} → HTTP {r.status_code}")
            return False
        code = r.json()["ocs"]["meta"].get("statuscode")
        # 404 here means the share no longer exists, which is the goal.
        return code in (100, 200, 404)
    except Exception as e:
        log.error(f"Unshare error: {e}")
        return False


def _share_exists_sync(share_id: str) -> bool:
    try:
        r = requests.get(
            f"{NC_URL}{_OCS_SHARES}/{share_id}",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
            auth=_auth(),
            timeout=15,
        )
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            return False
        body = r.json()["ocs"]
        if body["meta"].get("statuscode") not in (100, 200):
            return False
        return bool(body.get("data"))
    except Exception as e:
        log.error(f"Share lookup error: {e}")
        return False


def _clear_expiry_sync(share_id: str) -> bool:
    try:
        r = requests.put(
            f"{NC_URL}{_OCS_SHARES}/{share_id}",
            data={"expireDate": ""},
            params={"format": "json"},
            headers=_ocs_headers(),
            auth=_auth(),
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Clear expiry error: {e}")
        return False


def _path_exists_sync(nc_path: str) -> bool:
    try:
        r = requests.request("PROPFIND", _dav(nc_path), auth=_auth(),
                             headers={"Depth": "0"}, timeout=15)
        return r.status_code in (200, 207)
    except Exception as e:
        log.error(f"Exists check error: {e}")
        return False


def _delete_sync(nc_path: str) -> bool:
    url = _dav(nc_path)
    try:
        r = requests.delete(url, auth=_auth(), timeout=15)
        return r.status_code in (200, 204, 404)
    except Exception as e:
        log.error(f"Delete error: {e}")
        return False


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


async def ensure_folder(path: str) -> bool:
    return await _run(_ensure_folder_sync, path)


async def upload_file(local_path: str, nc_path: str) -> bool:
    return await _run(_upload_file_sync, local_path, nc_path)


async def create_share(nc_path: str) -> dict | None:
    return await _run(_create_share_sync, nc_path)


async def delete_share(share_id: str) -> bool:
    return await _run(_delete_share_sync, share_id)


async def share_exists(share_id: str) -> bool:
    return await _run(_share_exists_sync, share_id)


async def clear_share_expiry(share_id: str) -> bool:
    return await _run(_clear_expiry_sync, share_id)


async def path_exists(nc_path: str) -> bool:
    return await _run(_path_exists_sync, nc_path)


async def delete_nc_path(nc_path: str) -> bool:
    return await _run(_delete_sync, nc_path)


async def upload_and_share(
    local_paths: list,
    requester: str,
    date_str: str,
) -> dict | None:
    """Upload files, publish ONE read-only public link, drop the local copies.

    Each call gets its own batch folder. Two downloads on the same day used to
    land in the same dated folder, so sharing that folder exposed every file
    the requester had fetched that day, and "delete" removed all of them. A
    per-batch folder makes both the share and the deletion cover exactly this
    request.

    Returns a record with the public url and everything needed to revoke it, or
    None if nothing could be uploaded or the share could not be created.
    """
    batch = secrets.token_hex(4)
    folder = f"{NC_BASE}/{requester}/{date_str}/{batch}"
    if not await ensure_folder(folder):
        log.error(f"Could not create NC folder: {folder}")
        return None

    uploaded = []
    for local_path in local_paths:
        filename = os.path.basename(local_path)
        nc_path = f"{folder}/{filename}"
        if await upload_file(local_path, nc_path):
            uploaded.append((local_path, nc_path))
        else:
            log.warning(f"Failed to upload {local_path}")

    if not uploaded:
        return None

    # One file → share the file itself, so nothing else is even listable.
    # Several → share this batch's own folder, which holds only these files.
    share_target = uploaded[0][1] if len(uploaded) == 1 else folder
    share = await create_share(share_target)
    if not share:
        log.error(f"Share creation failed for {share_target}")
        return None

    url = _public_share_url(share)
    if not url:
        # The share exists but cannot be presented safely. Revoke it rather
        # than leaving an unreferenced public link behind.
        await delete_share(share["id"])
        return None

    for local_path, _ in uploaded:
        try:
            os.remove(local_path)
        except Exception as e:
            log.warning(f"Could not delete local {local_path}: {e}")

    return {
        "url": url,
        "share_id": share["id"],
        "share_path": share_target,
        "folder": folder,
        "count": len(uploaded),
        "expiration": share.get("expiration"),
    }


async def keep_share(record: dict) -> bool:
    """Honour 'keep': the file stays, and by default so does the link."""
    if not KEEP_CLEARS_EXPIRY:
        return True
    share_id = record.get("share_id")
    if not share_id:
        return True
    ok = await clear_share_expiry(share_id)
    if not ok:
        log.warning(f"Could not clear expiry on share {share_id}")
    return ok


async def revoke_and_delete(record: dict) -> dict:
    """Honour 'delete': revoke the link, remove the files, prove both.

    Verification matters here — reporting "gone" while a public link still
    resolves would be the worst possible failure for this feature.
    """
    share_id = record.get("share_id")
    folder = record.get("folder")

    share_revoked = True
    if share_id:
        await delete_share(share_id)
        share_revoked = not await share_exists(share_id)

    files_deleted = True
    if folder:
        await delete_nc_path(folder)
        files_deleted = not await path_exists(folder)

    return {"share_revoked": share_revoked,
            "files_deleted": files_deleted,
            "ok": share_revoked and files_deleted}
