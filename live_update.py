"""公開版 SOLARIS のライブデータ（ISS の軌道・雲）を作る（2026-10-07）。GitHub Actions（live-data.yml）から毎時動かす。手元でも同じに動く。

  python live_update.py --state STATE --site SITE [--prev URL] [--clouds on|off] [--keep-hours 24] [--max-new 3]
                        [--iss-every-hours 6] [--force-iss] [--now 2026-10-07T12:50] [--offline-iss] [--offline-clouds]

考え方（データを Git に積まない）：
  ・前回の正常なデータは STATE（Actions のキャッシュ）に持つ。無ければ公開中のサイト（--prev）から取り直す。
  ・SITE に「公開する物だけ」を作る（Pages の配信物＝毎回まるごと入れかわる）。Git にはコミットしない
  ・順番：前回分を置く → 新しく作る（作れたら確かめて置きかえる）→ 全部を確かめる → 最後に manifest.json を書く
ISS：--iss-every-hours ごと（前回の取得からその時間がたったら）CelesTrak から取る。取れない・確かめに通らない → 前回のまま（status＝stale）
雲：--clouds on のときだけ（既定 off）。NOAA GMGSI LW から tools/live/fetch_clouds.py と同じ作り方でフレームを作る（原本は一時フォルダ＝実行の終わりに捨てる）。
    公開するのは直近 --keep-hours 時間のフレームだけ（古いものは置かない＝容量が増えない）。1 回に作るのは新しい方から --max-new 枚まで。
    取れない → 前回のフレームのまま（未来の雲は作らない＝観測の無い時刻のフレームは無い）
終わりのコード：0＝全部 OK・2＝どれかが前回のまま（取れなかった・確かめに通らなかった。SITE は前回分で作ってある＝配信してよい）・1＝SITE を作れない
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VERSION = 1
UA = "solaris-live-publisher/1.0 (educational app on GitHub Pages)"


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def log(msg: str) -> None:
    print(msg, flush=True)


# ── ISS ────────────────────────────────────────────────────────────────
def update_iss(a, state: Path, site: Path, prev_man: dict | None, now: datetime) -> dict:
    from iss_update import dumps, fetch_iss, validate_iss
    st_file = state / "iss" / "iss.json"
    # 前回分：STATE → 無ければ公開中のサイト
    if not st_file.exists() and a.prev and prev_man and prev_man.get("iss", {}).get("file"):
        try:
            body = get(a.prev.rstrip("/") + "/live/" + prev_man["iss"]["file"])
            if hashlib.sha256(body).hexdigest() == prev_man["iss"].get("sha256"):
                atomic_write(st_file, body); log("ISS：前回分を公開中のサイトから取った")
        except Exception as e:  # noqa: BLE001
            log(f"ISS：前回分を公開中のサイトから取れない（{e}）")
    prev = None
    if st_file.exists():
        try:
            prev = json.loads(st_file.read_text(encoding="utf-8"))
            broken = [x for x in validate_iss(prev, now) if "SGP4" in x or "鍵" in x or "kind" in x]
            if broken:
                log("ISS：前回分が壊れている → 使わずに取り直す（" + "・".join(broken) + "）"); prev = None
        except Exception as e:  # noqa: BLE001
            log(f"ISS：前回分が読めない → 使わずに取り直す（{e}）"); prev = None
    info = {"type": "iss-gp", "source": "CelesTrak GP（NORAD 25544）", "status": "missing", "error": None}
    due = a.force_iss or prev is None or (now - parse_iso(prev["fetchedUtc"])) >= timedelta(hours=a.iss_every_hours) - timedelta(minutes=20)
    failed = False
    if due:
        try:
            if a.offline_iss:
                raise RuntimeError("（検査）取れない")
            app = fetch_iss(now)
            bad = validate_iss(app, now)
            if bad:
                raise RuntimeError("確かめに通らない：" + "・".join(bad))
            if prev and prev.get("epochUtc") == app["epochUtc"]:
                log(f"ISS：元期は前と同じ（{app['epochUtc']}）→ 取得の時刻だけ新しくする")
            atomic_write(st_file, dumps(app).encode("utf-8"))
            prev = app
            log(f"ISS：新しくした（元期 {app['epochUtc']}）")
        except Exception as e:  # noqa: BLE001
            failed = True
            info["error"] = str(e)
            log(f"ISS：取れない／確かめに通らない → 前回のまま（{e}）")
    else:
        log(f"ISS：まだ取る時刻ではない（前回 {prev['fetchedUtc']}）→ 前回のまま")
    if prev is not None:
        if True:  # 前回分は読んだときに確かめてある（壊れていれば prev＝None）
            out = site / "live" / "iss" / "iss.json"
            atomic_write(out, dumps(prev).encode("utf-8"))
            age_d = (now - parse_iso(prev["epochUtc"])).total_seconds() / 86400
            info.update({"status": "stale" if failed else ("old" if age_d > 3 else "ok"), "file": "iss/iss.json", "sourceTimestamp": prev["epochUtc"],
                         "generated": prev.get("generatedUtc") or prev["fetchedUtc"], "fetched": prev["fetchedUtc"],
                         "sha256": sha256(out), "size": out.stat().st_size})
    info["failed"] = failed
    return info


# ── 雲 ────────────────────────────────────────────────────────────────
def update_clouds(a, state: Path, site: Path, prev_man: dict | None, now: datetime) -> dict:
    info = {"type": "clouds-gmgsi", "source": "NOAA GMGSI LW（GOES/NOAA・Meteosat/EUMETSAT・Himawari/JMA）", "status": "disabled", "error": None, "failed": False}
    if a.clouds != "on":
        log("雲：公開しない設定（--clouds off）")
        return info
    sys.path.insert(0, str(HERE / "tools" / "live"))
    import fetch_clouds as fc  # tools/live/fetch_clouds.py（手元の取得係と同じ作り方）
    import gmgsi
    st = state / "clouds"
    (st / "f").mkdir(parents=True, exist_ok=True)
    raw = Path(tempfile.mkdtemp(prefix="gmgsi_"))  # 原本：実行の終わりに捨てる
    gmgsi.STORE = raw
    fc.OUT = st
    fc.DIST = raw / "_no_dist" / "x"  # 配信中のフォルダへの写しはしない
    keep_from = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=a.keep_hours)
    stem_t = lambda s: datetime.strptime(s, "%Y%m%dT%H").replace(tzinfo=timezone.utc)  # noqa: E731
    # 前回分：STATE に無ければ公開中のサイトから（直近 keep 時間の分だけ）
    have = {p.name.split(".")[0] for p in (st / "f").glob("*.json")}
    if not have and a.prev and prev_man and prev_man.get("clouds", {}).get("status") in ("ok", "stale", "old"):
        try:
            idx = json.loads(get(a.prev.rstrip("/") + "/live/clouds/index.json"))
            for fr in idx["frames"]:
                if stem_t(fr["stem"]) < keep_from:
                    continue
                names = [f"{fr['stem']}.json"] + [f"{fr['stem']}_{w}.webp" for w in idx["widths"]] + ([f"{fr['stem']}_t{fr['tiles']['level']}_f{k}.utb" for k in range(6)] if fr.get("tiles") else [])
                for n in names:
                    atomic_write(st / "f" / n, get(a.prev.rstrip("/") + "/live/clouds/f/" + n))
            log(f"雲：前回分を公開中のサイトから取った（{len(list((st / 'f').glob('*.json')))} フレーム）")
        except Exception as e:  # noqa: BLE001
            log(f"雲：前回分を公開中のサイトから取れない（{e}）")
    # 古いフレームは STATE からも消す（容量を増やさない）
    for j in (st / "f").glob("*.json"):
        stem = j.name.split(".")[0]
        if stem_t(stem) < keep_from:
            for p in (st / "f").glob(stem + "*"):
                p.unlink()
    # 新しいフレーム：直近 keep 時間の中で、無い時刻を新しい方から max-new 枚（観測がまだ無い時刻は作れない＝未来の雲は作らない）
    newest = now.replace(minute=0, second=0, microsecond=0)
    if now.minute < 40:
        newest -= timedelta(hours=1)  # GMGSI は正時の約 40 分後に出る
    missing, t = [], newest
    while t >= keep_from and len(missing) < a.max_new:
        if not (st / "f" / f"{t:%Y%m%dT%H}.json").exists():
            missing.append(t)
        t -= timedelta(hours=1)
    made, errors = 0, []
    if a.offline_clouds:
        errors.append("（検査）取れない")
        missing = []
    for t in missing:
        try:
            if fc.make_frame(t):
                made += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t:%Y-%m-%dT%H}: {e}")
            log(f"雲：{t:%Y-%m-%dT%H} を作れない（{e}）")
    shutil.rmtree(raw, ignore_errors=True)
    fc.write_index()
    idx_path = st / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    # 公開する物：一覧にあるフレームのファイルと一覧
    out = site / "live" / "clouds"
    (out / "f").mkdir(parents=True, exist_ok=True)
    total = 0
    for fr in idx["frames"]:
        for p in (st / "f").glob(fr["stem"] + "*"):
            if p.suffix == ".part":
                continue
            shutil.copy2(p, out / "f" / p.name); total += p.stat().st_size
    atomic_write(out / "index.json", idx_path.read_bytes())
    latest = idx["frames"][-1]["t"] if idx["frames"] else None
    age_h = (now - parse_iso(latest)).total_seconds() / 3600 if latest else None
    failed = bool(errors) or (made == 0 and missing != [] and age_h is not None and age_h > 2)
    info.update({"status": "missing" if not latest else ("stale" if failed or (age_h or 0) > 3 else "ok"), "file": "clouds/index.json",
                 "sourceTimestamp": latest, "generated": idx.get("generated"), "frames": len(idx["frames"]), "made": made,
                 "sha256": sha256(out / "index.json"), "size": (out / "index.json").stat().st_size, "framesBytes": total,
                 "error": "・".join(errors) or None, "failed": failed and bool(errors)})
    log(f"雲：{len(idx['frames'])} フレーム（新しく {made}）・最新 {latest}・{total / 1e6:.1f} MB")
    return info


# ── 確かめ・manifest ───────────────────────────────────────────────────
def verify_site(site: Path, iss: dict, clouds: dict) -> list[str]:
    bad = []
    if iss.get("file"):
        p = site / "live" / iss["file"]
        if not p.exists() or sha256(p) != iss["sha256"]:
            bad.append("ISS のファイルが manifest と合わない")
        else:
            json.loads(p.read_text(encoding="utf-8"))
    if clouds.get("file"):
        p = site / "live" / clouds["file"]
        idx = json.loads(p.read_text(encoding="utf-8"))
        for fr in idx["frames"]:
            for w in idx["widths"]:
                q = site / "live" / "clouds" / "f" / f"{fr['stem']}_{w}.webp"
                if not q.exists() or q.stat().st_size != fr["bytes"][str(w)]:
                    bad.append(f"雲のフレームが無い／大きさがちがう：{q.name}")
            if fr.get("tiles"):
                for k in range(6):
                    q = site / "live" / "clouds" / "f" / f"{fr['stem']}_t{fr['tiles']['level']}_f{k}.utb"
                    if not q.exists() or q.stat().st_size != fr["tiles"]["bytes"][k]:
                        bad.append(f"雲のタイルが無い／大きさがちがう：{q.name}")
        try:
            from PIL import Image
            for fr in idx["frames"][-1:]:
                Image.open(site / "live" / "clouds" / "f" / f"{fr['stem']}_2048.webp").verify()
        except Exception as e:  # noqa: BLE001
            bad.append(f"雲の画像が読めない：{e}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True); ap.add_argument("--site", required=True); ap.add_argument("--prev", default="")
    ap.add_argument("--clouds", default="off", choices=("on", "off")); ap.add_argument("--keep-hours", type=int, default=24); ap.add_argument("--max-new", type=int, default=3)
    ap.add_argument("--iss-every-hours", type=float, default=6); ap.add_argument("--force-iss", action="store_true"); ap.add_argument("--now", default="")
    ap.add_argument("--offline-iss", action="store_true"); ap.add_argument("--offline-clouds", action="store_true")
    a = ap.parse_args()
    now = parse_iso(a.now + ("" if a.now.endswith("Z") else "+00:00")) if a.now else datetime.now(timezone.utc)
    state, site = Path(a.state), Path(a.site)
    state.mkdir(parents=True, exist_ok=True)
    if site.exists():
        shutil.rmtree(site)
    (site / "live").mkdir(parents=True)
    prev_man = None
    if a.prev:
        try:
            prev_man = json.loads(get(a.prev.rstrip("/") + "/live/manifest.json"))
        except Exception as e:  # noqa: BLE001
            log(f"公開中の manifest が取れない（初めて・または取れない）：{e}")
    t0 = time.time()
    iss = update_iss(a, state, site, prev_man, now)
    clouds = update_clouds(a, state, site, prev_man, now)
    bad = verify_site(site, iss, clouds)
    if bad:
        log("確かめに通らない（配信しない）：" + "・".join(bad))
        return 1
    man = {"version": VERSION, "generated": iso(datetime.now(timezone.utc)), "now": iso(now), "publisher": "solaris-live（GitHub Actions）",
           "iss": {k: v for k, v in iss.items() if k != "failed"}, "clouds": {k: v for k, v in clouds.items() if k != "failed"},
           "credit": {"iss": "CelesTrak GP（元データ：米宇宙軍 Space-Track）",
                      "clouds": [  # 機関ごとの条件を分けて持つ（NOAA 由来だから全部パブリックドメイン、とはしない）
                          {"who": "NOAA", "text": "NOAA/NESDIS GMGSI をもとに SOLARIS が加工。GOES: NOAA", "license": "NOAA Open Data Dissemination（NODD）の公開データ", "url": "https://registry.opendata.aws/noaa-gmgsi/"},
                          {"who": "EUMETSAT", "text": f"Contains modified EUMETSAT Meteosat data {now.year}", "license": "CC BY 4.0（EUMETSAT データポリシー・Core データ）", "url": "https://www.eumetsat.int/data-policy"},
                          {"who": "JMA", "text": "Himawari: 気象庁（JMA）のデータ（NOAA が公開配布）を SOLARIS が加工", "license": "気象庁・NOAA の公開配布の条件", "url": "https://registry.opendata.aws/noaa-himawari/"},
                      ],
                      "noEndorsement": "NOAA・EUMETSAT・気象庁の承認・推薦を受けたものではありません"}}
    # manifest は最後（本体のファイルが全部そろってから）
    atomic_write(site / "live" / "manifest.json", json.dumps(man, ensure_ascii=False, indent=1).encode("utf-8"))
    (site / ".nojekyll").write_bytes(b"")
    if not (site / "index.html").exists():
        (site / "index.html").write_text("<!doctype html><meta charset=utf-8><title>SOLARIS live data</title><p>SOLARIS の公開版が読むライブデータ（ISS の軌道・雲）。<a href=live/manifest.json>manifest</a>", encoding="utf-8")
    size = sum(p.stat().st_size for p in site.rglob("*") if p.is_file())
    log(f"できた：{site}（{size / 1e6:.1f} MB・{time.time() - t0:.0f} 秒）・ISS {iss['status']}・雲 {clouds['status']}")
    return 2 if (iss.get("failed") or clouds.get("failed")) else 0


if __name__ == "__main__":
    sys.exit(main())
