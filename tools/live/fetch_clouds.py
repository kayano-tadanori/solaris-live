"""時間変化する雲の取得係（Stage 6B・2026-10-04・研究所 PC で 1 時間ごと）。ISS と同じ「外部データ層」（JPL 系の暦パックとは別）。

  python tools/live/fetch_clouds.py                      直近 LIVE_HOURS 時間を取り、雲のフレームを作る（晴れの基準に要る過去も取る）
  python tools/live/fetch_clouds.py --range 2026-09-26T00 2026-09-28T23   その期間のフレームを作る（検査・記録用）
  python tools/live/fetch_clouds.py --index              フレームの一覧（index.json）だけ作り直す
  … --range A B --rebuild                                 その期間のフレームを作り直す（決め方を変えたとき・上書き）

流れ：NOAA GMGSI LW（tools/live/gmgsi.py：取得の URL・大きさ・SHA-256・格子・メタデータを data_live/clouds/gmgsi_lw/…json に記録）
  → 雲の濃さ（tools/live/cloud_alpha.py の CHOSEN：同じ正時の過去の晴れの基準との差。表示の濃さは display_opacity）→ 正距円筒図法 4096×2048・2048×1024 へ（双線形）
  → 緯度 71° より極側は 0（観測なし。公式の有効範囲）・手前 2° で観測した雲を薄くしていく→ WebP（グレー・品質 80）→ public/live/clouds/f/<YYYYMMDDTHH>_<幅>.webp
  一覧：public/live/clouds/index.json（作ったフレームの全部。欠けた時刻は入れない）。dist/live/clouds/ があればそこにも写す（作り直さずに配信中の画面へ）
晴れの基準の過去が足りない（必要な日数の 6 割未満）時刻はフレームを作らない（不確かな雲を作らない）
原本（data_live/clouds/gmgsi_lw）は直近 14 日だけ残す（tools/live/rotate_clouds_raw.py が取得のあとに消す・2026-10-06 オトン承認。1 日 約 150 MB）
外部へ配るとき（2026-10-07 オトン判断で公開版に配る）：出典を機関ごとに表示する（NOAA・EUMETSAT〔CC BY 4.0〕・気象庁。src/data/cloudCredits.ts・tools/publish/live）
"""
from __future__ import annotations

import io
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloud_alpha import CHOSEN, clear_ref, display_opacity  # noqa: E402
from gmgsi import H, LAT_N, LAT_S, VALID_LAT, W, fetch_hour, load_hour, utc  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from make_earth_tiles import tile_dirs, write_bundle  # noqa: E402  Stage 4 と同じ面・同じ画素の決め方（256 px・縁 2 px）

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "public" / "live" / "clouds"
DIST = ROOT / "dist" / "live" / "clouds"
WIDTHS = (2048, 4096)
LIVE_HOURS = 36
MIN_HIST = 0.6
# 近距離の雲のタイル（Stage 6C）：cube-sphere の段 L2（1 面 4×4 枚・256 px・縁 2 px）。1 画素 約 0.089°（面の中央）
#   ＝GMGSI の元の細かさ（赤道 0.072°）を超えない（それより細かい段は作らない＝架空の細部を作らない）
TILE_LEVEL = 2
FADE_DEG = 2.0   # アプリのシェーダー（uCloudCov）と同じ


def merc_rows(lat: np.ndarray) -> np.ndarray:
    y0 = np.log(np.tan(np.pi / 4 + np.radians(LAT_N) / 2)); y1 = np.log(np.tan(np.pi / 4 + np.radians(LAT_S) / 2))
    return (np.log(np.tan(np.pi / 4 + np.radians(np.clip(lat, -89.9, 89.9)) / 2)) - y0) / (y1 - y0) * (H - 1)


