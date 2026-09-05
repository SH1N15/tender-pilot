# -*- coding: utf-8 -*-
"""P-B 法规合规库采集器（B1）。

- 每部法律/规章带多候选来源 URL（官方站点优先），逐个尝试，全部失败如实报告；
- 原始 HTML 与抽取纯文本落盘 data/corpus_raw/laws/，附 meta.json（来源 URL/发布机关/施行日期/抓取时间）；
- 真实性铁律：仅采集全文并保留来源引用，不做任何改写。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

RAW_DIR = Path("data/corpus_raw/laws")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 招投标相关法律/规章（多候选，运行时逐个验证）
LAWS: list[dict] = [
    {
        "name": "中华人民共和国招标投标法（2017修正）",
        "issuer": "全国人大常委会",
        "effective_date": "2017-12-28",
        "candidates": [
            "https://www.12371.cn/2020/06/11/ARTI1591824145773410.shtml",
            "https://www.shanwei.gov.cn/swssjj/gkmlpt/content/0/937/post_937662.html",
        ],
    },
    {
        "name": "中华人民共和国招标投标法实施条例",
        "issuer": "国务院",
        "effective_date": "2012-02-01",
        "candidates": [
            "https://www.ndrc.gov.cn/xxgk/zcfb/qt/201511/t20151103_967423.html",
        ],
    },
    {
        "name": "中华人民共和国政府采购法（2014修正）",
        "issuer": "全国人大常委会",
        "effective_date": "2014-08-31",
        "candidates": [
            "https://www.ndrc.gov.cn/xxgk/zcfb/qt/200507/t20050706_967929.html",
            "https://www.beijing.gov.cn/zhengce/zhengcefagui/qtwj/201908/t20190808_779843.html",
        ],
    },
    {
        "name": "中华人民共和国政府采购法实施条例",
        "issuer": "国务院",
        "effective_date": "2015-03-01",
        "candidates": [
            "https://www.hnsn.gov.cn/jc_snx/33/35/content_579.html",
            "https://www.mofcom.gov.cn/zcfb/zgdwjjmywg/art/2015/art_2338501d371740929e5c2114efa4cfae.html",
        ],
    },
    {
        "name": "政府采购货物和服务招标投标管理办法（财政部令第87号）",
        "issuer": "财政部",
        "effective_date": "2017-10-01",
        "candidates": [
            "https://www.mof.gov.cn/gp/xxgkml/tfs/201707/t20170718_2652766.htm",
            "https://www.gov.cn/gongbao/content/2017/content_5241918.htm",
        ],
    },
    {
        "name": "政府采购质疑和投诉办法（财政部令第94号）",
        "issuer": "财政部",
        "effective_date": "2018-03-01",
        "candidates": [
            "https://czj.wuhan.gov.cn/ZTZL/ZFCGXHXXFBZL/ZCXC/202504/t20250422_2570714.html",
        ],
    },
    {
        "name": "评标委员会和评标方法暂行规定（七部委令第12号）",
        "issuer": "国家计委等七部委",
        "effective_date": "2001-07-05",
        "candidates": [
            "https://www.ndrc.gov.cn/xxgk/zcfb/fzggwl/200506/t20050614_960580.html",
        ],
    },
    {
        "name": "中华人民共和国民法典",
        "issuer": "全国人民代表大会",
        "effective_date": "2021-01-01",
        "candidates": [
            "https://www.moj.gov.cn/pub/sfbgw/zwgkztzl/2025nianzhuanti/2025mfdxcy/2025mfdxcy_mfdql/202505/t20250507_518708.html",
        ],
    },
    {
        "name": "中华人民共和国建筑法（2019修正）",
        "issuer": "全国人大常委会",
        "effective_date": "2019-04-23",
        "candidates": [
            "https://www.12371.cn/2020/06/08/ARTI1591547819081126.shtml",
        ],
    },
    {
        "name": "必须招标的工程项目规定（发改委令第16号）",
        "issuer": "国家发展改革委",
        "effective_date": "2018-06-01",
        "candidates": [
            "https://www.ndrc.gov.cn/xxgk/zcfb/fzggwl/201803/t20180330_960858.html",
            "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=18461",
            "https://zbb.tjcu.edu.cn/info/1023/3123.htm",
            "https://jhcwc.yibinu.edu.cn/info/1067/6268.htm",
        ],
    },
    {
        "name": "政府采购框架协议采购方式管理暂行办法（财政部令第110号）",
        "issuer": "财政部",
        "effective_date": "2022-03-01",
        "candidates": [
            "https://guangdong.chinatax.gov.cn/gdsw/dgsw_zfcgzd/2023-07/03/content_244d9df90de64a4c9dcaea4272d2b9c9.shtml",
        ],
    },
    {
        "name": "中华人民共和国反不正当竞争法（2019修正）",
        "issuer": "全国人大常委会",
        "effective_date": "2019-04-23",
        "candidates": [
            "https://www.csrc.gov.cn/beijing/c105536/c7431842/content.shtml",
        ],
    },
    {
        "name": "电子招标投标办法（发改委等八部委令第20号）",
        "issuer": "国家发展改革委等八部委",
        "effective_date": "2013-05-01",
        "candidates": [
            "https://www.moj.gov.cn/pub/sfbgw/flfggz/flfggzbmgz/201305/t20130530_374184.html",
            "https://bidding.sysu.edu.cn/article/64",
        ],
    },
]

TIMEOUT = 30


def fetch(url: str) -> str:
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=TIMEOUT)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err or "fetch failed")


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def looks_like_full_law(text: str) -> bool:
    return ("第一条" in text or "第一条" in text.replace(" ", "")) and len(text) >= 2000


def safe_name(law_name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|（）()]", "_", law_name)[:60]


def collect(law: dict) -> dict:
    name = law["name"]
    safe = safe_name(name)
    if (RAW_DIR / f"{safe}.txt").exists():
        return {"name": name, "ok": True, "url": "cached", "chars": 0}
    for url in law["candidates"]:
        try:
            html = fetch(url)
            text = extract_text(html)
            if looks_like_full_law(text):
                RAW_DIR.mkdir(parents=True, exist_ok=True)
                (RAW_DIR / f"{safe}.html").write_text(html, encoding="utf-8", errors="ignore")
                (RAW_DIR / f"{safe}.txt").write_text(text, encoding="utf-8")
                meta = {
                    "name": name,
                    "issuer": law["issuer"],
                    "effective_date": law["effective_date"],
                    "source_url": url,
                    "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "chars": len(text),
                }
                (RAW_DIR / f"{safe}.meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return {"name": name, "ok": True, "url": url, "chars": len(text)}
            err = f"content not full law (len={len(text)}, 第一条={'第一条' in text})"
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:200]
        print(f"  [miss] {name} <- {url}: {err}")
    return {"name": name, "ok": False}


def main() -> None:
    results = [collect(law) for law in LAWS]
    ok = [r for r in results if r["ok"]]
    print(f"\n=== collected {len(ok)}/{len(results)} ===")
    for r in results:
        print(("OK  " if r["ok"] else "FAIL"), r["name"], r.get("url", ""), r.get("chars", ""))
    (RAW_DIR / "_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
