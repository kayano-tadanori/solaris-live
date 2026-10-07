# solaris-live

公開版 SOLARIS（https://kayano-tadanori.github.io/solaris/）が読むライブデータ（ISS の軌道・雲）。
GitHub Actions（`.github/workflows/live-data.yml`）が毎時つくり、GitHub Pages で配る。**データは Git にコミットしない**（配信物だけ毎回入れかわる）。

- 中身のソースは uchuu_sim（`tools/publish/live/build_live_repo.py` で組み立てる）。ここで直接直さない
- 配信：`live/manifest.json`（種類・出典・元の時刻・作った時刻・SHA-256・大きさ・状態）・`live/iss/iss.json`・`live/clouds/index.json`＋`live/clouds/f/*`
- ISS：CelesTrak GP（NORAD 25544）を 6 時間ごと。確かめ（NORAD・TLE・元期・必要な鍵・SGP4 で位置）に通ったときだけ新しくする
- 雲：NOAA GMGSI LW を毎時（止めるときは変数 `CLOUDS_PUBLIC=off`）。直近 `CLOUDS_KEEP_HOURS`（既定 24）時間だけ配る。原本は実行の終わりに捨てる
- 取れない回：前回のデータのまま配信し、ジョブを失敗にする
- secret：使わない

## はじめに設定すること（本番のとき）
1. Settings → Pages → Source を「GitHub Actions」
2. （必要なときだけ）Settings → Variables → `CLOUDS_PUBLIC`（既定 on）・`CLOUDS_KEEP_HOURS`・`ISS_EVERY_HOURS`
3. Actions → live-data → Run workflow（`force_iss` に印）で初回を作る

## 出典
- ISS：CelesTrak GP（元データ：米宇宙軍 Space-Track）
- 雲：NOAA/NESDIS GMGSI をもとに SOLARIS が加工（機関ごとの条件）
  - NOAA（GOES・合成）：NOAA Open Data Dissemination の公開データ https://registry.opendata.aws/noaa-gmgsi/
  - EUMETSAT（Meteosat）：Contains modified EUMETSAT Meteosat data <年>。CC BY 4.0（EUMETSAT データポリシー・Core データ）
  - 気象庁（Himawari）：気象庁のデータ（NOAA が公開配布）を加工
  - NOAA・EUMETSAT・気象庁の承認・推薦を受けたものではありません
