"""公開版（GitHub Pages）の ISS の軌道データを作る（2026-10-06 ISS-B で準備・まだ本番では動かしていない）。
GitHub Actions（iss_public.yml）から 6 時間ごとに動かす想定。Python の標準ライブラリだけで動く。

  python update_iss_public.py <公開サイトの根> [--force]

・CelesTrak GP（NORAD 25544・OMM の JSON と TLE）を取り、アプリの live/iss/iss.json と同じ形で書く（tools/live/fetch_iss.py の build_app_data と同じ中身。
  ただし NASA の軌道暦〔予定の噴射〕は入れない＝nasaOem: null）
・確かめる：NORAD 番号・TLE のチェックサム・OMM と TLE の元期が同じ・元期が未来でない・元期が 7 日より古くない（古ければ書かない＝アプリは前のデータの古さを表示する）
・元期が前と同じなら書かない（コミットが増えない）
・取りに行く回数は 1 回の実行で 2 回（CelesTrak の決まり：同じデータを短い間に何度も取らない。6 時間ごとなら 1 日 8 回）
終わりのコード：0＝書いた・3＝変わらない（書かない）・1＝失敗（書かない）
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OMM_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=json"
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle"
UA = "solaris-iss-public/1.0 (educational app on GitHub Pages; about 8 requests per day)"
WARN_DAYS, STOP_DAYS, PREDICT_DAYS = 3, 7, 5


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds").replace("+00:00", "Z")


RETRY_WAITS = (20, 60)  # 時間切れ・接続できない・5xx のときだけ、待ってから 2 回までやり直す（2026-10-11：CelesTrak の時間切れで 1 回失敗）


def get(url: str) -> bytes:
    """取る。時間切れ・つながらない・サーバーの一時的な失敗（5xx）だけやり直す。403・404 などはやり直さない（CelesTrak は短時間の連続取得を嫌う）"""
    import random
    import time
    import urllib.error
    last: Exception | None = None
    for i, wait in enumerate((0, *RETRY_WAITS)):
        if wait:
            time.sleep(wait + random.uniform(0, 5))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status != 200:
                    raise RuntimeError(f"{url}: HTTP {r.status}")
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        print(f"取得をやり直す（{i + 1} 回目の失敗：{last}）", flush=True)
    raise last if last else RuntimeError(url)


def tle_checksum_ok(line: str) -> bool:
    s = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68])
    return s % 10 == int(line[68])


def tle_epoch(l1: str) -> datetime:
    yy, doy = int(l1[18:20]), float(l1[20:32])
    y = 2000 + yy if yy < 57 else 1900 + yy
    return datetime(y, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)


def build(omm_body: bytes, tle_body: bytes, fetched: datetime) -> dict:
    omm = json.loads(omm_body.decode("utf-8"))[0]
    tle = [x.rstrip() for x in tle_body.decode("utf-8").splitlines() if x.strip()]
    l1 = next(x for x in tle if x.startswith("1 ")); l2 = next(x for x in tle if x.startswith("2 "))
    if int(omm["NORAD_CAT_ID"]) != 25544 or l1[2:7].strip() != "25544" or l2[2:7].strip() != "25544":
        raise RuntimeError("NORAD 番号が 25544 でない")
    if not (tle_checksum_ok(l1) and tle_checksum_ok(l2)):
        raise RuntimeError("TLE のチェックサムが合わない")
    ep = datetime.fromisoformat(omm["EPOCH"] + "+00:00")
    if abs((tle_epoch(l1) - ep).total_seconds()) > 1:
        raise RuntimeError(f"OMM と TLE の元期がちがう（{ep} と {tle_epoch(l1)}）")
    if ep > fetched + timedelta(hours=1):
        raise RuntimeError(f"元期が未来（{ep}）")
    if fetched - ep > timedelta(days=STOP_DAYS):
        raise RuntimeError(f"元期が {STOP_DAYS} 日より古い（{ep}）")
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    stamp = fetched.strftime("%Y%m%dT%H%M%SZ")
    return {
        "kind": "iss-gp", "noradId": 25544, "name": omm.get("OBJECT_NAME", "ISS (ZARYA)"),
        "epochUtc": iso(ep), "fetchedUtc": iso(fetched),
        "source": {"omm": "CelesTrak GP（OMM・JSON）NORAD 25544。元データは米宇宙軍 Space-Track", "tle": "CelesTrak GP（TLE）NORAD 25544",
                   "ommSha256": sha(omm_body), "tleSha256": sha(tle_body), "ommFile": f"celestrak_omm_{stamp}.json", "tleFile": f"celestrak_tle_{stamp}.txt"},
        "warnAfterUtc": iso(ep + timedelta(days=WARN_DAYS)), "stopAfterUtc": iso(ep + timedelta(days=STOP_DAYS)), "predictUntilUtc": iso(ep + timedelta(days=PREDICT_DAYS)),
        "tle": [l1, l2], "omm": omm, "nasaOem": None,
        "generatedUtc": iso(datetime.now(timezone.utc)),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__); return 1
    out = Path(sys.argv[1]) / "live" / "iss" / "iss.json"
    try:
        fetched = datetime.now(timezone.utc)
        app = build(get(OMM_URL), get(TLE_URL), fetched)
    except Exception as e:  # noqa: BLE001
        print(f"失敗：{e}（書かない＝アプリは前のデータの古さを表示する）")
        return 1
    if out.exists() and "--force" not in sys.argv:
        old = json.loads(out.read_text(encoding="utf-8"))
        if old.get("epochUtc") == app["epochUtc"]:
            print(f"変わらない（元期 {app['epochUtc']}）")
            return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(app, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"書いた：{out}（元期 {app['epochUtc']}・注意 {app['warnAfterUtc']}・停止 {app['stopAfterUtc']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
