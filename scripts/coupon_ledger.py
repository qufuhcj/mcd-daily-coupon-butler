#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦每日领券管家 · 券资产台账引擎（coupon_ledger.py）

输入：当日通过麦当劳 MCP 拉取的券包 / 积分快照 JSON
职责：
  1. 与历史台账比对，找出今日新增券
  2. 按当前时间给每张券分类（今日可用 / 未来生效 / 已过期）
  3. 计算临期券（--warn-days 内到期）与积分过期预警
  4. 计算折扣率与实惠度评级（买一送一自动按半价估算，其余按 originalPrice 计算）
  5. 生成每日券资产日报（Markdown 或 HTML，按 --report 扩展名自动选择）
  6. 统计连续打卡天数、累计入账券数

设计原则：纯 Python 标准库、零依赖、幂等（同一天重复跑只更新不重复入账）。

输入 JSON 格式（由 AI 把 MCP 返回整理后写入）：
{
  "date": "2026-10-13",
  "coupons": [
    {
      "name": "中薯条买一送一",
      "price": 13.5,                       # 用券价格（元），可选
      "originalPrice": 29.0,               # 餐品原价合计（元），可选；买一送一类券传单件原价
      "validStart": "2026-10-12 10:30",    # 可选
      "validEnd": "2026-10-16 23:59",      # 可选
      "note": "周一至周五 10:30-23:59",     # 使用时段说明，可选
      "tags": ["到店专用", "外送专用"],      # 可选
      "source": "麦麦省"                    # 可选
    }
  ],
  "points": {"available": 88.9, "expireThisMonth": 0, "expireNextMonth": 0}  # 可选
}

折扣率规则：
  - 券名含"买一送一/买一赠一"：视为两件合计，折扣率 = price / (originalPrice×2)；
    未提供 originalPrice 时按 5 折估算
  - 其余券：折扣率 = price / originalPrice（originalPrice 缺失则不展示折扣）
  - 评级：≤5折 神券 ｜ ≤7折 超值 ｜ ≤8.5折 划算 ｜ 其余 常规

用法：
  python3 coupon_ledger.py --input today.json --ledger data/ledger.json \
      --report reports/2026-10-13.html --warn-days 1     # HTML 日报
  python3 coupon_ledger.py --input today.json --report reports/2026-10-13.md   # Markdown
  python3 coupon_ledger.py --input today.json --json      # 机器可读输出
