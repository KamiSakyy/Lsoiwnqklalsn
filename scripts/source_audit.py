#!/usr/bin/env python3
import base64
import json
import re
import sys
import time
from urllib.parse import urlencode, quote, urljoin, urlparse
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
    if first_url.startswith("//"):
        first_url = "https:" + first_url
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


def _java_caesar(value):
    out = []
    for ch in value:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - ord("a") + 18) % 26 + ord("a")))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - ord("A") + 18) % 26 + ord("A")))
        else:
            out.append(ch)
    return "".join(out)


def _kodik_decode(value):
    value = str(value or "")
    if "//" in value or value.startswith("http"):
        return value.replace("\\/", "/")
    shifted = _java_caesar(value)
    shifted += "=" * ((4 - len(shifted) % 4) % 4)
    try:
        return base64.b64decode(shifted).decode("utf-8", "replace").replace("\\/", "/")
    except Exception:
        return ""


def _cookies(headers):
    vals = headers.get_all("Set-Cookie") if headers else []
    return "; ".join(v.split(";", 1)[0] for v in (vals or []))


def resolve_kodik(url):
    result = {"kind": "kodik", "url": url}
    st, headers, body, elapsed = fetch(url, headers={"Referer": "https://yani.tv/"}, timeout=20)
    html = body.decode("utf-8", "replace").replace("\n", "").replace("\r", "")
    result.update({"page_status": st, "page_ms": round(elapsed * 1000), "page_bytes": len(body)})
    params_text = ""
    for pattern in (r"\burlParams\s*=\s*'([^']+)'", r'\burlParams\s*=\s*"([^"]+)"'):
        m = re.search(pattern, html, re.I | re.S)
        if m:
            params_text = m.group(1)
            break
    payload = {}
    if params_text:
        try:
            payload.update(json.loads(params_text.replace("&quot;", '"').replace("\\/", "/")))
        except Exception as e:
            result["params_error"] = str(e)
    if not payload:
        patterns = {
            "d": r'var\s+domain\s*=\s*["\'](.+?)["\']',
            "d_sign": r'var\s+d_sign\s*=\s*["\'](.+?)["\']',
            "pd": r'var\s+pd\s*=\s*["\'](.+?)["\']',
            "pd_sign": r'var\s+pd_sign\s*=\s*["\'](.+?)["\']',
            "ref": r'var\s+ref\s*=\s*["\'](.+?)["\']',
            "ref_sign": r'var\s+ref_sign\s*=\s*["\'](.+?)["\']',
        }
        for key, pattern in patterns.items():
            m = re.search(pattern, html, re.I | re.S)
            payload[key] = m.group(1) if m else ""
    def find(pattern):
        m = re.search(pattern, html, re.I | re.S)
        return m.group(1) if m else ""
    payload["type"] = find(r'(?:videoInfo|vInfo)\.type\s*\+?=\s*["\'](.+?)["\']') or find(r'["\']type["\']\s*:\s*["\'](.+?)["\']')
    payload["hash"] = find(r'(?:videoInfo|vInfo)\.hash\s*\+?=\s*["\'](.+?)["\']') or find(r'["\']hash["\']\s*:\s*["\'](.+?)["\']')
    payload["id"] = find(r'(?:videoInfo|vInfo)\.id\s*\+?=\s*["\'](.+?)["\']') or find(r'["\']id["\']\s*:\s*["\'](.+?)["\']') or find(r'var\s+videoId\s*=\s*["\'](\d+)["\']')
    result["payload_keys"] = {k: bool(str(v)) for k, v in payload.items()}
    parsed = urlparse(url)
    endpoint = parsed.scheme + "://" + parsed.netloc + "/ftor"
    sm = re.search(r'''src=["']((?://[^"']+)?/assets/js/app\.player_single[^"']+)["']''', html, re.I)
    if not sm:
        sm = re.search(r'''src=["']([^"']*app\.player_single[^"']+)["']''', html, re.I)
    if sm:
        script_url = urljoin(url, sm.group(1))
        ss, sh, sb, se = fetch(script_url, headers={"Referer": url}, timeout=20)
        decoded_paths = []
        script = sb.decode("utf-8", "replace")
        for b64 in re.findall(r'atob\("([A-Za-z0-9+/=]+)"\)', script):
            try:
                decoded = base64.b64decode(b64).decode("utf-8", "replace")
                if decoded.startswith("/") and not decoded.startswith("//") and len(decoded) <= 16:
                    decoded_paths.append(decoded)
            except Exception:
                pass
        if decoded_paths:
            endpoint = urlparse(script_url).scheme + "://" + urlparse(script_url).netloc + decoded_paths[0]
        result["script"] = {"status": ss, "bytes": len(sb), "endpoint_path": decoded_paths[:2]}
    form = {key: str(payload.get(key, "")) for key in ("d", "d_sign", "pd", "pd_sign", "ref", "ref_sign", "type", "hash", "id")}
    form.update({"bad_user": "true", "cdn_is_working": "true", "info": "{}"})
    post_headers = {"Referer": url, "Origin": parsed.scheme + "://" + parsed.netloc,
                    "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36",
                    "Cookie": _cookies(headers)}
    def java_form(rows):
        parts = []
        for key, value in rows.items():
            encoded_key = quote(str(key), safe="")
            raw = key == "ref" and bool(re.search(r"%[0-9A-Fa-f]{2}", str(value)))
            parts.append(encoded_key + "=" + (str(value) if raw else quote(str(value), safe="")))
        return "&".join(parts).encode()
    post_data = java_form(form)
    ps, ph, pb, pe = fetch(endpoint, post_data, post_headers, 20)
    root = None
    try:
        root = json.loads(pb.decode("utf-8", "replace"))
    except Exception as e:
        result["post_json_error"] = str(e)
        result["post_body"] = pb.decode("utf-8", "replace")[:400]
        # Match VideoResolver.postJson fallback after the form endpoint rejects the request.
        json_headers = dict(post_headers)
        json_headers["Content-Type"] = "application/json"
        json_body = json.dumps({"videoId": payload.get("id", ""), **{k: payload.get(k, "") for k in ("d", "d_sign", "pd", "pd_sign", "ref", "ref_sign", "type", "hash", "id")}}, ensure_ascii=False).encode()
        js, jh, jb, je = fetch(endpoint, json_body, json_headers, 20)
        result["json_fallback"] = {"status": js, "ms": round(je * 1000), "bytes": len(jb), "body": jb.decode("utf-8", "replace")[:300]}
        if js and 200 <= js < 300:
            try: root = json.loads(jb.decode("utf-8", "replace"))
            except Exception: root = None
    links = root.get("links") if isinstance(root, dict) else None
    resolved = {}
    if isinstance(links, dict):
        for key, rows in links.items():
            if not isinstance(rows, list) or not rows:
                continue
            row = rows[0] if isinstance(rows[0], dict) else {}
            decoded = _kodik_decode(row.get("src", ""))
            if decoded:
                resolved[str(key)] = decoded
    result.update({"endpoint": endpoint, "post_status": ps, "post_ms": round(pe * 1000), "post_bytes": len(pb),
                   "link_keys": sorted(links.keys()) if isinstance(links, dict) else [],
                   "resolved": {k: v[:260] for k, v in resolved.items()}})
    if resolved:
        key, stream = next(iter(resolved.items()))
        ss, sh, sb, se = fetch(stream, headers={"Referer": url}, timeout=20)
        result["stream_probe"] = {"quality": key, "status": ss, "bytes": len(sb), "type": sh.get_content_type() if sh else ""}
    return result


