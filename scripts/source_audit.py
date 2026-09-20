#!/usr/bin/env python3
import json
import re
import sys
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UA = "Tsuyu-source-audit/1.0 (GitHub Actions)"


def fetch(url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if headers:
        h.update(headers)
    req = Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    started = time.time()
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, r.headers, body, time.time() - started
    except HTTPError as e:
        body = e.read(2048)
        return e.code, e.headers, body, time.time() - started
    except Exception as e:
        return None, {}, str(e).encode(), time.time() - started


def json_fetch(url, data=None, headers=None, timeout=20):
    status, h, body, elapsed = fetch(url, data, headers, timeout)
    try:
        return status, json.loads(body.decode("utf-8", "replace")), elapsed, ""
    except Exception as e:
        return status, None, elapsed, body.decode("utf-8", "replace")[:500]


def shiki_search(title):
    query = "{animes(search:%s,limit:6,order:ranked,censored:false){id name russian english airedOn{year}}}" % json.dumps(title, ensure_ascii=False)
    payload = json.dumps({"query": query}, ensure_ascii=False).encode()
    for host in ("https://shikimori.io/api/graphql", "https://shikimori.one/api/graphql", "https://shikimori.me/api/graphql"):
        status, root, elapsed, err = json_fetch(host, payload, {"Content-Type": "application/json"}, 20)
        if status == 200 and root and root.get("data"):
            return root["data"].get("animes") or [], host, elapsed, ""
        last = "%s status=%s err=%s" % (host, status, err[:180])
    return [], "", 0, last


def yummy_for_mal(mal):
    url = "https://api.yani.tv/anime?" + urlencode({"shikimori_ids": mal, "limit": 20})
    return json_fetch(url, timeout=20)


def yummy_by_title(title):
    url = "https://api.yani.tv/anime?" + urlencode({"q": title, "limit": 20})
    return json_fetch(url, timeout=20)


def summarize_yummy(row):
    if not isinstance(row, dict):
        return {}
    ids = row.get("remote_ids") or {}
    return {"anime_id": row.get("anime_id"), "title": row.get("title"), "year": row.get("year"),
            "mal": ids.get("myanimelist_id"), "shiki": ids.get("shikimori_id"),
            "episodes": row.get("episodes", row.get("ep_count"))}


def audit_yummy(row):
    if not isinstance(row, dict):
        return {"error": "no row"}
    anime_id = row.get("anime_id")
    if not anime_id:
        return {"error": "no anime_id", "row": summarize_yummy(row)}
    status, detail, elapsed, err = json_fetch("https://api.yani.tv/anime/" + quote(str(anime_id), safe=""), timeout=20)
    detail_row = detail.get("response") if isinstance(detail, dict) else None
    status2, videos, elapsed2, err2 = json_fetch("https://api.yani.tv/anime/%s/videos" % quote(str(anime_id), safe=""), timeout=25)
    rows = videos.get("response") if isinstance(videos, dict) else None
    if not isinstance(rows, list):
        rows = []
    voices = {}
    urls = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        voice = data.get("dubbing", "")
        player = data.get("player", "")
        key = (str(voice).strip(), str(player).strip())
        voices[key] = voices.get(key, 0) + 1
        if item.get("iframe_url"):
            urls.append(item["iframe_url"])
    first_url = urls[0] if urls else ""
    stream_status = None
    stream_len = None
    stream_type = ""
    if first_url:
        st, sh, body, et = fetch(first_url, headers={"Referer": "https://yani.tv/"}, timeout=20)
        stream_status, stream_len, stream_type = st, len(body), sh.get_content_type() if sh else ""
    return {
        "detail_status": status, "detail_ms": round(elapsed * 1000), "detail_has_response": isinstance(detail_row, dict),
        "videos_status": status2, "videos_ms": round(elapsed2 * 1000), "video_rows": len(rows),
        "voices": [{"name": k[0], "player": k[1], "episodes": n} for k, n in sorted(voices.items())],
        "first_iframe": first_url[:300], "first_iframe_status": stream_status, "first_iframe_bytes": stream_len,
        "first_iframe_type": stream_type, "summary": summarize_yummy(row), "video_error": err2[:180] if not rows else ""
    }


def main():
    titles = [
        "О моём перерождении в слизь 4",
        "One Piece",
        "Solo Leveling Season 2",
        "Frieren: Beyond Journey's End",
    ]
    all_ok = True
    for title in titles:
        print("\n=== %s ===" % title, flush=True)
        rows, host, ms, err = shiki_search(title)
        print("SHIKI", {"host": host, "ms": round(ms * 1000), "rows": rows[:3], "error": err[:180]}, flush=True)
        candidate = rows[0] if rows else None
        if not candidate:
            st, root, et, ee = yummy_by_title(title)
            candidates = root.get("response") if isinstance(root, dict) else []
            candidate = candidates[0] if candidates else None
            print("YUMMY_TITLE_FALLBACK", {"status": st, "ms": round(et * 1000), "rows": [summarize_yummy(x) for x in candidates[:3]], "error": ee[:180]}, flush=True)
        if not candidate:
            print("RESULT unavailable: no catalog identity", flush=True)
            all_ok = False
            continue
        # Prefer the Shikimori id because this is the exact lookup used by ApiRepository.findYummy.
        mal = candidate.get("id") if isinstance(candidate, dict) else None
        st, root, et, ee = yummy_for_mal(mal) if mal else (None, None, 0, "no mal")
        yrows = root.get("response") if isinstance(root, dict) else []
        if not isinstance(yrows, list): yrows = []
        print("YUMMY_MAL", {"mal": mal, "status": st, "ms": round(et * 1000), "rows": [summarize_yummy(x) for x in yrows[:5]], "error": ee[:180]}, flush=True)
        yrow = yrows[0] if yrows else (candidate if candidate.get("anime_id") else None)
        result = audit_yummy(yrow) if yrow else {"error": "no Yummy match"}
        print("YUMMY_PLAYBACK", json.dumps(result, ensure_ascii=False), flush=True)
        good = result.get("videos_status") == 200 and result.get("video_rows", 0) > 0 and result.get("first_iframe_status") in (200, 206)
        print("RESULT", "PASS" if good else "FAIL", flush=True)
        all_ok = all_ok and good
    if not all_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
