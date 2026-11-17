"""
ASIO Visualization Tools v1.0
10+ инструментов для создания красивых графиков и диаграмм без долгого программирования.
Использует matplotlib/seaborn или pure HTML/CSS fallback.
"""

import os
import json
import time
import logging
from typing import Dict, List, Any, Optional
import swallow

logger = logging.getLogger("infinity")

CHARTS_DIR = os.path.expanduser("~/.infinity/asio_agent_v4/charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

def _save_chart(html_content: str, filename: str, title: str) -> Dict:
    """Сохраняет HTML chart и возвращает metadata."""
    path = os.path.join(CHARTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return {
        "path": path,
        "filename": filename,
        "url": f"/charts/{filename}",
        "title": title
    }

def _parse_args_csv(args: str) -> Dict:
    """Парсит аргументы в формате key=value,CSV или JSON."""
    args = args.strip()
    # Try JSON first
    try:
        return json.loads(args)
    except Exception:
        swallow.note('chart_tools._parse_args_csv:L37')
        pass
    
    # Parse key=value format
    result = {}
    for part in args.split('|'):
        part = part.strip()
        if '=' in part:
            key, val = part.split('=', 1)
            result[key.strip()] = val.strip()
        elif ',' in part:
            result['data'] = [x.strip() for x in part.split(',')]
    return result

# === 1. BAR CHART ===
def tool_bar_chart(args):
    """
    Столбчатая диаграмма.
    Формат: Title | labels: A,B,C | values: 10,20,30 | color: #4A90D9
    """
    p = _parse_args_csv(args)
    title = p.get('title', 'Bar Chart')
    labels = p.get('labels', '').split(',') if 'labels' in p else p.get('data', ['A','B','C'])
    values = [float(x) for x in p.get('values', '').split(',')] if 'values' in p else [10,20,30]
    color = p.get('color', '#4A90D9')
    
    max_val = max(values) if values else 1
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px}}
    h2{{color:{color};text-align:center;margin-bottom:30px}}
    .chart{{display:flex;align-items:flex-end;justify-content:center;gap:20px;height:300px;padding:20px 0;border-bottom:2px solid #30363d}}
    .bar{{width:80px;background:linear-gradient(180deg,{color},#2a5a8a);border-radius:8px 8px 0 0;transition:all 0.3s;display:flex;flex-direction:column;align-items:center;justify-content:flex-end}}
    .bar:hover{{transform:scaleY(1.05);filter:brightness(1.2)}}
    .bar-value{{color:#fff;font-weight:bold;padding:8px;font-size:18px}}
    .bar-label{{color:#8b949e;font-size:13px;margin-top:10px}}
    </style></head><body><h2>📊 {title}</h2><div class="chart">"""
    
    for i, (label, value) in enumerate(zip(labels, values)):
        height = max(20, (value / max_val) * 280)
        html += f'<div class="bar" style="height:{height}px"><div class="bar-value">{value}</div></div><div class="bar-label">{label.strip()}</div>'
    
    html += "</div></body></html>"
    info = _save_chart(html, f"bar_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ Bar chart создан: {info['url']}\n{title}", **info, "tool": "bar_chart"}

# === 2. PIE CHART ===
def tool_pie_chart(args):
    """Круговая диаграмма. Формат: Title | labels: A,B | values: 30,70"""
    p = _parse_args_csv(args)
    title = p.get('title', 'Pie Chart')
    labels = p.get('labels', '').split(',') if 'labels' in p else ['A','B']
    values = [float(x) for x in p.get('values', '').split(',')] if 'values' in p else [30,70]
    colors = ['#4A90D9','#50C878','#FF6B6B','#FFD93D','#6C5CE7','#FF8C00','#00CEC9','#E84393']
    
    total = sum(values) or 1
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px;text-align:center}}
    h2{{margin-bottom:30px}}.pie{{width:300px;height:300px;border-radius:50%;margin:0 auto;position:relative;background:conic-gradient("""
    
    gradient_parts = []
    cumulative = 0
    for i, (label, value) in enumerate(zip(labels, values)):
        pct = (value / total) * 100
        gradient_parts.append(f"{colors[i % len(colors)]} {cumulative}% {cumulative + pct}%")
        cumulative += pct
    
    html += ','.join(gradient_parts)
    html += f""")"></div><div style="margin-top:30px;display:flex;justify-content:center;flex-wrap:wrap;gap:15px">"""
    
    for i, (label, value) in enumerate(zip(labels, values)):
        pct = round((value / total) * 100, 1)
        html += f'<div style="display:flex;align-items:center;gap:8px"><div style="width:16px;height:16px;background:{colors[i % len(colors)]};border-radius:3px"></div><span>{label.strip()}: {pct}%</span></div>'
    
    html += "</div></body></html>"
    info = _save_chart(html, f"pie_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ Pie chart создан: {info['url']}", **info, "tool": "pie_chart"}

# === 3. LINE CHART ===
def tool_line_chart(args):
    """Линейный график. Формат: Title | x: 1,2,3 | y: 10,20,15"""
    p = _parse_args_csv(args)
    title = p.get('title', 'Line Chart')
    x_vals = [float(x.strip()) for x in p.get('x', '').split(',')] if 'x' in p else list(range(len(p.get('y', '').split(','))))
    y_vals = [float(x.strip()) for x in p.get('y', '').split(',')]
    color = p.get('color', '#4A90D9')
    
    if not y_vals: y_vals = [0]; x_vals = [0]
    max_y = max(y_vals) if y_vals else 1
    min_y = min(y_vals) if y_vals else 0
    range_y = max_y - min_y or 1
    
    points = []
    for i, (x, y) in enumerate(zip(x_vals, y_vals)):
        px = 50 + (i / max(len(x_vals)-1, 1)) * 500
        py = 300 - ((y - min_y) / range_y) * 250
        points.append(f"{px},{py}")
    
    path_d = "M" + " L".join(points)
    dots = " ".join([f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="6" fill="{color}"/>' for p in points])
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px;text-align:center}}
    h2{{margin-bottom:20px}}svg{{border:1px solid #30363d;border-radius:8px;background:#0d1117}}</style></head>
    <body><h2>📈 {title}</h2><svg width="600" height="350"><path d="{path_d}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round"/>"""
    html += dots
    html += f"""</svg><div style="margin-top:15px;color:#8b949e">X: {', '.join(map(str,x_vals))}<br>Y: {', '.join(map(str,y_vals))}</div></body></html>"""
    info = _save_chart(html, f"line_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ Line chart создан: {info['url']}", **info, "tool": "line_chart"}

# === 4. TABLE ===
def tool_table(args):
    """Красивая HTML таблица. Формат: Title | headers: A,B,C | rows: 1,2,3|4,5,6"""
    p = _parse_args_csv(args)
    title = p.get('title', 'Table')
    headers = [h.strip() for h in p.get('headers', '').split(',')]
    rows_data = p.get('rows', '').split('|')
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px}}
    h2{{color:#58a6ff;margin-bottom:20px}}table{{width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden}}
    th{{background:#161b22;color:#58a6ff;padding:12px;text-align:left}}td{{padding:10px;border-top:1px solid #21262d}}
    tr:hover{{background:#161b22}}</style></head><body><h2>📋 {title}</h2><table><tr>{''.join(f'<th>{h}</th>' for h in headers)}</tr>"""
    
    for row in rows_data:
        if row.strip():
            cells = [c.strip() for c in row.split(',')]
            html += f"<tr>{''.join(f'<td>{c}</td>' for c in cells)}</tr>"
    
    html += "</table></body></html>"
    info = _save_chart(html, f"table_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ Table создан: {info['url']}", **info, "tool": "table"}

# === 5. KPI CARDS ===
def tool_kpi_cards(args):
    """KPI карточки с метриками. Формат: Revenue: $1000| Growth: 25%| Users: 500"""
    p = _parse_args_csv(args)
    title = p.get('title', 'KPI Dashboard')
    colors = ['#4A90D9','#50C878','#FF6B6B','#FFD93D','#6C5CE7','#FF8C00']
    
    items = args.split('|')
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px}}
    h2{{color:#58a6ff;margin-bottom:30px;text-align:center}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px}}
    .card{{background:#161b22;border-radius:12px;padding:25px;border-left:4px solid VAR;border:1px solid #21262d}}
    .card-value{{font-size:32px;font-weight:bold;color:#fff;margin:10px 0}}.card-label{{color:#8b949e;font-size:14px}}</style></head>
    <body><h2>📊 {title}</h2><div class="grid">"""
    
    for i, item in enumerate(items):
        item = item.strip()
        if ':' in item:
            label, value = item.split(':', 1)
        else:
            label, value = f"Metric {i+1}", item
        color_var = colors[i % len(colors)]
        card_html = f"""<div class="card" style="border-left-color:{color_var}"><div class="card-label">{label.strip()}</div><div class="card-value">{value.strip()}</div></div>"""
        html += card_html.replace('VAR', color_var)
    
    html += "</div></body></html>"
    info = _save_chart(html, f"kpi_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ KPI cards создан: {info['url']}", **info, "tool": "kpi_cards"}

# === 6. HEATMAP ===
def tool_heatmap(args):
    """Тепловая карта. Формат: Title | data: 1,2,3|4,5,6|7,8,9"""
    p = _parse_args_csv(args)
    title = p.get('title', 'Heatmap')
    rows = [row.strip().split(',') for row in args.split('|') if row.strip()]
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px;text-align:center}}
    h2{{margin-bottom:20px}}.heatmap{{display:inline-grid;gap:4px}}</style></head><body><h2>🔥 {title}</h2><div class="heatmap" style="grid-template-columns:repeat({len(rows[0])},50px)">"""
    
    all_vals = [float(v) for row in rows for v in row]
    max_val = max(all_vals) if all_vals else 1
    
    for row in rows:
        for val in row:
            v = float(val)
            intensity = v / max_val
            bg = f"rgba(74,144,217,{0.1 + intensity * 0.9})"
            html += f'<div style="background:{bg};padding:15px;border-radius:4px;color:#fff;font-weight:bold">{v}</div>'
    
    html += "</div></body></html>"
    info = _save_chart(html, f"heatmap_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ Heatmap создан: {info['url']}", **info, "tool": "heatmap"}

# === 7. PROGRESS BARS ===
def tool_progress_bars(args):
    """Прогресс бары. Формат: Task1: 75|Task2: 90|Task3: 45"""
    items = args.split('|')
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px;max-width:800px;margin:0 auto}}
    h2{{color:#58a6ff;margin-bottom:30px;text-align:center}}.item{{margin-bottom:20px}}
    .label{{display:flex;justify-content:space-between;margin-bottom:8px}}
    .bar{{background:#21262d;border-radius:8px;height:24px;overflow:hidden}}
    .fill{{height:100%;border-radius:8px;background:linear-gradient(90deg,#4A90D9,#50C878);transition:width 0.5s}}</style></head><body><h2>📈 Progress</h2>"""
    
    for item in items:
        item = item.strip()
        if ':' in item:
            label, pct = item.rsplit(':', 1)
            pct = min(100, max(0, float(pct.strip())))
        else:
            label, pct = item, 0
        html += f'<div class="item"><div class="label"><span>{label.strip()}</span><span>{pct:.0f}%</span></div><div class="bar"><div class="fill" style="width:{pct}%"></div></div></div>'
    
    html += "</body></html>"
    info = _save_chart(html, f"progress_{int(time.time())}.html", "Progress")
    return {"success": True, "output": f"✅ Progress bars создан: {info['url']}", **info, "tool": "progress_bars"}

# === 8. NEWS CARDS ===
def tool_news_cards(args):
    """Карточки новостей. Формат: Title1|URL1|Desc1 || Title2|URL2|Desc2"""
    groups = args.split('||')
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px}}
    h2{{color:#58a6ff;margin-bottom:30px;text-align:center}}.grid{{display:grid;gap:20px;max-width:900px;margin:0 auto}}
    .card{{background:#161b22;border:1px solid #21262d;border-radius:12px;padding:20px;transition:all 0.3s}}
    .card:hover{{border-color:#58a6ff;transform:translateY(-2px)}}
    .card a{{color:#58a6ff;text-decoration:none;font-size:18px;font-weight:bold}}.card p{{color:#8b949e;margin-top:10px}}</style></head>
    <body><h2>📰 Последние новости</h2><div class="grid">"""
    
    for group in groups:
        parts = group.strip().split('|')
        if len(parts) >= 3:
            title, url, desc = parts[0].strip(), parts[1].strip(), parts[2].strip()
            html += f'<div class="card"><a href="{url}" target="_blank">{title}</a><p>{desc}</p></div>'
        elif len(parts) >= 2:
            title, url = parts[0].strip(), parts[1].strip()
            html += f'<div class="card"><a href="{url}" target="_blank">{title}</a></div>'
        elif parts:
            html += f'<div class="card"><p>{parts[0].strip()}</p></div>'
    
    html += "</div></body></html>"
    info = _save_chart(html, f"news_{int(time.time())}.html", "News")
    return {"success": True, "output": f"✅ News cards создан: {info['url']}", **info, "tool": "news_cards"}

# === 9. WEATHER WIDGET ===
def tool_weather_widget(args):
    """Виджет погоды. Формат: City: Moscow | Temp: 25°C | Condition: Sunny | Humidity: 60%
    ВАЖНО: погодные данные (Temp/Condition/Humidity/Wind) берутся автоматически из open-meteo API —
    агент передаёт только город, остальные параметры игнорируются."""
    import urllib.request, urllib.parse, json as _json

    p = _parse_args_csv(args)
    city = (p.get('city') or '').strip()

    weather_data = None
    temp = condition = humidity = wind = '--'
    try:
        if city:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=ru"
            with urllib.request.urlopen(geo_url, timeout=10) as resp:
                geo = _json.loads(resp.read().decode('utf-8', errors='replace'))
            locs = geo.get("results") or []
            if locs:
                lat, lon = locs[0]["latitude"], locs[0]["longitude"]
                city = locs[0].get("name", city)
                w_url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                         f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
                         f"&timezone=auto")
                with urllib.request.urlopen(w_url, timeout=10) as resp:
                    w = _json.loads(resp.read().decode('utf-8', errors='replace'))
                cur = w.get("current", {})
                temp = f"{cur.get('temperature_2m', '--')}°C"
                humidity = f"{cur.get('relative_humidity_2m', '--')}%"
                wind = f"{cur.get('wind_speed_10m', '--')} м/с"
                wcode = cur.get("weather_code", 0)
                wmo = {0: "Ясно", 1: "В основном ясно", 2: "Переменная облачность", 3: "Пасмурно",
                       45: "Туман", 48: "Изморозь", 51: "Лёгкая морось", 53: "Морось", 55: "Сильная морось",
                       61: "Лёгкий дождь", 63: "Дождь", 65: "Сильный дождь", 66: "Ледяной дождь", 67: "Сильный ледяной дождь",
                       71: "Лёгкий снег", 73: "Снег", 75: "Сильный снег", 77: "Снежные зёрна",
                       80: "Лёгкие ливни", 81: "Ливни", 82: "Сильные ливни",
                       85: "Лёгкий снегопад", 86: "Сильный снегопад", 95: "Гроза", 96: "Гроза с градом", 99: "Сильная гроза с градом"}
                condition = wmo.get(wcode, f"Код {wcode}")
                weather_data = {"city": city, "temp": temp, "condition": condition, "humidity": humidity, "wind": wind}
    except Exception as e:
        logger.warning(f"[WEATHER] open-meteo error: {e}")

    if weather_data is None and not city:
        return {"success": False, "output": "❌ Формат: City: Moscow", "tool": "weather_widget"}

    icon = {'Ясно': '☀️', 'В основном ясно': '🌤️', 'Переменная облачность': '⛅', 'Пасмурно': '☁️',
            'Туман': '🌫️', 'Изморозь': '🌫️', 'Морось': '🌦️', 'Дождь': '🌧️', 'Снег': '❄️', 'Гроза': '⛈️'}.get(condition, '🌤️')
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0d1117,#161b22);color:#e6edf3;padding:40px;display:flex;justify-content:center}}
    .weather{{background:rgba(22,27,34,0.8);border-radius:20px;padding:40px;text-align:center;border:1px solid #30363d;backdrop-filter:blur(10px)}}
    .icon{{font-size:80px;margin-bottom:20px}}.city{{font-size:28px;font-weight:bold;margin-bottom:10px}}
    .temp{{font-size:64px;font-weight:bold;color:#58a6ff}}.details{{display:flex;gap:30px;justify-content:center;margin-top:20px}}
    .detail{{background:#0d1117;padding:15px 25px;border-radius:10px}}.detail-label{{color:#8b949e;font-size:12px}}
    .detail-value{{font-size:18px;font-weight:bold}}</style></head><body><div class="weather">
    <div class="icon">{icon}</div><div class="city">{city}</div><div class="temp">{temp}</div>
    <div class="details"><div class="detail"><div class="detail-label">Condition</div><div class="detail-value">{condition}</div></div>
    <div class="detail"><div class="detail-label">Humidity</div><div class="detail-value">{humidity}</div></div>
    <div class="detail"><div class="detail-label">Wind</div><div class="detail-value">{wind}</div></div></div></div></body></html>"""
    info = _save_chart(html, f"weather_{int(time.time())}.html", f"Weather - {city}")
    output = f"🌤️ Погода в {city}: {condition}, {temp}, влажность {humidity}, ветер {wind}"
    return {"success": True, "output": output, "weather": weather_data, **info, "tool": "weather_widget"}

# === 10. COMPARISON TABLE ===
def tool_comparison_table(args):
    """Таблица сравнения. Формат: Feature|Product A|Product B || Speed|Fast|Slow || Price|$10|$5"""
    groups = args.split('||')
    headers = [h.strip() for h in groups[0].strip().split('|')]
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px}}
    h2{{color:#58a6ff;margin-bottom:20px;text-align:center}}table{{width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden}}
    th{{background:#161b22;color:#58a6ff;padding:15px}}td{{padding:12px;border-top:1px solid #21262d;text-align:center}}
    tr:hover{{background:#161b22}}</style></head><body><h2>⚖️ Comparison</h2><table><tr>{''.join(f'<th>{h}</th>' for h in headers)}</tr>"""
    
    for group in groups[1:]:
        cells = [c.strip() for c in group.strip().split('|')]
        html += f"<tr>{''.join(f'<td>{c}</td>' for c in cells)}</tr>"
    
    html += "</table></body></html>"
    info = _save_chart(html, f"comparison_{int(time.time())}.html", "Comparison")
    return {"success": True, "output": f"✅ Comparison table создан: {info['url']}", **info, "tool": "comparison_table"}

# === 11. RADAR CHART ===
def tool_radar_chart(args):
    """Радарная диаграмма. Формат: Title | labels: A,B,C | values: 7,8,6"""
    p = _parse_args_csv(args)
    title = p.get('title', 'Radar')
    labels = p.get('labels', '').split(',') if 'labels' in p else ['A','B','C','D','E']
    values = [float(x.strip()) for x in p.get('values', '').split(',')]
    max_val = max(values) if values else 10
    
    n = len(labels)
    angle_step = 360 / n
    points = []
    for i, val in enumerate(values):
        angle = i * angle_step - 90
        import math
        r = (val / max_val) * 120
        x = 200 + r * math.cos(math.radians(angle))
        y = 200 + r * math.sin(math.radians(angle))
        points.append(f"{x},{y}")
    
    polygon = " ".join(points)
    
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;padding:30px;text-align:center}}h2{{margin-bottom:20px}}
    svg{{border:1px solid #30363d;border-radius:8px}}</style></head><body><h2>🎯 {title}</h2>
    <svg width="400" height="400"><polygon points="{polygon}" fill="rgba(74,144,217,0.3)" stroke="#4A90D9" stroke-width="2"/>"""
    
    for i, (label, point) in enumerate(zip(labels, points)):
        x, y = point.split(',')
        html += f'<circle cx="{x}" cy="{y}" r="5" fill="#4A90D9"/><text x="{float(x)+10}" y="{float(y)}" fill="#e6edf3" font-size="12">{label.strip()}</text>'
    
    html += "</svg></body></html>"
    info = _save_chart(html, f"radar_{int(time.time())}.html", title)
    return {"success": True, "output": f"✅ Radar chart создан: {info['url']}", **info, "tool": "radar_chart"}