def resolve_aksor(url):
    parsed = urlparse(url)
    origin = parsed.scheme + "://" + parsed.netloc
    ident = parsed.path.rstrip("/").split("/")[-1]
    api_url = origin + "/api/video/" + quote(ident, safe="")
    st, headers, body, elapsed = fetch(api_url, headers={"Referer": url, "Origin": origin}, timeout=20)
    try:
        root = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        root = None
    result = {"kind": "aksor", "url": url, "api": api_url, "status": st, "bytes": len(body), "ms": round(elapsed * 1000)}
    if isinstance(root, dict):
        qualities = root.get("qualities")
        sources = root.get("sources")
        result["quality_keys"] = sorted(qualities.keys()) if isinstance(qualities, dict) else []
        result["source_count"] = len(sources) if isinstance(sources, list) else 0
        urls = list(qualities.values()) if isinstance(qualities, dict) else []
        if not urls and isinstance(sources, list):
            urls = [r.get("url", r.get("src", "")) for r in sources if isinstance(r, dict)]
        if urls:
            stream = urls[0]
            ss, sh, sb, se = fetch(stream, headers={"Referer": url}, timeout=20)
            result["stream_probe"] = {"url": str(stream)[:260], "status": ss, "bytes": len(sb), "type": sh.get_content_type() if sh else ""}
    return result

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
        first_url = result.get("first_iframe", "")
        if first_url.startswith("https://kodikplayer.com/"):
            print("RESOLVER", json.dumps(resolve_kodik(first_url), ensure_ascii=False), flush=True)
        elif "aksor" in first_url:
            print("RESOLVER", json.dumps(resolve_aksor(first_url), ensure_ascii=False), flush=True)
        good = result.get("videos_status") == 200 and result.get("video_rows", 0) > 0 and result.get("first_iframe_status") in (200, 206)
        print("RESULT", "PASS" if good else "FAIL", flush=True)
        all_ok = all_ok and good
    if not all_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
