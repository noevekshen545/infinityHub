#!/usr/bin/env python3
import os, json, time, random, logging, threading
import swallow

logger = logging.getLogger("notification_engine")

NOTIF_FILE = os.path.expanduser("~/.infinity/notifications.json")

_NOTIF_LOCK = threading.Lock()

MESSAGE_TEMPLATES = [
    # Books
    "📚 Мы подобрали для вас {count} книг в жанре «{genre}» — возможно, вам понравится!",
    "📖 За последнюю неделю вы прочитали {hours}ч — отличный результат! Вот ещё несколько книг в вашем стиле.",
    "📕 Вы любите «{genre}» — у нас есть для вас {count} новых рекомендаций.",
    "📗 На основе ваших предпочтений — подборка книг, которые могут вас заинтересовать.",
    "📘 Ваш любимый жанр «{genre}» — вот что ещё стоит почитать.",
    "📙 Читателям «{genre}» также нравится этот автор — {author}.",
    "📚 Вы прочитали уже {total_hours}ч книг! Продолжайте в том же духе. Вот свежая подборка.",
    "📖 Мы заметили, что вы редко читаете «{genre}» — возможно, стоит попробовать что-то новое?",
    "📕 Книга дня: подборка специально для вас!",
    "📗 Вы дочитываете книги быстрее {percent}% пользователей. Вот что читают другие в жанре «{genre}».",

    # Media / Music
    "🎵 Вы слушали «{artist}» {count} раз на этой неделе. Вот похожие исполнители.",
    "🎶 Ваш любимый жанр «{genre}» — новая музыка в этом стиле уже ждёт вас.",
    "🎧 За эту неделю вы прослушали {hours}ч музыки. Новые треки для вашего плейлиста.",
    "🎵 Поклонникам «{artist}» также нравится — загляните в рекомендации.",
    "🎶 Вы слушаете «{genre}» чаще всего. Вот что ещё стоит послушать.",
    "🎧 Мы подобрали для вас {count} треков, которые могут вам понравиться.",
    "🎵 Ваш музыкальный вкус разнообразен — {genres_count} жанров за неделю!",
    "🎶 Исполнитель недели: {artist}. У нас есть ещё {count} треков этого исполнителя.",

    # ZIM / Articles
    "📰 Вы интересуетесь темой «{genre}» — вот похожие статьи из ZIM-архивов.",
    "📄 На основе ваших поисковых запросов — подборка статей по теме «{genre}».",
    "📑 Вы прочитали {count} статей на этой неделе! Вот что ещё может быть интересно.",
    "📰 Статьи по теме «{genre}» — {count} новых материалов для изучения.",
    "📄 Пользователи, которые читали о «{genre}», также интересовались...",

    # Mixed / General
    "📊 Ваша статистика за неделю: {total_hours}ч активности. Больше всего времени в модуле «{top_module}».",
    "⭐ На основе ваших интересов мы подготовили персональные рекомендации.",
    "📊 Вы достигли нового рекорда — {total_hours}ч за неделю!",
    "🎯 Попробуйте что-то новое: у вас накопилось {count} непрочитанных рекомендаций.",
    "📈 Ваша активность растёт! За эту неделю на {percent}% больше, чем на прошлой.",
    "🔔 Не пропустите: новые рекомендации уже готовы специально для вас.",
    "🎉 Поздравляем! Вы провели в приложении {total_hours}ч на этой неделе.",
    "💡 Совет дня: обратите внимание на раздел рекомендаций — там много интересного.",
]

def _load_notifications():
    if os.path.exists(NOTIF_FILE):
        try:
            with open(NOTIF_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            swallow.note('notification_engine._load_notifications:L57')
            return []
    return []

def _save_notifications(notifs):
    os.makedirs(os.path.dirname(NOTIF_FILE), exist_ok=True)
    with open(NOTIF_FILE, "w", encoding="utf-8") as f:
        json.dump(notifs, f, ensure_ascii=False, indent=2)

def get_active_notifications(max_age_hours=72):
    notifs = _load_notifications()
    cutoff = time.time() - max_age_hours * 3600
    return [n for n in notifs if n.get("time", 0) > cutoff and not n.get("dismissed", False)]

def dismiss_notification(notif_id):
    notifs = _load_notifications()
    for n in notifs:
        if n.get("id") == notif_id:
            n["dismissed"] = True
    _save_notifications(notifs)

def add_notification(text, module="recommendations", type="recommendation", time_ts=None,
                     profile="", data=None):
    """Добавляет одно уведомление в файл (thread-safe).

    profile — кому адресовано (id профиля). Пусто — общее уведомление,
    видимое всем, как рекомендации. data — полезная нагрузка для действия
    (например, id доступа к файлу), панель уведомлений её не показывает.
    """
    with _NOTIF_LOCK:
        notifs = _load_notifications()
        notif = {
            "id": f"notif_{int(time.time() * 1000)}_{len(notifs)}",
            "text": text,
            "module": module,
            "type": type,
            "time": time_ts if time_ts is not None else time.time(),
            "date": time.strftime("%Y-%m-%d"),
            "dismissed": False,
        }
        if profile:
            notif["profile"] = str(profile)
        if isinstance(data, dict) and data:
            notif["data"] = data
        notifs.append(notif)
        _save_notifications(notifs)
    return notif

def generate_notifications(stats_data, max_per_day=3):
    today = time.strftime("%Y-%m-%d")
    notifs = _load_notifications()
    today_count = sum(1 for n in notifs if n.get("date") == today and not n.get("dismissed", False))
    if today_count >= max_per_day:
        return []

    new_notifs = []
    templates_pool = list(MESSAGE_TEMPLATES)
    random.shuffle(templates_pool)
    selected = templates_pool[:max_per_day - today_count]
    days_sent_today = {n.get("template_idx") for n in notifs if n.get("date") == today}

    for i, template in enumerate(selected):
        template_idx = MESSAGE_TEMPLATES.index(template)
        if template_idx in days_sent_today:
            continue
        days_sent_today.add(template_idx)

        msg = _fill_template(template, stats_data)
        module = _detect_module(template)
        notif = {
            "id": f"notif_{int(time.time())}_{i}",
            "text": msg,
            "module": module,
            "type": "recommendation",
            "time": time.time(),
            "date": today,
            "dismissed": False,
            "template_idx": template_idx,
        }
        new_notifs.append(notif)

    if new_notifs:
        notifs.extend(new_notifs)
        _save_notifications(notifs)

    return new_notifs

def _fill_template(template, data):
    fill = {
        "genre": data.get("top_genre") or "любимый жанр",
        "author": data.get("top_author") or "популярный автор",
        "artist": data.get("top_author") or "ваш исполнитель",
        "count": str(random.randint(3, 10)),
        "hours": str(data.get("total_hours", 5)),
        "total_hours": str(data.get("total_hours", 10)),
        "percent": str(random.randint(10, 95)),
        "genres_count": str(random.randint(2, 8)),
        "top_module": data.get("top_module", "книги"),
    }
    try:
        return template.format(**fill)
    except KeyError:
        swallow.note('notification_engine._fill_template:L159')
        return template

def _detect_module(template):
    if any(m in template for m in ["📚", "📖", "📕", "📗", "📘", "📙", "книг"]):
        return "books"
    if any(m in template for m in ["🎵", "🎶", "🎧", "музык", "трек", "исполнител"]):
        return "media"
    if any(m in template for m in ["📰", "📄", "📑", "стат", "ZIM"]):
        return "zim"
    return "recommendations"