def to_equirect(a: np.ndarray, width: int) -> np.ndarray:
    """雲の濃さ（メルカトルの格子）→ 正距円筒図法（画素の中心・双線形）。緯度 VALID_LAT より極側は 0"""
    h = width // 2
    lat = 90 - (np.arange(h) + 0.5) * 180 / h
    lon = -180 + (np.arange(width) + 0.5) * 360 / width
    fy = merc_rows(lat)[:, None]
    fx = ((lon + 180) / (360 / W))[None, :]
    y0 = np.clip(np.floor(fy).astype(int), 0, H - 1); y1 = np.clip(y0 + 1, 0, H - 1); ty = np.clip(fy - np.floor(fy), 0, 1)
    x0 = np.floor(fx).astype(int) % W; x1 = (x0 + 1) % W; tx = fx - np.floor(fx)
    out = (a[y0, x0] * (1 - tx) + a[y0, x1] * tx) * (1 - ty) + (a[y1, x0] * (1 - tx) + a[y1, x1] * tx) * ty
    # 観測の範囲の境目：手前 FADE_DEG で観測した雲を薄くしていき、外は 0（作り物の雲は足さない。急な境目は WebP でにじむ）
    k = np.clip((VALID_LAT - np.abs(lat)) / FADE_DEG, 0.0, 1.0)
    return out * (k * k * (3 - 2 * k))[:, None]


