# -*- coding: utf-8 -*-
"""
箱6: 過去のデータを集める(collect_hist.py)
公式の番組表(B ファイル)と競走成績(K ファイル)を、1日ずつ、新しい日から古い日へ集めて、
  hist/日付/race_weather_日付.csv (番組表+競走成績+気象。data/ と同じ形)
  hist/日付/payout_日付.csv       (払戻金)
に保存します。読み方は collect.py とまったく同じ(collect.py の関数をそのまま使う)。
オッズ・直前情報は集めません(B/K ファイルに無いため)。

・data/ には書きません(毎日の収集やアプリの取り込みには混ざりません)
・途中で止まっても、もう一度実行すると、集めていない日から続けます
・30日ごとに、集めた分をリポジトリに保存(コミット)します
・アクセスの間隔は collect.py と同じ 3 秒以上
秘密の情報(パスワード・鍵など)は一切使っていません。

使い方: python collect_hist.py 開始日 終了日   (例: python collect_hist.py 20240424 20260423)
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess
from datetime import datetime, timedelta

import pandas as pd

import collect as C

OUT = "hist"
STATE = os.path.join(OUT, "_state.json")      # 開催なし・取れなかった日の記録
COMMIT_EVERY = 30


def save_state(st):
    os.makedirs(OUT, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0, sort_keys=True)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def commit(msg):
    """集めた分をリポジトリに保存する(GitHub Actions の中でだけ動かす)"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    subprocess.run(["git", "add", OUT])
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", msg])
    for _ in range(3):
        subprocess.run(["git", "pull", "-q", "--rebase"])
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            print(f"  → リポジトリに保存しました({msg})", flush=True)
            return
    print("  → 保存(push)に失敗しました。次の保存のときにもう一度試します", flush=True)


def collect_day(date):
    """戻り値: ('ok', レース数) / ('none', 0) 開催なし / ('error', 0)"""
    workdir = tempfile.mkdtemp()
    try:
        b_txt = C.download_txt("b", date, workdir)
        if b_txt == "notfound":
            return "none", 0
        if b_txt is None:
            return "error", 0
        bangumi = C.parse_bangumi(b_txt)
        if len(bangumi) == 0:
            return "none", 0
        k_txt = C.download_txt("k", date, workdir)
        if k_txt in (None, "notfound"):
            return "error", 0
        seiseki = C.parse_seiseki(k_txt)
        weather = C.parse_weather(k_txt)
        payout = C.parse_payout(k_txt)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    race = pd.merge(bangumi, seiseki, on=["場コード", "R", "艇番"], how="left")
    race.insert(0, "日付", date)
    weather.insert(0, "日付", date)
    race = pd.merge(race, weather, on=C.KEY, how="left")
    payout.insert(0, "日付", date)
    payout = payout.reindex(columns=C.PAYOUT_COLS)
    d = os.path.join(OUT, date)
    os.makedirs(d, exist_ok=True)
    race.to_csv(os.path.join(d, f"race_weather_{date}.csv"), index=False, encoding="utf-8-sig")
    payout.to_csv(os.path.join(d, f"payout_{date}.csv"), index=False, encoding="utf-8-sig")
    return "ok", int(race[["場コード", "R"]].drop_duplicates().shape[0])


def main():
    a = sys.argv[1:]
    start = (a[0] if len(a) > 0 else "") or os.environ.get("INPUT_START", "") or "20240424"
    end = (a[1] if len(a) > 1 else "") or os.environ.get("INPUT_END", "") or "20260423"
    d, d0 = datetime.strptime(end, "%Y%m%d"), datetime.strptime(start, "%Y%m%d")
    if not shutil.which("lha"):
        print("lha(解凍の道具)が見つかりません。lhasa をインストールしてください。")
        sys.exit(1)
    st = load_state()
    total, i, done_since = (d - d0).days + 1, 0, 0
    cnt = {"ok": 0, "none": 0, "error": 0}
    print(f"===== 過去のデータを集めます: {start}〜{end}({total} 日・新しい日から順に)=====", flush=True)
    while d >= d0:
        i += 1
        ds = d.strftime("%Y%m%d")
        d -= timedelta(days=1)
        if os.path.exists(os.path.join(OUT, ds, f"race_weather_{ds}.csv")) or st.get(ds) == "none":
            continue
        try:
            kind, n = collect_day(ds)
        except Exception as e:
            kind, n = "error", 0
            print(f"  {ds} エラー: {str(e)[:150]}", flush=True)
        st[ds] = kind
        cnt[kind] += 1
        save_state(st)
        print(f"  {ds} ({i}/{total}) " + {"ok": f"{n} レース", "none": "開催なし", "error": "取れませんでした(もう一度実行すると取り直します)"}[kind], flush=True)
        done_since += 1
        if done_since >= COMMIT_EVERY:
            commit(f"過去データ追加 〜{ds}(自動収集・箱6)")
            done_since = 0
    commit(f"過去データ追加 {start}〜{end}(自動収集・箱6)")
    print(f"===== 終わりました: 集めた {cnt['ok']} 日 / 開催なし {cnt['none']} 日 / 取れなかった {cnt['error']} 日 =====", flush=True)
    if cnt["error"]:
        print("取れなかった日は、もう一度「Run workflow」を押すと取り直します。")


if __name__ == "__main__":
    main()
