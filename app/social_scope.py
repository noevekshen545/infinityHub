import swallow
"""Область владения для альбомов и статей: группа или канал сервера.

Альбомы и статьи живут в одних и тех же таблицах, поэтому нужно уметь
проверять права для двух разных сущностей:

  * группа        — social_groups + social_group_members
  * канал сервера — social_server_channels + social_server_members

Раньше group_id в social_albums и social_articles был единственным
привязкой, и каналы сервера туда попасть не могли. Перегружать group_id
значением из другой таблицы нельзя: идентификаторы случайные, коллизия
теоретически возможна, а главное — потерялась бы возможность понять,
что за объект имеется в виду. Поэтому добавлена отдельная колонка
channel_id, и scope отвечает на вопрос «кто я и что мне можно» независимо
от того, где лежит объект.
"""


class Scope:
    """Разрешения пользователя в конкретной области (группа или канал)."""

    __slots__ = ("kind", "group_id", "channel_id", "server_id", "role")

    def __init__(self, kind, role, group_id="", channel_id="", server_id=""):
        self.kind = kind            # "group" | "channel"
        self.role = role
        self.group_id = group_id
        self.channel_id = channel_id
        self.server_id = server_id

    @property
    def is_manager(self):
        # В группе роль модератора тоже управляет содержимым, в сервере
        # роли называются иначе, но уровень тот же.
        if self.kind == "group":
            return self.role in ("owner", "admin", "moderator")
        return self.role in ("owner", "admin", "mod")

    @property
    def owner_col(self):
        """Колонка, по которой объект привязан к этой области."""
        return "group_id" if self.kind == "group" else "channel_id"

    @property
    def owner_id(self):
        return self.group_id if self.kind == "group" else self.channel_id

    @property
    def label(self):
        return "группы" if self.kind == "group" else "канала"


def group_scope(conn, group_id, profile_id):
    """Область группы. Бросает 403, если пользователь не участник."""
    row = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
        (group_id, profile_id)).fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(403, "Вы не участник группы")
    return Scope("group", row["role"], group_id=group_id)


def channel_scope(conn, server_id, channel_id, profile_id):
    """Область канала сервера.

    Проверяются два условия: пользователь состоит в сервере и имеет доступ
    к самому каналу. Приватный канал открыт только перечисленным в
    access_json участникам и администраторам сервера.
    """
    from fastapi import HTTPException
    role_row = conn.execute(
        "SELECT role FROM social_server_members WHERE server_id=? AND profile_id=?",
        (server_id, profile_id)).fetchone()
    if not role_row:
        raise HTTPException(403, "Вы не участник сервера")
    channel = conn.execute(
        "SELECT * FROM social_server_channels WHERE id=? AND server_id=?",
        (channel_id, server_id)).fetchone()
    if not channel:
        raise HTTPException(404, "Канал не найден")
    scope = Scope("channel", role_row["role"], channel_id=channel_id, server_id=server_id)
    if channel["is_private"] and not scope.is_manager:
        import json
        try:
            access = json.loads(channel["access_json"] or "[]")
        except ValueError:
            swallow.note('social_scope.channel_scope:L88')
            access = []
        if profile_id not in access:
            raise HTTPException(403, "Нет доступа к каналу")
    return scope


def album_filter(scope, extra=""):
    """Кусок SQL-условия, отбирающий альбомы этой области.

    Возвращает (фрагмент, параметры). Параметры нужно подставить в том же
    порядке, в каком фрагмент попал в запрос.
    """
    frag = f"{scope.owner_col}=?"
    params = [scope.owner_id]
    if extra:
        frag += " AND " + extra
    return frag, params


def article_filter(scope, extra=""):
    """Кусок SQL-условия для статей этой области."""
    return album_filter(scope, extra)