"""

import argparse
import json
import sys
from datetime import datetime

DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ---------- 基础工具 ----------

def parse_dt(text, end=False):
    """解析日期时间字符串；纯日期且 end=True 时补 23:59:59。"""
    if not text:
        return None
    text = str(text).strip()
    for fmt in DT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt
    return None


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def coupon_key(coupon):
    """同一名称、同一生效期的券视为同一张（同名不同天的早餐券能区分开）。"""
    return "{}|{}".format(coupon.get("name", ""), coupon.get("validStart", ""))


def fmt_price(price):
    if price is None:
        return "-"
    try:
        return "¥{:.2f}".format(float(price)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(price)


def to_float(value):
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------- 折扣率与实惠度 ----------

BOGO_KEYWORDS = ("买一送一", "买一赠一", "第二件半价")


def coupon_value(coupon):
    """计算折扣率与实惠度评级。

    返回 dict: {rate, tier, savings, bogo}
      rate    折扣率（0.5 = 5折），无法计算时为 None
      tier    评级：神券/超值/划算/常规/None
      savings 预计省（元），无法计算时为 None
      bogo    是否买一送一类
    """
    name = str(coupon.get("name", ""))
    price = to_float(coupon.get("price"))
    orig = to_float(coupon.get("originalPrice"))
    bogo = any(k in name for k in BOGO_KEYWORDS)
    if "第二件半价" in name:
        bogo = True

    rate, base = None, None
    if bogo:
        base = orig * 2 if orig else None
        rate = (price / base) if (price and base) else 0.5
    elif price and orig:
        base = orig
        rate = price / orig

    savings = None
    if price and base and base > price:
        savings = base - price

    tier = None
    if rate is not None:
        if rate <= 0.5:
            tier = "神券"
        elif rate <= 0.7:
            tier = "超值"
        elif rate <= 0.85:
            tier = "划算"
        else:
            tier = "常规"
    return {"rate": rate, "tier": tier, "savings": savings, "bogo": bogo}


def value_text(coupon):
    """Markdown 用的折扣描述，如「≈4.7折·神券」；无法计算返回空串。"""
    v = coupon_value(coupon)
    if v["rate"] is None:
        return ""
    approx = "" if v["bogo"] and coupon.get("originalPrice") else "≈"
    text = "{}{:.1f}折·{}".format(approx, v["rate"] * 10, v["tier"])
    if v["savings"]:
        text += "（约省{}）".format(fmt_price(v["savings"]))
    return text


# ---------- 分类与统计 ----------

def classify(coupon, now):
    """按当前时间分类：active(可用) / future(未生效) / expired(已过期)。"""
    start = parse_dt(coupon.get("validStart"))
    end = parse_dt(coupon.get("validEnd"), end=True)
    if end and now > end:
        return "expired"
    if start and now < start:
        return "future"
    return "active"


def days_left(coupon, now):
    end = parse_dt(coupon.get("validEnd"), end=True)
    if not end:
        return None
    return (end.date() - now.date()).days


def historical_keys(ledger):
    """台账中所有出现过的券 key（用于判断今日是否新增）。"""
    keys = set()
    for day in ledger.get("days", []):
        for c in day.get("coupons", []):
            keys.add(coupon_key(c))
    return keys


def streak_days(ledger, today_str):
    """连续打卡天数：从今天往前数台账中连续出现的日期数（当天自动计入）。"""
    dates = {d.get("date") for d in ledger.get("days", []) if d.get("date")}
    dates.add(today_str)
    streak = 0
    day = datetime.strptime(today_str, "%Y-%m-%d")
    while day.strftime("%Y-%m-%d") in dates:
        streak += 1
        day = day.fromordinal(day.toordinal() - 1)
    return streak


def bucketize(coupons, now, warn_days):
    """按状态与临期分桶，返回 (expiring_today, expiring_soon, active, future)。"""
    expiring_today, expiring_soon, active, future = [], [], [], []
    for c in coupons:
        status = classify(c, now)
        left = days_left(c, now)
        if status == "active":
            if left == 0:
                expiring_today.append(c)
            elif left is not None and left <= warn_days:
                expiring_soon.append(c)
            else:
                active.append(c)
        elif status == "future":
            future.append(c)
    return expiring_today, expiring_soon, active, future


def build_tips(expiring_today, expiring_soon, points, best, warn_days=1):
    tips = []
    if expiring_today:
        tips.append("🚨 有 {} 张券今天过期，优先安排这一顿！".format(len(expiring_today)))
    if expiring_soon:
        tips.append("🔔 {} 张券 {} 天内到期，列进本周干饭计划 🗓️".format(
            len(expiring_soon), warn_days))
    if best:
        tips.append("🔥 今天最值得用的神券：**{}**（{}），想吃就趁现在 😋".format(
            best.get("name", ""), value_text(best) or "折扣力度最大"))
    if points.get("expireThisMonth"):
        tips.append("🪙 本月有 {} 积分要过期，尽快到麦享会使用或兑换 ⏳".format(
            points["expireThisMonth"]))
    if not tips:
        tips.append("✅ 券包状态健康，按需使用即可；管家明早继续帮你领新券 🌅")
    return tips


def best_coupon(coupons):
    """全场折扣率最低（最实惠）的券。"""
    best, best_rate = None, None
    for c in coupons:
        rate = coupon_value(c)["rate"]
        if rate is not None and (best_rate is None or rate < best_rate):
            best, best_rate = c, rate
    return best


# ---------- Markdown 日报 ----------

def build_markdown(snapshot, ledger, now, warn_days):
    coupons = snapshot.get("coupons", [])
    points = snapshot.get("points") or {}
    date_str = snapshot.get("date") or now.strftime("%Y-%m-%d")
    expiring_today, expiring_soon, active, future = bucketize(coupons, now, warn_days)

    prev_keys = historical_keys(ledger)
    new_coupons = [] if not ledger.get("days") else \
        [c for c in coupons if coupon_key(c) not in prev_keys]

    def coupon_line(c, with_left=True):
        parts = ["- **{}**".format(c.get("name", "未命名券"))]
        parts.append("用券价 {}".format(fmt_price(c.get("price"))))
        vt = value_text(c)
        if vt:
            parts.append("**{}**".format(vt))
        end = c.get("validEnd", "")
        if end and with_left:
            left = days_left(c, now)
            if left == 0:
                parts.append("**今天到期**（{}）".format(end))
            elif left is not None:
                parts.append("{}（剩 {} 天）".format(end, left))
        tags = c.get("tags") or []
        if tags:
            parts.append("［{}］".format("／".join(tags)))
        note = c.get("note")
        if note:
            parts.append("⏱ {}".format(note))
        return " ".join(parts)

    lines = []
    lines.append("# 🎫 麦麦每日券资产日报 · {}".format(date_str))
    lines.append("")
    streak = streak_days(ledger, date_str)
    summary = "打卡第 {} 天 ｜ 券包共 {} 张（可用 {} · 待生效 {}）".format(
        streak, len(coupons), len(expiring_today) + len(expiring_soon) + len(active),
        len(future))
    if new_coupons:
        summary += " ｜ 今日新增 {} 张".format(len(new_coupons))
    lines.append("> {}".format(summary))
    lines.append("")

    if expiring_today:
        lines.append("## ⏰ 今天就过期（抓紧用）")
        lines.extend(coupon_line(c) for c in expiring_today)
        lines.append("")
    if expiring_soon:
        lines.append("## 🔔 {} 天内到期".format(warn_days))
        lines.extend(coupon_line(c) for c in expiring_soon)
        lines.append("")
    if active:
        lines.append("## ✅ 可用中")
        lines.extend(coupon_line(c) for c in active)
        lines.append("")
    if future:
        lines.append("## 📆 已领待生效")
        lines.extend(coupon_line(c) for c in future)
        lines.append("")
    if new_coupons:
        lines.append("## 🆕 今日新入手")
        lines.extend("- {}".format(c.get("name", "未命名券")) for c in new_coupons)
        lines.append("")

    if points:
        lines.append("## 🪙 积分账户")
        avail = points.get("available")
        expire_m = points.get("expireThisMonth")
        expire_n = points.get("expireNextMonth")
        lines.append("- 可用积分：{}".format(avail if avail is not None else "-"))
        if expire_m:
            lines.append("- ⚠️ **本月将有 {} 积分过期**，尽快到麦享会使用或兑换".format(expire_m))
        if expire_n:
            lines.append("- ⏳ 下月将过期积分：{}".format(expire_n))
        lines.append("")

    lines.append("## 💡 今日建议")
    lines.extend("- {}".format(t) for t in
                 build_tips(expiring_today, expiring_soon, points,
                            best_coupon(expiring_today + active), warn_days))
    lines.append("")
    lines.append("---")
    lines.append("*数据来源：麦当劳 MCP（auto-bind-coupons / query-my-coupons / query-my-account），由 coupon_ledger.py 自动生成。*")
    return "\n".join(lines)


# ---------- HTML 日报 ----------

TIER_STYLE = {
    "神券": ("badge hot", "🔥"),
    "超值": ("badge great", "✨"),
    "划算": ("badge good", "👍"),
    "常规": ("badge norm", "·"),
}


def badge_html(coupon):
    v = coupon_value(coupon)
    if v["rate"] is None:
        return ""
    cls, icon = TIER_STYLE.get(v["tier"], ("badge norm", "·"))
    approx = "" if v["bogo"] and coupon.get("originalPrice") else "≈"
    text = "{}{:.1f}折".format(approx, v["rate"] * 10)
    return '<span class="{}">{} {}</span>'.format(cls, esc(text), icon)


def tag_html(t):
    """渠道标签着色：到店=蓝 ｜ 外送/麦乐送=绿 ｜ 其他=灰。"""
    cls = "tag"
    if "到店" in t or "堂食" in t:
        cls = "tag dine"
    elif "外送" in t or "麦乐送" in t or "配送" in t:
        cls = "tag delivery"
    return '<i class="{}">{}</i>'.format(cls, esc(t))


def coupon_card_html(c, now, tone=""):
    name = esc(c.get("name", "未命名券"))
    v = coupon_value(c)
    price = fmt_price(c.get("price"))
    orig = to_float(c.get("originalPrice"))
    img = str(c.get("image") or "").strip()

    thumb = ""
    if img:
        thumb = ('<img class="thumb" src="{src}" alt="" loading="lazy" '
                 'onerror="this.style.display=\'none\'">').format(src=esc(img))

    price_html = '<span class="price">💰 {}</span>'.format(esc(price))
    if orig:
        if v["bogo"]:
            orig_note = "原价 {} ×2".format(esc(fmt_price(orig)))
        else:
            orig_note = "原价 {}".format(esc(fmt_price(orig)))
        price_html += '<span class="orig">{}</span>'.format(esc(orig_note))
    if v["savings"]:
        price_html += '<span class="save">💸 省{}</span>'.format(esc(fmt_price(v["savings"])))

    left = days_left(c, now)
    if left == 0:
        meta = '<span class="meta urgent">🚨 今天到期 · {}</span>'.format(
            esc(c.get("validEnd", "")))
    elif left is not None:
        meta = '<span class="meta">📅 {} · 剩 {} 天</span>'.format(
            esc(c.get("validEnd", "")), left)
    else:
        meta = '<span class="meta">📅 {}</span>'.format(
            esc(c.get("validEnd", "") or "长期有效"))

    tags = "".join(tag_html(t) for t in (c.get("tags") or []))
    note = '<span class="note">⏱ {}</span>'.format(esc(c["note"])) if c.get("note") else ""
    badge = badge_html(c)
    return (
        '<div class="card coupon {tone}">{thumb}'
        '<div class="cbody">'
        '<div class="row1"><span class="name">{name}</span>{badge}</div>'
        '<div class="row2">{price_html}</div>'
        '<div class="row2">{meta}</div>'
        '<div class="row3">{tags}{note}</div>'
        '</div></div>'
    ).format(tone=tone, thumb=thumb, name=name, badge=badge,
             price_html=price_html, meta=meta, tags=tags, note=note)


def section_html(icon, title, subtitle, cards_html, cls="", collapsed=False,
                 anchor=None):
    """anchor: 区块锚点 id，供顶部磁贴跳转。"""
    if not cards_html:
        return ""
    sub = '<p class="sec-sub">{}</p>'.format(esc(subtitle)) if subtitle else ""
    id_attr = ' id="{}"'.format(anchor) if anchor else ""
    if collapsed:
        return ('<details class="sec {cls}"{id_attr}><summary>{icon} {title} ▾</summary>{sub}'
                '<div class="grid">{cards}</div></details>'
                ).format(cls=cls, id_attr=id_attr, icon=icon, title=esc(title),
                         sub=sub, cards=cards_html)
    return ('<section class="sec {cls}"{id_attr}><h2>{icon} {title}</h2>{sub}'
            '<div class="grid">{cards}</div></section>'
            ).format(cls=cls, id_attr=id_attr, icon=icon, title=esc(title),
                     sub=sub, cards=cards_html)


def build_html(snapshot, ledger, now, warn_days):
    coupons = snapshot.get("coupons", [])
    points = snapshot.get("points") or {}
    date_str = snapshot.get("date") or now.strftime("%Y-%m-%d")
    expiring_today, expiring_soon, active, future = bucketize(coupons, now, warn_days)

    prev_keys = historical_keys(ledger)
    new_coupons = [] if not ledger.get("days") else \
        [c for c in coupons if coupon_key(c) not in prev_keys]
    streak = streak_days(ledger, date_str)
    usable = len(expiring_today) + len(expiring_soon) + len(active)

    try:
        wd = WEEKDAYS[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except ValueError:
        wd = ""

    # 顶部告警横幅（今天过期 / 积分清零）
    alerts = []
    if expiring_today:
        alerts.append("<b>{} 张券今天过期</b>".format(len(expiring_today)))
    if points.get("expireThisMonth"):
        alerts.append("<b>{}</b> 积分月底清零".format(esc(points["expireThisMonth"])))
    if alerts:
        alert_html = '<div class="alert">🚨 ' + "，".join(alerts) + "，别浪费！🍔</div>"
    else:
        alert_html = ""

    new_banner = ('<div class="new-banner">🆕 今日新入手 {} 张，管家已收进卡包 ✅</div>'
                  ).format(len(new_coupons)) if new_coupons else ""

    # 核心数据磁贴（一屏看完，不用滚；可用券/待生效可点击跳转）
    pts_num = points.get("available")
    stats_html = (
        '<div class="stats">'
        '<div class="stat red"><div class="num">{streak}</div><div class="lbl">🔥 连续打卡(天)</div></div>'
        '<a class="stat link" href="#sec-active"><div class="num">{usable}</div><div class="lbl">🎫 可用券(张) ▸</div></a>'
        '<a class="stat link" href="#sec-future" onclick="var d=document.getElementById(\'sec-future\');if(d)d.open=true;">'
        '<div class="num">{future}</div><div class="lbl">📆 待生效(张) ▸</div></a>'
        '<div class="stat"><div class="num">{pts}</div><div class="lbl">🪙 可用积分</div></div>'
        '</div>'
    ).format(streak=streak, usable=usable, future=len(future),
             pts=esc(pts_num if pts_num is not None else "-"))

    sec_today = section_html(
        "⏰", "今天就过期 · 抓紧用", "最后一天，不用就没了 😤",
        "".join(coupon_card_html(c, now, "tone-hot") for c in expiring_today),
        cls="hot", anchor="sec-today")
    sec_soon = section_html(
        "🔔", "{} 天内到期".format(warn_days), "列进本周干饭计划 🗓️",
        "".join(coupon_card_html(c, now, "tone-warn") for c in expiring_soon))
    sec_active = section_html(
        "✅", "可用中", "折扣力度已标注，随取随用 💪",
        "".join(coupon_card_html(c, now) for c in active), anchor="sec-active")
    sec_future = section_html(
        "📆", "已领待生效（{} 张）".format(len(future)),
        "麦当劳按天投放，到点管家会提醒你 ⏰",
        "".join(coupon_card_html(c, now) for c in future), collapsed=True,
        anchor="sec-future")

    new_html = ""
    if new_coupons:
        items = "".join('<span class="new-item">＋ {}</span>'.format(esc(c.get("name", "")))
                        for c in new_coupons)
        new_html = ('<section class="sec"><h2>🆕 今日新入手</h2>'
                    '<div class="new-list">{}</div></section>').format(items)

    points_html = ""
    if points:
        avail = points.get("available")
        expire_m = points.get("expireThisMonth")
        expire_n = points.get("expireNextMonth")
        warn = ""
        if expire_m:
            warn = ('<div class="pt-warn">🚨 本月将有 <b>{}</b> 积分过期，'
                    '尽快到麦享会使用或兑换 ⏳</div>').format(esc(expire_m))
        elif expire_n:
            warn = ('<div class="pt-sub">⏳ 下月将过期积分：{}</div>'
                    ).format(esc(expire_n))
        points_html = (
            '<section class="sec"><h2>🪙 积分账户</h2>'
            '<div class="pt-card"><div class="pt-num">{}</div>'
            '<div class="pt-label">💰 可用积分</div>{warn}</div></section>'
        ).format(esc(avail if avail is not None else "-"), warn=warn)

    tips = build_tips(expiring_today, expiring_soon, points,
                      best_coupon(expiring_today + active), warn_days)
    tips_html = "".join('<li>{}</li>'.format(t.replace("**", "")) for t in tips)

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>麦麦每日券资产日报 · {date}</title>
<style>
:root {{
  --bg: #f5f5f7; --card: #ffffff; --ink: #1d1d1f; --ink2: #6e6e73;
  --red: #da291c; --gold: #b8860b; --green: #34c759; --orange: #ff9500;
  --blue: #0071e3; --line: #e8e8ed;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro SC", "PingFang SC",
    "Helvetica Neue", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--ink);
  -webkit-font-smoothing: antialiased; padding: 28px 16px 40px;
}}
.wrap {{ max-width: 860px; margin: 0 auto; }}
header {{ text-align: center; margin-bottom: 8px; }}
.kicker {{ font-size: 12px; color: var(--red); font-weight: 700;
  letter-spacing: .14em; margin-bottom: 6px; }}
h1 {{ font-size: 28px; font-weight: 800; letter-spacing: -.02em; }}
.date-sub {{ color: var(--ink2); font-size: 14px; margin-top: 6px; }}

.alert {{ margin-top: 16px; background: linear-gradient(135deg, #da291c, #ff6a3d);
  color: #fff; border-radius: 14px; padding: 12px 18px; font-size: 15px;
  font-weight: 600; text-align: center; box-shadow: 0 4px 14px rgba(218,41,28,.28); }}
.new-banner {{ margin-top: 10px; background: #fff8e6; color: var(--gold);
  border-radius: 12px; padding: 10px 16px; font-size: 14px; font-weight: 600;
  text-align: center; }}

.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
  margin-top: 14px; }}
.stat {{ background: var(--card); border-radius: 14px; padding: 14px 6px;
  text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.stat .num {{ font-size: 26px; font-weight: 800; }}
.stat .lbl {{ font-size: 12px; color: var(--ink2); margin-top: 2px; }}
.stat.red .num {{ color: var(--red); }}
a.stat {{ text-decoration: none; color: var(--ink); }}
a.stat:hover {{ box-shadow: 0 3px 10px rgba(0,113,227,.18); }}
a.stat:hover .lbl {{ color: var(--blue); }}

.sec {{ margin-top: 22px; }}
.sec h2 {{ font-size: 17px; font-weight: 750; margin-bottom: 3px; }}
.sec-sub {{ font-size: 12.5px; color: var(--ink2); margin-bottom: 10px; }}
/* "今天就过期" 区块：浅红底 + 深红标题，高对比可读 */
.sec.hot {{ background: #fdecea; border-radius: 16px; padding: 14px 16px 16px; }}
.sec.hot h2 {{ color: #a8170c; }}
.sec.hot .sec-sub {{ color: #b3544b; }}
details.sec summary {{ cursor: pointer; font-size: 17px; font-weight: 750;
  list-style: none; user-select: none; }}
details.sec summary::-webkit-details-marker {{ display: none; }}
details.sec summary:hover {{ color: var(--blue); }}
details.sec[open] summary {{ margin-bottom: 3px; }}

.grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }}
@media (max-width: 680px) {{ .grid {{ grid-template-columns: 1fr; }} }}
.card {{ background: var(--card); border-radius: 16px; padding: 13px 15px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.card.coupon {{ border-left: 4px solid var(--green);
  display: flex; gap: 12px; align-items: flex-start; }}
.card.tone-hot {{ border-left-color: var(--red); background: #fff8f7; }}
.card.tone-warn {{ border-left-color: var(--orange); }}
.thumb {{ width: 62px; height: 62px; object-fit: cover; border-radius: 12px;
  flex: none; background: #f2f2f4; }}
.cbody {{ flex: 1; min-width: 0; }}
.row1 {{ display: flex; align-items: center; justify-content: space-between;
  gap: 8px; }}
.name {{ font-size: 15px; font-weight: 700; }}
.badge {{ font-size: 12px; font-weight: 700; color: #fff; border-radius: 999px;
  padding: 3px 10px; white-space: nowrap; flex: none; }}
.badge.hot {{ background: linear-gradient(135deg, #da291c, #ff6a3d); }}
.badge.great {{ background: var(--orange); }}
.badge.good {{ background: var(--green); }}
.badge.norm {{ background: #b0b0b5; }}
.row2 {{ display: flex; align-items: baseline; flex-wrap: wrap; gap: 7px;
  margin-top: 6px; }}
.price {{ font-size: 17px; font-weight: 800; }}
.orig {{ font-size: 11.5px; color: var(--ink2); text-decoration: line-through; }}
.save {{ font-size: 11.5px; font-weight: 700; color: #1e8e3e;
  background: #e8f5ec; border-radius: 6px; padding: 2px 8px; }}
.meta {{ font-size: 12px; color: var(--ink2); }}
.meta.urgent {{ color: var(--red); font-weight: 750; }}
.row3 {{ margin-top: 7px; }}
.row3 i.tag {{ font-style: normal; font-size: 11px; border-radius: 5px;
  padding: 2px 8px; margin-right: 5px; color: var(--ink2); background: #f2f2f4; }}
.row3 i.tag.dine {{ color: #0066cc; background: #e8f1ff; }}
.row3 i.tag.delivery {{ color: #1e8e3e; background: #e8f5ec; }}
.note {{ font-size: 11px; color: var(--ink2); }}

.new-list {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.new-item {{ background: #fff8e6; color: var(--gold); font-size: 13px;
  font-weight: 650; border-radius: 999px; padding: 6px 14px; }}

.pt-card {{ background: var(--card); border-radius: 16px; padding: 18px;
  text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.pt-num {{ font-size: 34px; font-weight: 800;
  background: linear-gradient(135deg, #da291c, #ff9500);
  -webkit-background-clip: text; background-clip: text; color: transparent; }}
.pt-label {{ font-size: 13px; color: var(--ink2); margin-top: 2px; }}
.pt-warn {{ margin-top: 12px; background: #fff3f2; color: var(--red);
  font-size: 13px; border-radius: 10px; padding: 10px 14px; }}
.pt-sub {{ margin-top: 10px; font-size: 12px; color: var(--ink2); }}

.tips {{ background: var(--card); border-radius: 16px; padding: 16px 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.tips h2 {{ font-size: 17px; font-weight: 750; margin-bottom: 8px; }}
.tips li {{ font-size: 14px; line-height: 1.75; color: var(--ink);
  margin-left: 18px; }}

footer {{ text-align: center; color: var(--ink2); font-size: 12px;
  margin-top: 28px; line-height: 1.8; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="kicker">🍟 MCD DAILY COUPON BUTLER 🍟</div>
  <h1>🎫 麦麦每日券资产日报</h1>
  <div class="date-sub">🗓️ {date} {weekday} · ☀️ 管家已为你领好今天的券</div>
</header>
{alert}
{new_banner}
{stats}
<section class="sec"><div class="tips"><h2>💡 今日建议</h2><ul>{tips}</ul></div></section>
{sec_today}{sec_soon}{sec_active}{sec_future}{new_html}{points_html}
<footer>🍟 数据来源：麦当劳 MCP（auto-bind-coupons / query-my-coupons / query-my-account）· 由 coupon_ledger.py 自动生成 🍔<br>
券面金额为持券支付价，最终以官方渠道实时结果为准</footer>
</div>
</body>
</html>
""".format(date=esc(date_str), weekday=esc(wd), alert=alert_html,
           new_banner=new_banner, stats=stats_html,
           sec_today=sec_today, sec_soon=sec_soon, sec_active=sec_active,
           sec_future=sec_future, new_html=new_html, points_html=points_html,
           tips=tips_html)