def merc_sample(a: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """メルカトルの格子の雲の濃さを緯度・経度（度）で双線形に読む。観測の範囲の境目の手前で薄くし、外は 0（to_equirect と同じ）"""
    fy = merc_rows(lat)
    fx = ((lon + 180) / (360 / W)) % W
    y0 = np.clip(np.floor(fy).astype(int), 0, H - 1); y1 = np.clip(y0 + 1, 0, H - 1); ty = np.clip(fy - np.floor(fy), 0, 1)
    x0 = np.floor(fx).astype(int) % W; x1 = (x0 + 1) % W; tx = fx - np.floor(fx)
    out = (a[y0, x0] * (1 - tx) + a[y0, x1] * tx) * (1 - ty) + (a[y1, x0] * (1 - tx) + a[y1, x1] * tx) * ty
    k = np.clip((VALID_LAT - np.abs(lat)) / FADE_DEG, 0.0, 1.0)
    return out * (k * k * (3 - 2 * k))


def make_tiles(a: np.ndarray, stem: str) -> list[int]:
    """雲の濃さ → 段 L2 のタイル（WebP グレー・品質 80）を面ごとのまとめファイル f/<stem>_t2_f<面>.utb に。戻り値＝面ごとの大きさ"""
    sizes = []
    n = 2 ** TILE_LEVEL
    for f in range(6):
        tiles = []
        for y in range(n):
            for x in range(n):
                d = tile_dirs(f, TILE_LEVEL, x, y)
                lat = np.degrees(np.arcsin(np.clip(d[..., 2], -1, 1))); lon = np.degrees(np.arctan2(d[..., 1], d[..., 0]))
                img = Image.fromarray(np.clip(np.rint(merc_sample(a, lat, lon) * 255), 0, 255).astype(np.uint8), "L")
                b = io.BytesIO(); img.save(b, "WEBP", quality=80, method=6)
                tiles.append((x, y, b.getvalue()))
        path = OUT / "f" / f"{stem}_t{TILE_LEVEL}_f{f}.utb"
        write_bundle(path, tiles)
        sizes.append(path.stat().st_size)
    return sizes


def make_frame(t: datetime) -> bool:
    cur = load_hour(t) or fetch_hour(t)
    if cur is None:
        return False
    hist = []
    for k in range(1, CHOSEN.days + 1):
        tt = t - timedelta(days=k)
        h = load_hour(tt) or fetch_hour(tt)
        if h is not None:
            hist.append(h[0])
    if len(hist) < MIN_HIST * CHOSEN.days:
        print(f"  {utc(t)}：晴れの基準の過去が足りない（{len(hist)}/{CHOSEN.days} 日）→ 作らない", flush=True)
        return False
    ref = clear_ref(np.stack(hist), CHOSEN.pct)
    a = display_opacity(cur[0], ref, CHOSEN)
    stem = f"{t:%Y%m%dT%H}"
    (OUT / "f").mkdir(parents=True, exist_ok=True)
    meta = {"t": utc(t), "stem": stem, "bytes": {}, "platforms": cur[1].get("platform"), "sha256": cur[1].get("sha256"),
            "source_url": cur[1].get("url"), "history_days": len(hist)}
    sc = sorted(cur[1].get("source_scans") or [])
    if sc:
        meta["scans"] = [f"{s[:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:00Z" for s in (sc[0], sc[-1])]
    for w in WIDTHS:
        img = Image.fromarray(np.clip(np.rint(to_equirect(a, w) * 255), 0, 255).astype(np.uint8), "L")
        b = io.BytesIO(); img.save(b, "WEBP", quality=80, method=6)
        (OUT / "f" / f"{stem}_{w}.webp").write_bytes(b.getvalue())
        meta["bytes"][str(w)] = len(b.getvalue())
    meta["tiles"] = {"level": TILE_LEVEL, "px": 256, "border": 2, "bytes": make_tiles(a, stem)}
    (OUT / "f" / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"  {utc(t)}：作った（過去 {len(hist)} 日・{', '.join(f'{k}: {v / 1e3:.0f} KB' for k, v in meta['bytes'].items())}）", flush=True)
    return True


def write_index() -> None:
    frames = []
    for j in sorted((OUT / "f").glob("*.json")):
        m = json.loads(j.read_text(encoding="utf-8"))
        if all((OUT / "f" / f"{m['stem']}_{w}.webp").exists() for w in WIDTHS):
            fr = {k: m[k] for k in ("t", "stem", "bytes", "platforms", "scans", "sha256") if k in m}
            if "tiles" in m and all((OUT / "f" / f"{m['stem']}_t{m['tiles']['level']}_f{f}.utb").exists() for f in range(6)):
                fr["tiles"] = m["tiles"]
            frames.append(fr)
    idx = {
        "version": 1, "generated": utc(datetime.now(timezone.utc)),
        "source": {"name": "NOAA GMGSI", "nameJa": "NOAA の静止気象衛星の全球合成（GMGSI）", "product": "赤外 LW（約 11 µm）",
                   "url": "https://registry.opendata.aws/noaa-gmgsi/", "interval": "1 時間", "resolution": "約 8 km（赤道）",
                   "credit": "NOAA/NESDIS GMGSI（GOES/NOAA・Meteosat/EUMETSAT・Himawari/JMA）"},
        "method": {"name": CHOSEN.label(), "summary": "同じ場所・同じ正時の過去の晴れの値（基準）より冷たい分を雲の濃さにする"},
        "coverageLatDeg": VALID_LAT, "fadeDeg": FADE_DEG, "widths": list(WIDTHS), "maxGapHours": 3, "frames": frames,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    if DIST.parent.exists():
        DIST.mkdir(parents=True, exist_ok=True)
        (DIST / "f").mkdir(exist_ok=True)
        for f in (OUT / "f").iterdir():
            d = DIST / "f" / f.name
            if not d.exists() or d.stat().st_size != f.stat().st_size:
                shutil.copy2(f, d)
        shutil.copy2(OUT / "index.json", DIST / "index.json")
    print(f"一覧：{len(frames)} フレーム", flush=True)


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def main() -> int:
    args = sys.argv[1:]
    if "--index" in args:
        write_index(); return 0
    rebuild = "--rebuild" in args  # 決め方を変えたとき：フレームを作り直す（上書き）
    if "--range" in args:
        i = args.index("--range"); t0, t1 = parse(args[i + 1]), parse(args[i + 2])
    else:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        t0, t1 = now - timedelta(hours=LIVE_HOURS), now
    # 先に要る時刻（フレームの時刻と、その晴れの基準の過去）を並行して取る（1 本ずつだと遅い）
    need, t = set(), t0
    while t <= t1:
        if not (OUT / "f" / f"{t:%Y%m%dT%H}.json").exists():
            need.add(t)
            need.update(t - timedelta(days=k) for k in range(1, CHOSEN.days + 1))
        t += timedelta(hours=1)
    need = sorted(x for x in need if load_hour(x) is None)
    if need:
        print(f"取る：{len(need)} 時刻", flush=True)
        from concurrent.futures import ThreadPoolExecutor
        def one(x):
            for _ in range(3):
                try:
                    return fetch_hour(x)
                except Exception as e:  # 通信の途中で切れた等：やり直す
                    err = e
            print(f"  {utc(x)}：取れない {err}", flush=True)
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(one, need))
    t = t0
    while t <= t1:
        if rebuild or not (OUT / "f" / f"{t:%Y%m%dT%H}.json").exists():
            try:
                make_frame(t)
            except Exception as e:  # 一つの時刻の失敗で全体を止めない（次の起動でまた試す）
                print(f"  {utc(t)}：失敗 {e}", flush=True)
        t += timedelta(hours=1)
    write_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
