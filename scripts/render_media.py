"""Render the README images from the recorded run in examples/local-run/run.json. Nothing is
typed in by hand: every bubble and every row comes from what the run wrote.

    python scripts/render_media.py   (needs: pip install playwright && playwright install chromium)
"""
import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = json.loads((ROOT / "examples" / "local-run" / "run.json").read_text(encoding="utf-8"))
MEDIA = ROOT / "media"
FONT = "-apple-system, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', 'Segoe UI', Helvetica, Arial, sans-serif"
NAMES = {"amy": "Amy", "wei": "Wei", "lin": "Lin", "sam": "Sam"}
STYLE = f"""<style>
 body{{margin:0;background:#fff;font-family:{FONT}}}
 .row{{display:inline-flex;gap:18px;padding:18px;background:#f3f3f3}}
 .phone{{width:340px;background:#ededed;border-radius:18px;overflow:hidden;border:1px solid #d6d6d6;flex:none}}
 .bar{{background:#ededed;border-bottom:1px solid #d9d9d9;text-align:center;padding:12px 0 10px;font-weight:600;font-size:15px}}
 .bar small{{display:block;font-weight:400;color:#888;font-size:11px;margin-top:2px}}
 .msgs{{padding:10px 10px 14px}}
 .m{{display:flex;margin:9px 0;align-items:flex-start;gap:7px}} .m.me{{flex-direction:row-reverse}}
 .av{{width:32px;height:32px;border-radius:5px;flex:none;color:#fff;font-size:13px;font-weight:700;
   display:flex;align-items:center;justify-content:center}}
 .b{{max-width:236px;padding:8px 10px;border-radius:5px;font-size:13.5px;line-height:1.45;white-space:pre-wrap;
   word-break:break-word;background:#fff}}
 .me .b{{background:#95ec69}}
 .tag{{font-size:10px;color:#8a8a8a;margin:-5px 0 0 39px}} .me+.tag{{display:none}}
 .note{{text-align:center;color:#999;font-size:11px;margin:10px 0}}
 h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#666;font-size:13px;line-height:1.45;margin-bottom:14px;max-width:720px}}
 .wrap{{display:inline-block;padding:22px 26px;background:#fff}}
 table{{border-collapse:collapse;font-size:13px;width:960px}}
 td,th{{border-bottom:1px solid #e6e6e6;padding:7px 9px;text-align:left;vertical-align:top}}
 th{{background:#f6f6f6;font-weight:600}} .ok{{color:#07a35a;font-weight:600}} .warn{{color:#c27c00;font-weight:600}}
 .bad{{color:#d93025;font-weight:600}} code{{font-family:Menlo,Consolas,monospace;font-size:12px}}
</style>"""
VIA = {"passive": "passive reply", "customer_service": "customer-service message"}


def phone(name, msgs):
    out = []
    for m in msgs:
        if m["from"] == "follower":
            out.append(f"<div class='m me'><div class='av' style='background:#576b95'>{NAMES[name][0]}</div>"
                       f"<div class='b'>{html.escape(m['text'])}</div></div>")
        elif m["from"] == "account":
            out.append(f"<div class='m'><div class='av' style='background:#2e7d32'>茶</div>"
                       f"<div class='b'>{html.escape(m['text'])}</div></div>"
                       f"<div class='tag'>{VIA.get(m.get('via'), '')}</div>")
        else:
            out.append(f"<div class='note'>{html.escape(m['text'])}</div>")
    return (f"<div class='phone'><div class='bar'>Lumen Tea House<small>chat with {NAMES[name]} (fake WeChat, "
            f"real Claude)</small></div><div class='msgs'>{''.join(out)}</div></div>")


def page(body, width=None):
    w = f"style='width:{width}px'" if width else ""
    return f"<html><head><meta charset='utf-8'>{STYLE}</head><body><div class='wrap' {w}>{body}</div></body></html>"


def chats_page(names, title, sub):
    return page(f"<h1>{title}</h1><div class='sub'>{sub}</div><div class='row'>"
                + "".join(phone(n, RUN["chats"][n]) for n in names) + "</div>")


def steps_page():
    s = RUN["summary"]
    rows = []
    for st in RUN["steps"]:
        out = st.get("outcome") or st.get("result")
        cls = "ok"
        if out in ("no_reply",):
            cls = "warn"
        if isinstance(out, str) and (out.startswith("HTTP 403") or out in ("window_closed",)):
            cls = "bad"
        detail = st.get("note") or st.get("after") or ""
        if st.get("reply_type") == "text" and st.get("reply"):
            detail = detail or html.escape(st["reply"][:90]) + ("…" if len(st["reply"]) > 90 else "")
        elif out == "no_reply":
            detail = detail or "answered 'success' in time; the answer followed as a customer-service message"
        rows.append(f"<tr><td>{st['step']}</td><td>{html.escape(st['title'])}</td><td class='{cls}'>"
                    f"{html.escape(str(out))}</td><td>{st.get('seconds', '')}</td><td>{detail}</td></tr>")
    answers = ", ".join(f"{x}s" for x in s["answer_seconds"])
    sub = (f"{s['date']}: the real service and a fake WeChat on two real HTTP ports, real {s['model']}. "
           f"{s['claude_calls']} Claude calls, {s['tokens_in']:,} tokens in / {s['tokens_out']:,} out, about "
           f"${s['cost_usd']:.3f}. Claude answer times: {answers}. access_token fetched {s['token_fetches']} times "
           f"(once at start, once after the forced expiry).")
    return page("<h1>The recorded run, step by step</h1>"
                f"<div class='sub'>{html.escape(sub)}</div><table><tr><th>#</th><th>What happened</th><th>Result</th>"
                "<th>s</th><th>Detail</th></tr>" + "".join(rows) + "</table>")


def main():
    MEDIA.mkdir(exist_ok=True)
    jobs = {
        "chat-en-zh.png": chats_page(["amy", "wei"], "Answers in the follower's language",
                                     "Real Claude replies recorded in the local run. Amy writes English (her gift-wrap "
                                     "question was sent in AES safe mode), Wei writes Chinese, asks for a person with "
                                     "转人工, and a colleague replies through the customer-service API."),
        "chat-edge-cases.png": chats_page(["lin", "sam"], "Mixed language, slow answers, attacks",
                                          "Lin mixes Chinese and English; her second answer took longer than the "
                                          "passive budget, so it arrived as a customer-service message. Sam tries a "
                                          "prompt injection, asks something the knowledge base does not cover, and "
                                          "sends a photo. The injection answer took over 4 s, so it came as a customer-service message after his next question."),
        "run-steps.png": steps_page(),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(device_scale_factor=2)
        for name, doc in jobs.items():
            pg.set_content(doc, wait_until="load")
            pg.locator(".wrap").screenshot(path=str(MEDIA / name))
            print("wrote", MEDIA / name)
        browser.close()


if __name__ == "__main__":
    main()