# ---------- 主流程 ----------

def main():
    parser = argparse.ArgumentParser(description="麦麦每日领券管家 · 券资产台账引擎")
    parser.add_argument("--input", required=True, help="当日快照 JSON 路径")
    parser.add_argument("--ledger", default="data/ledger.json", help="历史台账 JSON 路径")
    parser.add_argument("--report", help="日报输出路径（.md 生成 Markdown，.html 生成 HTML；缺省打印 Markdown 到 stdout）")
    parser.add_argument("--warn-days", type=int, default=1, help="临期预警天数（默认 1）")
    parser.add_argument("--now", help="覆盖当前时间，格式 YYYY-MM-DD HH:MM（调试用）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 摘要")
    args = parser.parse_args()

    now = parse_dt(args.now) or datetime.now()
    snapshot = load_json(args.input)
    if not snapshot:
        print("错误：无法读取输入文件 {}".format(args.input), file=sys.stderr)
        return 2

    ledger = load_json(args.ledger) or {"days": []}
    date_str = snapshot.get("date") or now.strftime("%Y-%m-%d")

    # 先用「昨天为止」的台账生成报告（今日新增的判定基准），再更新台账
    if args.report and args.report.endswith(".html"):
        report = build_html(snapshot, ledger, now, args.warn_days)
    else:
        report = build_markdown(snapshot, ledger, now, args.warn_days)

    # 台账更新（幂等：同一天重复执行则覆盖当天快照）
    ledger["days"] = [d for d in ledger["days"] if d.get("date") != date_str]
    ledger["days"].append({
        "date": date_str,
        "coupons": snapshot.get("coupons", []),
        "points": snapshot.get("points") or {},
    })
    ledger["days"].sort(key=lambda d: d.get("date", ""))
    if args.ledger:
        import os
        os.makedirs(os.path.dirname(args.ledger) or ".", exist_ok=True)
        with open(args.ledger, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)

    expiring_today, expiring_soon, active, future = bucketize(
        snapshot.get("coupons", []), now, args.warn_days)
    summary = {
        "date": date_str,
        "total": len(snapshot.get("coupons", [])),
        "usable": len(expiring_today) + len(expiring_soon) + len(active),
        "future": len(future),
        "expiringToday": len(expiring_today),
        "expiringSoon": len(expiring_soon),
        "newToday": 0,
        "streak": streak_days(ledger, date_str),
        "points": snapshot.get("points") or {},
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.report:
        import os
        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        if not args.json:
            print("日报已写入：{}".format(args.report))
    elif not args.json:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
