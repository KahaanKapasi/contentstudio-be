"""Two reference scenes: shown to Gemini in the prompt and rendered in the tests, so they must always work."""

STAT_REVEAL = '''from studio_motion import *

scene = Scene(palette="madrid")
scene.glow(W / 2, scene.safe.cy, u(560), opacity=0.30)
top, mid, bottom = scene.safe.split(2, 5, 3)

label = Text("CHAMPIONS LEAGUE TITLES", size=u(58), color=scene.palette.muted, x=top.cx, y=top.cy, max_width=top.w)
label.slide_in(at=0.2, from_="top")

big = Counter(0, 15, at=0.5, dur=1.8, size=u(430), color=scene.palette.accent, x=mid.cx, y=mid.cy)
big.pop(at=0.4)

caption = Text("MORE THAN ANY OTHER CLUB", size=u(70), x=bottom.cx, y=bottom.cy - u(50), max_width=bottom.w)
caption.slide_in(at=2.2)
bar = Bar(bottom.cx, bottom.cy + u(110), w=bottom.w * 0.8, h=u(26), value=1.0, color=scene.palette.accent)
bar.grow(at=2.5, dur=1.2)

scene.add(label, big, caption, bar)
scene.fade_out(0.5)
'''

TOP5_COUNTDOWN = '''from studio_motion import *

scene = Scene(palette="madrid")
data = [("Mbappe", 44), ("Vinicius", 36), ("Bellingham", 30), ("Rodrygo", 24), ("Valverde", 18)]  # best first
top = scene.safe
title = Text("TOP 5 SCORERS", size=u(92), color=scene.palette.accent, x=top.cx, y=top.top + u(60), max_width=top.w)
title.slide_in(at=0.1, from_="top")
scene.add(title)

area = Box(top.x, top.y + u(190), top.w, top.h - u(190))
rows = area.rows(len(data), gap=u(22))
step = (DURATION - 3.0) / len(data)
best = data[0][1]
for i, (name, goals) in enumerate(data):
    row = rows[i]
    t0 = 0.8 + (len(data) - 1 - i) * step  # reveal from 5th place up to 1st
    card = RoundedRect(row.cx, row.cy, row.w, row.h, color=scene.palette.card, radius=u(30))
    rank = Text(str(i + 1), size=row.h * 0.62, color=scene.palette.accent, x=row.left + u(70), y=row.cy)
    label = Text(name.upper(), size=row.h * 0.34, anchor="left", x=row.left + u(150), y=row.cy - row.h * 0.17)
    score = Counter(0, goals, at=t0 + 0.2, dur=1.0, size=row.h * 0.5, anchor="right", x=row.right - u(40), y=row.cy)
    bar = Bar(row.left + u(150), row.cy + row.h * 0.24, w=(row.w - u(420)) * goals / best, h=u(16), anchor="left", color=scene.palette.accent2)
    bar.grow(at=t0 + 0.3, dur=0.9)
    for el in (card, rank, label, score, bar):
        el.slide_in(at=t0, from_="right", distance=u(220), dur=0.55)
    scene.add(card, rank, label, score, bar)
scene.fade_out(0.5)
'''

EXAMPLES = (("Stat reveal", STAT_REVEAL), ("Top-5 countdown", TOP5_COUNTDOWN))
