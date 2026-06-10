"""Nextcloud WebDAV + OCS Share API integration for Loki."""
import os
import asyncio
import logging
import requests

log = logging.getLogger(__name__)

NC_URL  = os.getenv("NEXTCLOUD_URL", "http://192.168.1.247:8082")
NC_USER = os.getenv("NEXTCLOUD_USER", "admin")
NC_PASS = os.getenv("NEXTCLOUD_PASS", "")
NC_BASE = "Loki Downloads"


def _dav(path: str) -> str:
    return f"{NC_URL}/remote.php/dav/files/{NC_USER}/{path.lstrip('/')}"


def _auth():
    return (NC_USER, NC_PASS)


def _ocs_headers() -> dict:
    return {"OCS-APIRequest": "true", "Content-Type": "application/x-www-form-urlencoded"}


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


def _create_share_sync(nc_path: str) -> str | None:
    url = f"{NC_URL}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    try:
        r = requests.post(
            url,
            data={"path": f"/{nc_path.lstrip('/')}", "shareType": 3},
            params={"format": "json"},
            headers=_ocs_headers(),
            auth=_auth(),
            timeout=15,
        )
        if r.status_code == 200:
            token = r.json()["ocs"]["data"]["token"]
            return f"{NC_URL}/s/{token}"
        log.error(f"Share {nc_path} → {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        log.error(f"Share error: {e}")
        return None


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


async def create_share(nc_path: str) -> str | None:
    return await _run(_create_share_sync, nc_path)


async def delete_nc_path(nc_path: str) -> bool:
    return await _run(_delete_sync, nc_path)


async def upload_and_share(
    local_paths: list,
    requester: str,
    date_str: str,
) -> tuple:
    """
    Upload files to NC_BASE/{requester}/{date_str}/, create a public share,
    delete local copies on success.
    Returns (share_url, nc_folder_path) or (None, None) on failure.
    """
    folder = f"{NC_BASE}/{requester}/{date_str}"
    if not await ensure_folder(folder):
        log.error(f"Could not create NC folder: {folder}")
        return None, None

    uploaded = []
    for local_path in local_paths:
        filename = os.path.basename(local_path)
        nc_path = f"{folder}/{filename}"
        if await upload_file(local_path, nc_path):
            uploaded.append((local_path, nc_path))
        else:
            log.warning(f"Failed to upload {local_path}")

    if not uploaded:
        return None, None

    # Share the folder for multiple files, single file path for one
    share_target = uploaded[0][1] if len(uploaded) == 1 else folder
    share_url = await create_share(share_target)

    if share_url:
        for local_path, _ in uploaded:
            try:
                os.remove(local_path)
            except Exception as e:
                log.warning(f"Could not delete local {local_path}: {e}")

    return share_url, folder
