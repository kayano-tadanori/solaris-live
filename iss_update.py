"""公開版のライブデータ（2026-10-07）：ISS の軌道データ。ISS-B の update_iss_public.py の取得・確かめを使い、SGP4 で本当に位置が出るかも確かめる。

  from iss_update import fetch_iss, validate_iss

・取得：CelesTrak GP（NORAD 25544・OMM と TLE を 1 回ずつ＝1 日 8 回まで）。中身の形はアプリの live/iss/iss.json と同じ（tools/live/fetch_iss.py と同じ鍵・NASA の軌道暦は null）
・確かめる（どれか 1 つでもだめなら公開用を新しくしない）：
  NORAD 25544／TLE のチェックサム／OMM と TLE の元期が同じ／元期が未来でない／7 日より古くない（update_iss_public.build）
  アプリが読む鍵がそろっている／SGP4（python-sgp4）で元期・+1 時間・+1 日・+3 日の位置が出る／高さ 300〜500 km・速さ 7.4〜7.9 km/s
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from update_iss_public import OMM_URL, TLE_URL, build, get  # ISS-B で作った取得と確かめ（同じものを使う）

REQUIRED = ("kind", "noradId", "epochUtc", "fetchedUtc", "warnAfterUtc", "stopAfterUtc", "predictUntilUtc", "tle", "omm", "source")
OMM_KEYS = ("EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR", "NORAD_CAT_ID")
R_EARTH = 6378.137


def fetch_iss(now: datetime | None = None) -> dict:
    """CelesTrak から取り、アプリの形にする（確かめに通らなければ例外）"""
    fetched = now or datetime.now(timezone.utc)
    return build(get(OMM_URL), get(TLE_URL), fetched)


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def validate_iss(app: dict, now: datetime | None = None) -> list[str]:
    """公開してよいか（だめな理由の一覧。空なら OK）"""
    bad: list[str] = []
    for k in REQUIRED:
        if k not in app:
            bad.append(f"鍵が無い：{k}")
    if bad:
        return bad
    if app["kind"] != "iss-gp" or int(app["noradId"]) != 25544:
        bad.append("kind／NORAD 番号がちがう")
    for k in OMM_KEYS:
        if k not in app["omm"]:
            bad.append(f"OMM の鍵が無い：{k}")
    now = now or datetime.now(timezone.utc)
    ep = _parse(app["epochUtc"])
    if ep > now + timedelta(hours=1):
        bad.append(f"元期が未来：{app['epochUtc']}")
    if now - ep > timedelta(days=7):
        bad.append(f"元期が 7 日より古い：{app['epochUtc']}")
    if not (_parse(app["warnAfterUtc"]) > ep and _parse(app["stopAfterUtc"]) > _parse(app["warnAfterUtc"])):
        bad.append("注意・停止の時刻の順がおかしい")
    try:
        from sgp4.api import Satrec, jday
        l1, l2 = app["tle"]
        sat = Satrec.twoline2rv(l1, l2)
        for dt in (timedelta(0), timedelta(hours=1), timedelta(days=1), timedelta(days=3)):
            t = ep + dt
            jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
            e, r, v = sat.sgp4(jd, fr)
            if e != 0:
                bad.append(f"SGP4 が計算できない（{dt}・エラー {e}）"); break
            alt = math.sqrt(sum(x * x for x in r)) - R_EARTH
            spd = math.sqrt(sum(x * x for x in v))
            if not (300 <= alt <= 500 and 7.4 <= spd <= 7.9):
                bad.append(f"位置がおかしい（{dt}・高さ {alt:.0f} km・速さ {spd:.2f} km/s）"); break
    except Exception as ex:  # noqa: BLE001
        bad.append(f"SGP4 で読めない：{ex}")
    return bad


def dumps(app: dict) -> str:
    return json.dumps(app, ensure_ascii=False, indent=1)
