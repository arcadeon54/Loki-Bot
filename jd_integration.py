"""JDownloader integration via MyJDownloader API for Loki."""
import os
import asyncio
import logging
import time

log = logging.getLogger(__name__)

MYJD_EMAIL   = os.getenv("MYJD_EMAIL", "")
MYJD_PASS    = os.getenv("MYJD_PASSWORD", "")
MYJD_DEVICE  = os.getenv("MYJD_DEVICE", "")
JD_HOST_ROOT = "/home/g2k247/downloads/jdownloader"
JD_CTR_ROOT  = "/output"


def _get_device():
    try:
        import myjdapi
        jd = myjdapi.Myjdapi()
        jd.set_app_key("Loki")
        jd.connect(MYJD_EMAIL, MYJD_PASS)
        jd.update_devices()
        return jd, jd.get_device(MYJD_DEVICE)
    except Exception as e:
        log.error(f"JD connect: {e}")
        return None, None


def _queue_sync(url: str, package_name: str, dest_ctr: str) -> bool:
    _, device = _get_device()
    if not device:
        return False
    try:
        device.linkgrabber.add_links([{
            "autostart": True,
            "links": url,
            "packageName": f"Loki-{package_name}",
            "destinationFolder": dest_ctr,
            "overwritePackagizerRules": True,
        }])
        log.info(f"JD: queued {url} → {dest_ctr}")
        return True
    except Exception as e:
        log.error(f"JD add_links: {e}")
        return False


def _is_done_sync(dest_host: str) -> bool:
    _, device = _get_device()
    if not device:
        return True
    try:
        packages = device.downloads.query_packages([{
            "saveTo": True, "status": True, "finished": True
        }])
        for pkg in packages:
            save_to = pkg.get("saveTo", "").replace(JD_CTR_ROOT, JD_HOST_ROOT, 1).rstrip("/")
            if save_to == dest_host.rstrip("/"):
                if not pkg.get("finished", False):
                    return False
        return True
    except Exception as e:
        log.error(f"JD check_done: {e}")
        return True


async def queue_url(url: str, requester: str, date_str: str) -> tuple:
    """Add URL to JDownloader LinkGrabber with autostart. Returns (success, host_dest_path)."""
    dest_ctr = f"{JD_CTR_ROOT}/{requester}/{date_str}"
    dest_host = os.path.join(JD_HOST_ROOT, requester, date_str)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _queue_sync, url, requester, dest_ctr)
    return ok, dest_host


async def wait_for_completion(
    dest_host: str,
    timeout_sec: int = 3600,
    poll_sec: int = 20,
) -> list:
    """
    Poll until JD finishes downloading to dest_host.
    Returns list of completed file paths, empty list on timeout.
    """
    deadline = time.monotonic() + timeout_sec
    loop = asyncio.get_event_loop()
    await asyncio.sleep(15)  # give JD time to register the package
    seen_done_with_no_files = False
    while time.monotonic() < deadline:
        if os.path.isdir(dest_host):
            files = [
                os.path.join(dest_host, f)
                for f in os.listdir(dest_host)
                if not f.endswith((".part", ".tmp", ".crdownload"))
                and os.path.isfile(os.path.join(dest_host, f))
            ]
            done = await loop.run_in_executor(None, _is_done_sync, dest_host)
            if done and files:
                log.info(f"JD: {len(files)} file(s) complete in {dest_host}")
                return files
            if done and not files:
                if seen_done_with_no_files:
                    # Package is done but saved nothing — site likely unsupported
                    log.warning(f"JD: package finished but no files saved in {dest_host}")
                    return []
                seen_done_with_no_files = True
        await asyncio.sleep(poll_sec)
    log.warning(f"JD: timed out waiting for {dest_host}")
    return []
