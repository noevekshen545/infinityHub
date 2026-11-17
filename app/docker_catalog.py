#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docker_catalog.py — каталог «Топ-100 контейнеров» для модуля Docker.

Почему статический каталог, а не «живой» топ с Docker Hub:
    у Hub нет публичного endpoint'а «top by pull count» (``/v2/repositories/
    ?ordering=pull_count`` отдаёт 405, пустой ``query`` — 0 результатов).
    Поэтому держим курируемый список из 100 самых популярных образов,
    а живые звёзды/качки (stars/pulls) догружает docker_module через
    ``/v2/repositories/<repo>/`` с кэшем (см. api_hub_top в docker_module).

Поля записи:
    id         — стабильный идентификатор (для установки/поиска на фронте)
    name       — человекочитаемое имя
    image      — полный ref образа (с тегом) для docker run
    repo       — путь репозитория на Docker Hub (library/nginx, jellyfin/jellyfin);
                 None — образ не на Hub (ghcr/quay) и не обогащается
    category   — категория для чипов фильтрации
    desc       — короткое описание (RU)
    icon       — slug на cdn.simpleicons.org (None → буквенный аватар на фронте)
    official   — Docker Official Image
    homepage   — сайт проекта (ссылка «↗» в карточке)
    ports      — пресет портов   "8080:80 8443:443"
    volumes    — пресет томов     "data:/path" через пробел
    envs       — пресет ENV       "K=V" через \\n
    restart    — политика рестарта
    cmd        — доп. аргументы командной строки (minio и т.п.)
    gpus       — "all" если образ любит GPU
    stars/pulls— офлайн-базelines (перетираются живыми данными при обогащении)

Функции:
    get_top100()      — весь каталог (сортировка по pulls)
    get(id)           — запись по id или None
    search(q)         — фильтр по имени/описанию/категории
    categories()      — список категорий с количеством
    validate_catalog()— проверка целостности (используется тестами)
"""

# Порядок списка — приоритет популярности (примерный, по pulls).
CATALOG = [
    # ── Веб ────────────────────────────────────────────────────────────────
    {"id": "nginx", "name": "Nginx", "image": "nginx:alpine", "repo": "library/nginx",
     "category": "Веб", "desc": "Самый популярный веб-сервер и reverse-proxy", "icon": "nginx",
     "official": True, "homepage": "https://nginx.org", "ports": "8080:80", "volumes": "",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 21400, "pulls": 13459000000},
    {"id": "httpd", "name": "Apache HTTPD", "image": "httpd:alpine", "repo": "library/httpd",
     "category": "Веб", "desc": "Классический Apache-веб-сервер", "icon": "apache",
     "official": True, "homepage": "https://httpd.apache.org", "ports": "8080:80", "volumes": "",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 4600, "pulls": 1600000000},
    {"id": "caddy", "name": "Caddy", "image": "caddy:alpine", "repo": "library/caddy",
     "category": "Веб", "desc": "Веб-сервер с авто-HTTPS и простым конфигом", "icon": "caddy",
     "official": True, "homepage": "https://caddyserver.com", "ports": "8080:80 8443:443",
     "volumes": "caddy_data:/data caddy_config:/config", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 5200, "pulls": 700000000},
    {"id": "traefik", "name": "Traefik", "image": "traefik:v3.3", "repo": "library/traefik",
     "category": "Веб", "desc": "Cloud-native reverse-proxy с авто-конфигом", "icon": "traefik",
     "official": True, "homepage": "https://traefik.io", "ports": "8080:80 8443:443 8081:8080",
     "volumes": "/var/run/docker.sock:/var/run/docker.sock", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 50000, "pulls": 3500000000},
    {"id": "haproxy", "name": "HAProxy", "image": "haproxy:alpine", "repo": "library/haproxy",
     "category": "Веб", "desc": "Высокопроизводительный TCP/HTTP балансировщик", "icon": "haproxy",
     "official": True, "homepage": "https://www.haproxy.org", "ports": "8080:80 8443:443",
     "volumes": "", "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 1400, "pulls": 900000000},
    {"id": "tomcat", "name": "Apache Tomcat", "image": "tomcat:11-jre21-temurin-noble",
     "repo": "library/tomcat", "category": "Веб", "desc": "Java servlet-контейнер (Java EE)",
     "icon": "apachetomcat", "official": True, "homepage": "https://tomcat.apache.org",
     "ports": "8080:8080", "volumes": "", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 3000, "pulls": 1200000000},

    # ── Базы данных ───────────────────────────────────────────────────────
    {"id": "postgres", "name": "PostgreSQL", "image": "postgres:17-alpine",
     "repo": "library/postgres", "category": "Базы данных",
     "desc": "Надёжная реляционная БД (лучшая в своём классе)",
     "icon": "postgresql", "official": True, "homepage": "https://www.postgresql.org",
     "ports": "5432:5432", "volumes": "pg_data:/var/lib/postgresql/data",
     "envs": "POSTGRES_PASSWORD=changeme", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 15400, "pulls": 8300000000},
    {"id": "mysql", "name": "MySQL", "image": "mysql:9", "repo": "library/mysql",
     "category": "Базы данных", "desc": "Мир-популярная реляционная БД", "icon": "mysql",
     "official": True, "homepage": "https://www.mysql.com", "ports": "3306:3306",
     "volumes": "mysql_data:/var/lib/mysql", "envs": "MYSQL_ROOT_PASSWORD=changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 11600, "pulls": 6900000000},
    {"id": "mariadb", "name": "MariaDB", "image": "mariadb:11", "repo": "library/mariadb",
     "category": "Базы данных", "desc": "Форк MySQL с открытым кодом", "icon": "mariadb",
     "official": True, "homepage": "https://mariadb.org", "ports": "3306:3307",
     "volumes": "maria_data:/var/lib/mysql", "envs": "MARIADB_ROOT_PASSWORD=changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 6200, "pulls": 4200000000},
    {"id": "mongo", "name": "MongoDB", "image": "mongo:8", "repo": "library/mongo",
     "category": "Базы данных", "desc": "Популярная document/NoSQL база", "icon": "mongodb",
     "official": True, "homepage": "https://www.mongodb.com", "ports": "27017:27017",
     "volumes": "mongo_data:/data/db", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 9800, "pulls": 5600000000},
    {"id": "redis", "name": "Redis", "image": "redis:alpine", "repo": "library/redis",
     "category": "Кэш", "desc": "Самая быстрая in-memory БД и кэш", "icon": "redis",
     "official": True, "homepage": "https://redis.io", "ports": "6379:6379",
     "volumes": "redis_data:/data", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 13600, "pulls": 11400000000},
    {"id": "memcached", "name": "Memcached", "image": "memcached:alpine",
     "repo": "library/memcached", "category": "Кэш", "desc": "Лёгкий распределённый кэш",
     "icon": "memcached", "official": True, "homepage": "https://memcached.org",
     "ports": "11211:11211", "volumes": "", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 1100, "pulls": 1700000000},
    {"id": "influxdb", "name": "InfluxDB", "image": "influxdb:1.11", "repo": "library/influxdb",
     "category": "Базы данных", "desc": "Time-series БД для метрик", "icon": "influxdb",
     "official": True, "homepage": "https://www.influxdata.com", "ports": "8086:8086",
     "volumes": "influx_data:/var/lib/influxdb", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 28000, "pulls": 1600000000},
    {"id": "elasticsearch", "name": "Elasticsearch", "image": "elasticsearch:8.17.0",
     "repo": "library/elasticsearch", "category": "Базы данных",
     "desc": "Поиск и аналитика (ELK-стек)", "icon": "elasticsearch", "official": True,
     "homepage": "https://www.elastic.co", "ports": "9200:9200 9300:9300",
     "volumes": "es_data:/usr/share/elasticsearch/data",
     "envs": "discovery.type=single-node\nxpack.security.enabled=false\nES_JAVA_OPTS=-Xms512m -Xmx512m",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 24000, "pulls": 4400000000},
    {"id": "neo4j", "name": "Neo4j", "image": "neo4j:5", "repo": "library/neo4j",
     "category": "Базы данных", "desc": "Графовая база данных", "icon": "neo4j",
     "official": True, "homepage": "https://neo4j.com", "ports": "7474:7474 7687:7687",
     "volumes": "neo4j_data:/data", "envs": "NEO4J_AUTH=neo4j/changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 13400, "pulls": 300000000},

    # ── Хранилище ─────────────────────────────────────────────────────────
    {"id": "minio", "name": "MinIO", "image": "minio/minio:latest", "repo": "minio/minio",
     "category": "Хранилище", "desc": "S3-совместимое объектное хранилище", "icon": "minio",
     "official": False, "homepage": "https://min.io", "ports": "9000:9000 9001:9001",
     "volumes": "minio_data:/data", "envs": "MINIO_ROOT_USER=admin\nMINIO_ROOT_PASSWORD=changeme123",
     "restart": "unless-stopped", "cmd": "server /data --console-address ':9001'",
     "gpus": "", "stars": 27000, "pulls": 1400000000},
    {"id": "nextcloud", "name": "Nextcloud", "image": "nextcloud:stable", "repo": "library/nextcloud",
     "category": "Хранилище", "desc": "Своё облако: файлы, календари, фото", "icon": "nextcloud",
     "official": True, "homepage": "https://nextcloud.com", "ports": "8080:80",
     "volumes": "nc_data:/var/www/html", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 29000, "pulls": 1100000000},
    {"id": "syncthing", "name": "Syncthing", "image": "syncthing/syncthing:latest",
     "repo": "syncthing/syncthing", "category": "Хранилище",
     "desc": "P2P-синхронизация файлов между устройствами", "icon": "syncthing",
     "official": False, "homepage": "https://syncthing.net",
     "ports": "8384:8384 22000:22000/tcp 21027:21027/udp",
     "volumes": "st_config:/var/syncthing st_data:/var/syncthing/data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 7200, "pulls": 800000000},
    {"id": "filebrowser", "name": "Filebrowser", "image": "filebrowser/filebrowser:latest",
     "repo": "filebrowser/filebrowser", "category": "Хранилище",
     "desc": "Веб-менеджер файлов", "icon": "cloudflare", "official": False,
     "homepage": "https://filebrowser.org", "ports": "8080:80",
     "volumes": "/:/srv", "envs": "", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 25000, "pulls": 700000000},

    # ── Медиа и загрузки ──────────────────────────────────────────────────
    {"id": "jellyfin", "name": "Jellyfin", "image": "jellyfin/jellyfin:latest",
     "repo": "jellyfin/jellyfin", "category": "Медиа", "desc": "Медиа-сервер (open-source альтернатива Plex)",
     "icon": "jellyfin", "official": False, "homepage": "https://jellyfin.org",
     "ports": "8096:8096 8920:8920", "volumes": "jf_config:/config jf_cache:/cache",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 36000, "pulls": 800000000},
    {"id": "immich", "name": "Immich", "image": "ghcr.io/immich-app/immich-server:release",
     "repo": None, "category": "Медиа", "desc": "Self-hosted замена Google Фото",
     "icon": "immich", "official": False, "homepage": "https://immich.app",
     "ports": "2283:2283", "volumes": "immich_upload:/usr/src/app/upload",
     "envs": "TZ=Europe/Moscow", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 80000, "pulls": 300000000},
    {"id": "navidrome", "name": "Navidrome", "image": "deluan/navidrome:latest",
     "repo": "deluan/navidrome", "category": "Медиа", "desc": "Музыкальный стриминг-сервер для своей коллекции",
     "icon": "navidrome", "official": False, "homepage": "https://www.navidrome.org",
     "ports": "4533:4533", "volumes": "nd_data:/data nd_music:/music:ro", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 13000, "pulls": 250000000},
    {"id": "qbittorrent", "name": "qBittorrent", "image": "lscr.io/linuxserver/qbittorrent:latest",
     "repo": None, "category": "Загрузки", "desc": "Веб-интерфейс торрент-клиента",
     "icon": "qbittorrent", "official": False, "homepage": "https://www.qbittorrent.org",
     "ports": "8080:8080 6881:6881", "volumes": "qb_config:/config qb_downloads:/downloads",
     "envs": "PUID=1000\nPGID=1000", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 3200, "pulls": 900000000},
    {"id": "transmission", "name": "Transmission", "image": "linuxserver/transmission:latest",
     "repo": None, "category": "Загрузки", "desc": "Лёгкий торрент-клиент с веб-UI",
     "icon": "transmission", "official": False, "homepage": "https://transmissionbt.com",
     "ports": "9091:9091 51413:51413", "volumes": "tr_config:/config tr_downloads:/downloads",
     "envs": "PUID=1000\nPGID=1000", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 1800, "pulls": 1000000000},

    # ── Мониторинг ────────────────────────────────────────────────────────
    {"id": "grafana", "name": "Grafana", "image": "grafana/grafana:latest",
     "repo": "grafana/grafana", "category": "Мониторинг",
     "desc": "Красивые дашборды и алерты для метрик", "icon": "grafana",
     "official": False, "homepage": "https://grafana.com", "ports": "3000:3000",
     "volumes": "grafana_data:/var/lib/grafana", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 67000, "pulls": 3000000000},
    {"id": "prometheus", "name": "Prometheus", "image": "prom/prometheus:latest",
     "repo": "prom/prometheus", "category": "Мониторинг",
     "desc": "Сбор и хранение метрик (TSDB)", "icon": "prometheus", "official": False,
     "homepage": "https://prometheus.io", "ports": "9090:9090",
     "volumes": "prom_data:/prometheus", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 56000, "pulls": 1800000000},
    {"id": "uptime-kuma", "name": "Uptime Kuma", "image": "louislam/uptime-kuma:1",
     "repo": "louislam/uptime-kuma", "category": "Мониторинг",
     "desc": "Мониторинг доступности сайтов и сервисов", "icon": "uptimekuma",
     "official": False, "homepage": "https://uptime.kuma.pet", "ports": "3001:3001",
     "volumes": "kuma_data:/app/data", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 68000, "pulls": 700000000},
    {"id": "netdata", "name": "Netdata", "image": "netdata/netdata:latest",
     "repo": "netdata/netdata", "category": "Мониторинг",
     "desc": "Мониторинг системы в реальном времени", "icon": "netdata",
     "official": False, "homepage": "https://www.netdata.cloud", "ports": "19999:19999",
     "volumes": "netdata_config:/etc/netdata", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 7400, "pulls": 700000000},
    {"id": "dozzle", "name": "Dozzle", "image": "amir20/dozzle:latest",
     "repo": "amir20/dozzle", "category": "Мониторинг",
     "desc": "Живой просмотр логов контейнеров в браузере", "icon": "docker",
     "official": False, "homepage": "https://dozzle.dev", "ports": "8080:8080",
     "volumes": "/var/run/docker.sock:/var/run/docker.sock", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 6800, "pulls": 500000000},

    # ── Сеть и безопасность ───────────────────────────────────────────────
    {"id": "pihole", "name": "Pi-hole", "image": "pihole/pihole:2025.03.0",
     "repo": "pihole/pihole", "category": "Сеть",
     "desc": "DNS-блокировщик рекламы для всей сети", "icon": "pihole",
     "official": False, "homepage": "https://pi-hole.net", "ports": "8082:80 53:53/tcp 53:53/udp",
     "volumes": "pihole_etc:/etc/pihole", "envs": "TZ=Europe/Moscow\nWEBPASSWORD=changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 25000, "pulls": 700000000},
    {"id": "adguard", "name": "AdGuard Home", "image": "adguard/adguardhome:latest",
     "repo": "AdGuardTeam/AdGuardHome", "category": "Сеть",
     "desc": "Сетевой ad-block и DNS-шифрование", "icon": "adguard",
     "official": False, "homepage": "https://adguard.com", "ports": "3000:3000 53:53/tcp 53:53/udp",
     "volumes": "adh_work:/opt/adguardhome/work", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 27000, "pulls": 500000000},
    {"id": "vaultwarden", "name": "Vaultwarden", "image": "vaultwarden/server:latest",
     "repo": "vaultwarden/server", "category": "Безопасность",
     "desc": "Лёгкий менеджер паролей (Bitwarden-совместимый)", "icon": "bitwarden",
     "official": False, "homepage": "https://github.com/dani-garcia/vaultwarden",
     "ports": "8080:80", "volumes": "vw_data:/data",
     "envs": "SIGNUPS_ALLOWED=false\nADMIN_TOKEN=changeme", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 43000, "pulls": 600000000},
    {"id": "wireguard", "name": "WireGuard VPN", "image": "linuxserver/wireguard:latest",
     "repo": None, "category": "Сеть", "desc": "VPN-сервер (WireGuard)", "icon": "wireguard",
     "official": False, "homepage": "https://www.wireguard.com",
     "ports": "51820:51820/udp", "volumes": "wg_config:/config",
     "envs": "SERVERURL=localhost\nPEERS=5", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 2500, "pulls": 600000000},
    {"id": "nginx-proxy-manager", "name": "Nginx Proxy Manager",
     "image": "jc21/nginx-proxy-manager:latest", "repo": "jc21/nginx-proxy-manager",
     "category": "Сеть", "desc": "Reverse-proxy с простым веб-UI и SSL", "icon": "nginx",
     "official": False, "homepage": "https://nginxproxymanager.com",
     "ports": "80:80 443:443 81:81", "volumes": "npm_data:/data npm_letsencrypt:/etc/letsencrypt",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 24000, "pulls": 700000000},

    # ── Управление Docker и DevOps ────────────────────────────────────────
    {"id": "portainer", "name": "Portainer CE", "image": "portainer/portainer-ce:latest",
     "repo": "portainer/portainer-ce", "category": "DevOps",
     "desc": "Веб-панель управления Docker", "icon": "portainer",
     "official": False, "homepage": "https://www.portainer.io", "ports": "9443:9443 9000:9000",
     "volumes": "/var/run/docker.sock:/var/run/docker.sock portainer_data:/data",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 30000, "pulls": 3200000000},
    {"id": "watchtower", "name": "Watchtower", "image": "containrrr/watchtower:latest",
     "repo": "containrrr/watchtower", "category": "DevOps",
     "desc": "Автообновление контейнеров до новых образов", "icon": "docker",
     "official": False, "homepage": "https://containrrr.dev/watchtower",
     "ports": "", "volumes": "/var/run/docker.sock:/var/run/docker.sock",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 19000, "pulls": 1100000000},
    {"id": "gitea", "name": "Gitea", "image": "gitea/gitea:1",
     "repo": "gitea/gitea", "category": "DevOps",
     "desc": "Лёгкий self-hosted Git с веб-интерфейсом", "icon": "gitea",
     "official": False, "homepage": "https://gitea.io", "ports": "3000:3000 2222:22",
     "volumes": "gitea_data:/data gitea_config:/etc/gitea", "envs": "USER_UID=1000\nUSER_GID=1000",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 46000, "pulls": 700000000},
    {"id": "jenkins", "name": "Jenkins", "image": "jenkins/jenkins:lts-jdk17",
     "repo": "library/jenkins", "category": "DevOps", "desc": "CI/CD-сервер классика",
     "icon": "jenkins", "official": True, "homepage": "https://www.jenkins.io",
     "ports": "8080:8080 50000:50000", "volumes": "jenkins_home:/var/jenkins_home",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 22000, "pulls": 1400000000},
    {"id": "drone", "name": "Drone CI", "image": "drone/drone:latest",
     "repo": "drone/drone", "category": "DevOps", "desc": "Лёгкий облачный CI/CD",
     "icon": "drone", "official": False, "homepage": "https://www.drone.io",
     "ports": "8080:80", "volumes": "drone_data:/data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 29000, "pulls": 300000000},

    # ── Приложения и CMS ──────────────────────────────────────────────────
    {"id": "wordpress", "name": "WordPress", "image": "wordpress:php8.3-apache",
     "repo": "library/wordpress", "category": "Приложения",
     "desc": "Самая популярная CMS в мире", "icon": "wordpress",
     "official": True, "homepage": "https://wordpress.org", "ports": "8080:80",
     "volumes": "wp_data:/var/www/html", "envs": "WORDPRESS_DB_HOST=db\nWORDPRESS_DB_USER=root\nWORDPRESS_DB_PASSWORD=changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 5600, "pulls": 2600000000},
    {"id": "ghost", "name": "Ghost", "image": "ghost:5-alpine", "repo": "library/ghost",
     "category": "Приложения", "desc": "Современная платформа для блогов", "icon": "ghost",
     "official": True, "homepage": "https://ghost.org", "ports": "2368:2368",
     "volumes": "ghost_content:/var/lib/ghost/content",
     "envs": "NODE_ENV=production\nurl=http://localhost:2368\ndatabase__client=sqlite3",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 47000, "pulls": 800000000},
    {"id": "wikijs", "name": "Wiki.js", "image": "ghcr.io/requarks/wiki:2",
     "repo": None, "category": "Приложения", "desc": "Красивая wiki на Node.js",
     "icon": "wikipedia", "official": False, "homepage": "https://js.wiki",
     "ports": "3000:3000", "volumes": "wiki_data:/wiki", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 24000, "pulls": 200000000},
    {"id": "outline", "name": "Outline", "image": "outlinewiki/outline:latest",
     "repo": "outlinewiki/outline", "category": "Приложения",
     "desc": "Красивая командная база знаний", "icon": "notion", "official": False,
     "homepage": "https://www.getoutline.com", "ports": "3000:3000",
     "volumes": "outline_data:/var/lib/outline",
     "envs": "NODE_ENV=production\nSECRET_KEY=changeme\nUTIL_SECRET=changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 31000, "pulls": 150000000},
    {"id": "bookstack", "name": "BookStack", "image": "lscr.io/linuxserver/bookstack:latest",
     "repo": None, "category": "Приложения", "desc": "Документация и книги в wiki-стиле",
     "icon": "wikipedia", "official": False, "homepage": "https://www.bookstackapp.com",
     "ports": "8080:80", "volumes": "bookstack_data:/config",
     "envs": "PUID=1000\nPGID=1000\nDB_HOST=db", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 4200, "pulls": 150000000},
    {"id": "phpmyadmin", "name": "phpMyAdmin", "image": "phpmyadmin:5",
     "repo": "library/phpmyadmin", "category": "Базы данных",
     "desc": "Веб-интерфейс для MySQL/MariaDB", "icon": "mysql", "official": True,
     "homepage": "https://www.phpmyadmin.net", "ports": "8081:80",
     "volumes": "", "envs": "PMA_HOST=db", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 1200, "pulls": 1200000000},
    {"id": "adminer", "name": "Adminer", "image": "adminer:4", "repo": "library/adminer",
     "category": "Базы данных", "desc": "Компактный веб-менеджер БД (все СУБД)",
     "icon": "postgresql", "official": True, "homepage": "https://www.adminer.org",
     "ports": "8081:8080", "volumes": "", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 5600, "pulls": 800000000},
    {"id": "rabbitmq", "name": "RabbitMQ", "image": "rabbitmq:4-management",
     "repo": "library/rabbitmq", "category": "Сообщения",
     "desc": "Очереди сообщений с management-UI", "icon": "rabbitmq",
     "official": True, "homepage": "https://www.rabbitmq.com", "ports": "5672:5672 15672:15672",
     "volumes": "rabbit_data:/var/lib/rabbitmq", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 12400, "pulls": 1900000000},
    {"id": "nats", "name": "NATS", "image": "nats:2-alpine", "repo": "library/nats",
     "category": "Сообщения", "desc": "Сверхлёгкая messaging-система", "icon": "nats",
     "official": True, "homepage": "https://nats.io", "ports": "4222:4222 8222:8222",
     "volumes": "", "envs": "", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 16000, "pulls": 700000000},
    {"id": "kafka", "name": "Apache Kafka", "image": "bitnami/kafka:3.9",
     "repo": "bitnami/kafka", "category": "Сообщения",
     "desc": "Распределённый потоковый брокер", "icon": "apachekafka",
     "official": False, "homepage": "https://kafka.apache.org", "ports": "9092:9092",
     "volumes": "kafka_data:/bitnami/kafka", "envs": "KAFKA_CFG_NODE_ID=0\nKAFKA_CFG_PROCESS_ROLES=controller,broker",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 7400, "pulls": 1500000000},

    # ── AI и автоматизация ────────────────────────────────────────────────
    {"id": "ollama", "name": "Ollama", "image": "ollama/ollama:latest",
     "repo": "ollama/ollama", "category": "AI",
     "desc": "Локальные LLM (Llama, Qwen, Gemma) с GPU", "icon": "ollama",
     "official": False, "homepage": "https://ollama.com", "ports": "11434:11434",
     "volumes": "ollama_data:/root/.ollama", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "all", "stars": 130000, "pulls": 900000000},
    {"id": "open-webui", "name": "Open WebUI", "image": "ghcr.io/open-webui/open-webui:main",
     "repo": None, "category": "AI", "desc": "Красивый веб-интерфейс для local LLM",
     "icon": "openai", "official": False, "homepage": "https://openwebui.com",
     "ports": "3000:8080", "volumes": "openwebui_data:/app/backend/data",
     "envs": "OLLAMA_BASE_URL=http://host.docker.internal:11434",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 100000, "pulls": 400000000},
    {"id": "libretranslate", "name": "LibreTranslate", "image": "libretranslate/libretranslate:latest",
     "repo": "LibreTranslate/LibreTranslate", "category": "AI",
     "desc": "Своя машина перевода (open-source)", "icon": "deepl", "official": False,
     "homepage": "https://libretranslate.com", "ports": "5000:5000",
     "volumes": "lt_data:/usr/local/libretranslate", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 13000, "pulls": 100000000},
    {"id": "n8n", "name": "n8n", "image": "n8nio/n8n:latest", "repo": "n8nio/n8n",
     "category": "Автоматизация", "desc": "Визуальная автоматизация и скрипты (Zapier-подобный)",
     "icon": "n8n", "official": False, "homepage": "https://n8n.io", "ports": "5678:5678",
     "volumes": "n8n_data:/home/node/.n8n",
     "envs": "GENERIC_TIMEZONE=Europe/Moscow\nN8N_SECURE_COOKIE=false",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 60000, "pulls": 400000000},
    {"id": "activepieces", "name": "Activepieces", "image": "activepieces/activepieces:latest",
     "repo": "activepieces/activepieces", "category": "Автоматизация",
     "desc": "Open-source автоматизация процессов", "icon": "zapier", "official": False,
     "homepage": "https://www.activepieces.com", "ports": "8080:80",
     "volumes": "ap_data:/assets", "envs": "AP_FRONTEND_URL=http://localhost:8080\nAP_DB_URL=postgres://db/ap",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 17000, "pulls": 50000000},

    # ── Умный дом ─────────────────────────────────────────────────────────
    {"id": "homeassistant", "name": "Home Assistant", "image": "ghcr.io/home-assistant/home-assistant:stable",
     "repo": None, "category": "Умный дом", "desc": "Платформа умного дома с огромным сообществом",
     "icon": "homeassistant", "official": False, "homepage": "https://www.home-assistant.io",
     "ports": "8123:8123", "volumes": "ha_config:/config", "envs": "TZ=Europe/Moscow",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 76000, "pulls": 700000000},
    {"id": "node-red", "name": "Node-RED", "image": "nodered/node-red:latest",
     "repo": "node-red/node-red", "category": "Умный дом",
     "desc": "Визуальное программирование потоков и IoT", "icon": "nodered",
     "official": False, "homepage": "https://nodered.org", "ports": "1880:1880",
     "volumes": "nodered_data:/data", "envs": "TZ=Europe/Moscow",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 20000, "pulls": 400000000},
    {"id": "mosquitto", "name": "Eclipse Mosquitto", "image": "eclipse-mosquitto:2",
     "repo": "library/eclipse-mosquitto", "category": "Умный дом",
     "desc": "MQTT-брокер для IoT-устройств", "icon": "eclipse", "official": True,
     "homepage": "https://mosquitto.org", "ports": "1883:1883 9001:9001",
     "volumes": "mosquitto_data:/mosquitto/data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 9400, "pulls": 900000000},
    {"id": "zigbee2mqtt", "name": "Zigbee2MQTT", "image": "koenkk/zigbee2mqtt:latest",
     "repo": "koenkk/zigbee2mqtt", "category": "Умный дом",
     "desc": "Zigbee-устройства через MQTT (без хабов)", "icon": "zigbee",
     "official": False, "homepage": "https://www.zigbee2mqtt.io", "ports": "8080:8080",
     "volumes": "z2m_data:/app/data", "envs": "TZ=Europe/Moscow",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 12000, "pulls": 200000000},

    # ── Коммуникации ──────────────────────────────────────────────────────
    {"id": "rocket-chat", "name": "Rocket.Chat", "image": "rocket.chat:7",
     "repo": "rocket.chat", "category": "Коммуникации",
     "desc": "Self-hosted чат команды (Slack-подобный)", "icon": "rocketdotchat",
     "official": False, "homepage": "https://rocket.chat", "ports": "3000:3000",
     "volumes": "rocketchat_uploads:/app/uploads",
     "envs": "MONGO_URL=mongodb://db:27017/rocketchat\nROOT_URL=http://localhost:3000",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 42000, "pulls": 400000000},
    {"id": "mattermost", "name": "Mattermost", "image": "mattermost/mattermost-team-edition:latest",
     "repo": None, "category": "Коммуникации",
     "desc": "Открытая корпоративная коммуникация", "icon": "mattermost",
     "official": False, "homepage": "https://mattermost.com", "ports": "8065:8065",
     "volumes": "mm_data:/mattermost/data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 11000, "pulls": 300000000},
    {"id": "synapse", "name": "Synapse (Matrix)", "image": "elementhq/synapse:latest",
     "repo": None, "category": "Коммуникации",
     "desc": "Децентрализованный мессенджер Matrix", "icon": "matrix",
     "official": False, "homepage": "https://element.io", "ports": "8008:8008",
     "volumes": "synapse_data:/data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 10000, "pulls": 200000000},

    # ── Продуктивность ────────────────────────────────────────────────────
    {"id": "vikunja", "name": "Vikunja", "image": "vikunja/vikunja:latest",
     "repo": "kolaente/vikunja", "category": "Продуктивность",
     "desc": "Задачи, проекты и календарь (Todoist-подобный)", "icon": "todoist",
     "official": False, "homepage": "https://vikunja.io", "ports": "3456:3456",
     "volumes": "vikunja_files:/app/vikunja/files", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 3600, "pulls": 60000000},
    {"id": "actual-budget", "name": "Actual Budget", "image": "actualbudget/actual-server:latest",
     "repo": "actualbudget/actual-server", "category": "Продуктивность",
     "desc": "Личный бюджет с синхронизацией", "icon": None, "official": False,
     "homepage": "https://actualbudget.com", "ports": "5006:5006",
     "volumes": "actual_data:/data", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 19000, "pulls": 50000000},
    {"id": "stirling-pdf", "name": "Stirling PDF", "image": "frooodle/s:latest",
     "repo": "Stirling-Tools/S", "category": "Продуктивность",
     "desc": "Все операции с PDF в браузере", "icon": "adobeacrobatreader",
     "official": False, "homepage": "https://stirlingpdf.com", "ports": "8080:8080",
     "volumes": "stirling_data:/usr/share/tessdata", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 60000, "pulls": 300000000},
    {"id": "onlyoffice", "name": "ONLYOFFICE Docs", "image": "onlyoffice/documentserver:latest",
     "repo": None, "category": "Продуктивность",
     "desc": "Онлайн-офис (Word/Excel/Slides) для своего сервера", "icon": "onlyoffice",
     "official": False, "homepage": "https://www.onlyoffice.com", "ports": "8080:80",
     "volumes": "oo_data:/var/www/onlyoffice/Data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 2400, "pulls": 100000000},
    {"id": "paperless-ngx", "name": "Paperless-ngx", "image": "ghcr.io/paperless-ngx/paperless-ngx:latest",
     "repo": None, "category": "Продуктивность",
     "desc": "Оцифровка и поиск по документам", "icon": "paperlessngx",
     "official": False, "homepage": "https://docs.paperless-ngx.com",
     "ports": "8000:8000", "volumes": "pl_data:/usr/src/paperless/data",
     "envs": "PAPERLESS_TIME_ZONE=Europe/Moscow",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 27000, "pulls": 150000000},
    {"id": "memos", "name": "Memos", "image": "ghcr.io/usememos/memos:latest",
     "repo": None, "category": "Продуктивность", "desc": "Лёгкие заметки и мысли (облегчённый Notion)",
     "icon": "notion", "official": False, "homepage": "https://www.usememos.com",
     "ports": "5230:5230", "volumes": "memos_data:/var/opt/memos",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 38000, "pulls": 150000000},

    # ── Аналитика ─────────────────────────────────────────────────────────
    {"id": "umami", "name": "Umami", "image": "ghcr.io/umami-software/umami:postgresql-latest",
     "repo": None, "category": "Аналитика", "desc": "Приватная веб-аналитика (легко Google Analytics)",
     "icon": "googleanalytics", "official": False, "homepage": "https://umami.is",
     "ports": "3000:3000", "volumes": "", "envs": "APP_URL=http://localhost:3000\nDATABASE_URL=postgres://db/umami",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 27000, "pulls": 100000000},
    {"id": "matomo", "name": "Matomo", "image": "matomo:latest", "repo": "library/matomo",
     "category": "Аналитика", "desc": "Полноценная веб-аналитика (Piwik)", "icon": "matomo",
     "official": True, "homepage": "https://matomo.org", "ports": "8080:80",
     "volumes": "matomo_data:/var/www/html", "envs": "MATOMO_DB_HOST=db",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 2000, "pulls": 200000000},
    {"id": "changedetection", "name": "changedetection.io", "image": "ghcr.io/dgtlmoon/changedetection.io:latest",
     "repo": None, "category": "Мониторинг", "desc": "Уведомления об изменениях на сайтах",
     "icon": None, "official": False, "homepage": "https://changedetection.io",
     "ports": "5000:5000", "volumes": "cd_data:/datastore", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 23000, "pulls": 100000000},

    # ── Дашборды и утилиты ────────────────────────────────────────────────
    {"id": "homepage", "name": "Homepage", "image": "gethomepage/homepage:latest",
     "repo": "gethomepage/homepage", "category": "Утилиты",
     "desc": "Красивая домашняя страница-дашборд сервисов", "icon": None,
     "official": False, "homepage": "https://gethomepage.dev", "ports": "3000:3000",
     "volumes": "hp_config:/app/config", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 23000, "pulls": 150000000},
    {"id": "homarr", "name": "Homarr", "image": "ghcr.io/homarr-labs/homarr:latest",
     "repo": None, "category": "Утилиты", "desc": "Минималистичный дашборд приложений",
     "icon": None, "official": False, "homepage": "https://homarr.dev",
     "ports": "7575:7575", "volumes": "homarr_data:/app/data", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 25000, "pulls": 150000000},
    {"id": "it-tools", "name": "IT Tools", "image": "corentinth/it-tools:latest",
     "repo": "corentinth/it-tools", "category": "Утилиты",
     "desc": "Набор IT-инструментов в одном веб-приложении", "icon": None,
     "official": False, "homepage": "https://it-tools.tech", "ports": "8080:80",
     "volumes": "", "envs": "", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 27000, "pulls": 100000000},
    {"id": "beszel", "name": "Beszel", "image": "henrygd/beszel:latest",
     "repo": "henrygd/beszel", "category": "Мониторинг",
     "desc": "Сверхлёгкий мониторинг серверов и контейнеров", "icon": None,
     "official": False, "homepage": "https://beszel.dev", "ports": "8090:8090",
     "volumes": "beszel_data:/beszel_data", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 15000, "pulls": 50000000},
    {"id": "glances", "name": "Glances", "image": "nicolaka/glances:latest",
     "repo": "nicolaka/glances", "category": "Мониторинг",
     "desc": "Кроссплатформенный мониторинг системы", "icon": None,
     "official": False, "homepage": "https://nicolargo.github.io/glances",
     "ports": "61208:61208", "volumes": "",
     "envs": "GLANCES_OPT=-w", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 24000, "pulls": 500000000},
    {"id": "speedtest-tracker", "name": "Speedtest Tracker", "image": "henrygd/speedtest-tracker:latest",
     "repo": "henrygd/speedtest-tracker", "category": "Мониторинг",
     "desc": "История скорости интернета с графиками", "icon": None,
     "official": False, "homepage": "https://github.com/henrygd/speedtest-tracker",
     "ports": "8080:80", "volumes": "speedtest_data:/config", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 8000, "pulls": 50000000},

    # ── Ещё базы данных и кэш ─────────────────────────────────────────────
    {"id": "clickhouse", "name": "ClickHouse", "image": "clickhouse/clickhouse-server:25.3",
     "repo": "clickhouse/clickhouse-server", "category": "Базы данных",
     "desc": "Сверхбыстрая аналитическая БД (OLAP)", "icon": "clickhouse",
     "official": False, "homepage": "https://clickhouse.com", "ports": "8123:8123 9000:9009",
     "volumes": "ch_data:/var/lib/clickhouse", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 37000, "pulls": 900000000},
    {"id": "valkey", "name": "Valkey", "image": "valkey/valkey:8-alpine",
     "repo": "valkey/valkey", "category": "Кэш",
     "desc": "Open-source форк Redis 7.2 (Linux Foundation)", "icon": "valkey",
     "official": False, "homepage": "https://valkey.io", "ports": "6379:6379",
     "volumes": "valkey_data:/data", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 12000, "pulls": 300000000},
    {"id": "couchdb", "name": "CouchDB", "image": "couchdb:3", "repo": "library/couchdb",
     "category": "Базы данных", "desc": "Документная БД с офлайн-синхронизацией",
     "icon": "couchdb", "official": True, "homepage": "https://couchdb.apache.org",
     "ports": "5984:5984", "volumes": "couch_data:/opt/couchdb/data",
     "envs": "COUCHDB_USER=admin\nCOUCHDB_PASSWORD=changeme", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 1200, "pulls": 400000000},

    # ── Git, реестры, IDE ─────────────────────────────────────────────────
    {"id": "forgejo", "name": "Forgejo", "image": "codeberg.org/forgejo/forgejo:1",
     "repo": None, "category": "DevOps",
     "desc": "Лёгкий self-hosted Git (форк Gitea)", "icon": "forgejo",
     "official": False, "homepage": "https://forgejo.org", "ports": "3000:3000 222:22",
     "volumes": "forgejo_data:/data", "envs": "USER_UID=1000\nUSER_GID=1000",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 14000, "pulls": 150000000},
    {"id": "registry", "name": "Docker Registry", "image": "registry:2",
     "repo": "library/registry", "category": "DevOps",
     "desc": "Свой приватный Docker-реестр образов", "icon": "docker",
     "official": True, "homepage": "https://docs.docker.com/registry/",
     "ports": "5000:5000", "volumes": "registry_data:/var/lib/registry",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 4200, "pulls": 3000000000},
    {"id": "code-server", "name": "code-server", "image": "lscr.io/linuxserver/code-server:latest",
     "repo": None, "category": "Продуктивность",
     "desc": "VS Code в браузере со своего сервера", "icon": "visualstudiocode",
     "official": False, "homepage": "https://github.com/coder/code-server",
     "ports": "8443:8443", "volumes": "cs_config:/config", "envs": "PUID=1000\nPGID=1000",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 23000, "pulls": 500000000},

    # ── Медиа-автоматизация и книги ───────────────────────────────────────
    {"id": "plex", "name": "Plex Media Server", "image": "plexinc/pms-docker:latest",
     "repo": "plexinc/pms-docker", "category": "Медиа", "desc": "Популярный медиа-сервер",
     "icon": "plex", "official": False, "homepage": "https://www.plex.tv",
     "ports": "32400:32400", "volumes": "plex_config:/config plex_media:/data",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 4600, "pulls": 700000000},
    {"id": "sonarr", "name": "Sonarr", "image": "lscr.io/linuxserver/sonarr:latest",
     "repo": None, "category": "Медиа", "desc": "Авто-загрузка сериалов (PVR)", "icon": "sonarr",
     "official": False, "homepage": "https://sonarr.tv", "ports": "8989:8989",
     "volumes": "sonarr_config:/config sonarr_tv:/tv", "envs": "PUID=1000\nPGID=1000",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 9500, "pulls": 500000000},
    {"id": "radarr", "name": "Radarr", "image": "lscr.io/linuxserver/radarr:latest",
     "repo": None, "category": "Медиа", "desc": "Авто-загрузка фильмов (PVR)", "icon": "radarr",
     "official": False, "homepage": "https://radarr.video", "ports": "7878:7878",
     "volumes": "radarr_config:/config radarr_movies:/movies", "envs": "PUID=1000\nPGID=1000",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 5300, "pulls": 400000000},
    {"id": "jellyseerr", "name": "Jellyseerr", "image": "fallenbagel/jellyseerr:latest",
     "repo": "fallenbagel/jellyseerr", "category": "Медиа",
     "desc": "Запросы медиа для Jellyfin (аналог Overseerr)", "icon": None,
     "official": False, "homepage": "https://docs.jellyseerr.dev", "ports": "5055:5055",
     "volumes": "jserr_data:/app/config", "envs": "", "restart": "unless-stopped",
     "cmd": "", "gpus": "", "stars": 5000, "pulls": 100000000},
    {"id": "calibre-web", "name": "Calibre-Web", "image": "lscr.io/linuxserver/calibre-web:latest",
     "repo": None, "category": "Медиа", "desc": "Веб-библиотека книг (ePub, PDF)", "icon": "calibre",
     "official": False, "homepage": "https://github.com/janeczku/calibre-web",
     "ports": "8383:8383", "volumes": "cw_config:/config cw_books:/books",
     "envs": "PUID=1000\nPGID=1000", "restart": "unless-stopped", "cmd": "",
     "gpus": "", "stars": 8000, "pulls": 300000000},
    {"id": "audiobookshelf", "name": "Audiobookshelf", "image": "advplyr/audiobookshelf:latest",
     "repo": "advplyr/audiobookshelf", "category": "Медиа",
     "desc": "Стриминг аудиокниг и подкастов", "icon": None, "official": False,
     "homepage": "https://www.audiobookshelf.org", "ports": "13378:80",
     "volumes": "abs_config:/config abs_audiobooks:/audiobooks", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 5000, "pulls": 100000000},

    # ── Поиск, RSS, закладки, уведомления ─────────────────────────────────
    {"id": "searxng", "name": "SearXNG", "image": "searxng/searxng:latest",
     "repo": "searxng/searxng", "category": "Утилиты",
     "desc": "Приватная мета-поисковая система", "icon": None, "official": False,
     "homepage": "https://docs.searxng.org", "ports": "8888:8080",
     "volumes": "searxng_data:/etc/searxng", "envs": "SEARXNG_BASE_URL=http://localhost:8888",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 17000, "pulls": 200000000},
    {"id": "freshrss", "name": "FreshRSS", "image": "freshrss/freshrss:latest",
     "repo": "FreshRSS/FreshRSS", "category": "Продуктивность",
     "desc": "Лёгкий RSS-ридер", "icon": "freshrss", "official": False,
     "homepage": "https://freshrss.org", "ports": "8080:80",
     "volumes": "freshrss_data:/var/www/FreshRSS", "envs": "CRON_MIN='3,33'",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 6500, "pulls": 150000000},
    {"id": "miniflux", "name": "Miniflux", "image": "miniflux/miniflux:latest",
     "repo": "miniflux/miniflux", "category": "Продуктивность",
     "desc": "Минималистичный RSS-ридер", "icon": None, "official": False,
     "homepage": "https://miniflux.app", "ports": "8080:8080", "volumes": "",
     "envs": "DATABASE_URL=postgres://db/miniflux\nRUN_MIGRATIONS=1\nCREATE_ADMIN=1",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 6000, "pulls": 100000000},
    {"id": "linkding", "name": "Linkding", "image": "sissbruecker/linkding:latest",
     "repo": "sissbruecker/linkding", "category": "Продуктивность",
     "desc": "Свои закладки с тегами и поиском", "icon": None, "official": False,
     "homepage": "https://github.com/sissbruecker/linkding", "ports": "9090:9090",
     "volumes": "linkding_data:/etc/linkding", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 7000, "pulls": 100000000},
    {"id": "ntfy", "name": "ntfy", "image": "binwiederhier/ntfy:latest",
     "repo": "binwiederhier/ntfy", "category": "Утилиты",
     "desc": "Push-уведомления на телефон (просто и бесплатно)", "icon": None,
     "official": False, "homepage": "https://ntfy.sh", "ports": "8080:80",
     "volumes": "ntfy_cache:/var/cache/ntfy", "envs": "NTFY_BASE_URL=http://localhost:8080",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 23000, "pulls": 150000000},

    # ── Сеть, VPN, безопасность ───────────────────────────────────────────
    {"id": "tailscale", "name": "Tailscale", "image": "tailscale/tailscale:latest",
     "repo": "tailscale/tailscale", "category": "Сеть",
     "desc": "Приватная VPN-сеть (WireGuard) без портов", "icon": "tailscale",
     "official": False, "homepage": "https://tailscale.com", "ports": "",
     "volumes": "ts_state:/var/lib/tailscale",
     "envs": "TS_AUTHKEY=changeme\nTS_STATE_DIR=/var/lib/tailscale",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 5600, "pulls": 500000000},
    {"id": "cloudflared", "name": "Cloudflared", "image": "cloudflare/cloudflared:latest",
     "repo": "cloudflare/cloudflared", "category": "Сеть",
     "desc": "Туннель Cloudflare (внешний доступ без IP)", "icon": "cloudflare",
     "official": False, "homepage": "https://developers.cloudflare.com/cloudflare-one/",
     "ports": "", "volumes": "", "envs": "TUNNEL_TOKEN=changeme",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 8000, "pulls": 500000000},
    {"id": "vault", "name": "HashiCorp Vault", "image": "hashicorp/vault:1.18",
     "repo": "hashicorp/vault", "category": "Безопасность",
     "desc": "Хранение секретов и шифрование", "icon": "vault",
     "official": False, "homepage": "https://www.vaultproject.io", "ports": "8200:8200",
     "volumes": "vault_data:/vault/file", "envs": "VAULT_DEV_ROOT_TOKEN_ID=changeme\nVAULT_DEV_LISTEN_ADDR=0.0.0.0:8200",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 31000, "pulls": 500000000},
    {"id": "crowdsec", "name": "CrowdSec", "image": "crowdsecurity/crowdsec:latest",
     "repo": "crowdsecurity/crowdsec", "category": "Безопасность",
     "desc": "Collaborative IDS: защита от брутфорса и сканеров", "icon": None,
     "official": False, "homepage": "https://www.crowdsec.net", "ports": "",
     "volumes": "cs_config:/etc/crowdsec cs_data:/var/lib/crowdsec/data",
     "envs": "", "restart": "unless-stopped", "cmd": "", "gpus": "",
     "stars": 7000, "pulls": 100000000},
    {"id": "yacht", "name": "Yacht", "image": "selfhostedpro/yacht:latest",
     "repo": "SelfhostedPro/Yacht", "category": "DevOps",
     "desc": "Минималистичный веб-менеджер контейнеров", "icon": None,
     "official": False, "homepage": "https://yacht.sh", "ports": "8080:8080",
     "volumes": "yacht_config:/config", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 3000, "pulls": 50000000},

    # ── Дашборды и AI-конструкторы ────────────────────────────────────────
    {"id": "dashy", "name": "Dashy", "image": "lauragift21/dashy:latest",
     "repo": "Lissy93/dashy", "category": "Утилиты",
     "desc": "Настраиваемый дашборд для self-hosted сервисов", "icon": None,
     "official": False, "homepage": "https://dashy.to", "ports": "8080:80",
     "volumes": "dashy_conf:/app/public/conf", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 16000, "pulls": 100000000},
    {"id": "dify", "name": "Dify", "image": "langgenius/dify:latest",
     "repo": "langgenius/dify", "category": "AI",
     "desc": "Конструктор LLM-приложений и агентов (no-code)", "icon": None,
     "official": False, "homepage": "https://dify.ai", "ports": "8080:80",
     "volumes": "dify_sandbox:/workspace", "envs": "",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 100000, "pulls": 100000000},
    {"id": "linkwarden", "name": "Linkwarden", "image": "ghcr.io/linkwarden/linkwarden:latest",
     "repo": None, "category": "Продуктивность",
     "desc": "Сохранение и архивация ссылок с синхронизацией", "icon": None,
     "official": False, "homepage": "https://linkwarden.app", "ports": "3000:3000",
     "volumes": "lw_data:/app/data", "envs": "NEXTAUTH_SECRET=changeme\nDATABASE_URL=postgres://db/linkwarden",
     "restart": "unless-stopped", "cmd": "", "gpus": "", "stars": 14000, "pulls": 50000000},
]

_REQUIRED_FIELDS = (
    "id", "name", "image", "category", "desc", "official", "homepage",
    "ports", "volumes", "envs", "restart", "cmd", "gpus", "stars", "pulls",
)

_BY_ID = {e["id"]: e for e in CATALOG}


def get_top100(limit=100, sort="pulls"):
    """Весь каталог, отсортированный по популярности.

    sort: "pulls" (по умолчанию) | "stars" | "name" | "cat"
    limit<=0 — без ограничения.
    """
    items = list(CATALOG)
    if sort == "stars":
        items.sort(key=lambda e: e.get("stars", 0), reverse=True)
    elif sort == "name":
        items.sort(key=lambda e: e["name"].lower())
    elif sort == "cat":
        items.sort(key=lambda e: (e["category"], e["name"].lower()))
    else:  # pulls
        items.sort(key=lambda e: e.get("pulls", 0), reverse=True)
    if limit and limit > 0:
        items = items[:limit]
    return [dict(e) for e in items]


def get(entry_id):
    """Запись каталога по id (копию) или None."""
    e = _BY_ID.get(entry_id)
    return dict(e) if e else None


def search(q, limit=100):
    """Фильтр по имени/описанию/категории/id (регистронезависимый)."""
    q = (q or "").strip().lower()
    if not q:
        return get_top100(limit=limit, sort="pulls")
    out = []
    for e in CATALOG:
        hay = " ".join((e["id"], e["name"], e["category"], e["desc"])).lower()
        if q in hay:
            out.append(dict(e))
    return out[:limit] if limit and limit > 0 else out


def categories():
    """Список категорий: [{"name": …, "count": …}, …] по алфавиту."""
    counts = {}
    for e in CATALOG:
        counts[e["category"]] = counts.get(e["category"], 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items())]


def validate_catalog():
    """Проверка целостности каталога. Возвращает список ошибок (пусто = ок).

    Ловит регрессии при правке CATALOG: дубли id, отсутствующие поля,
    кривые пресеты портов/томов.
    """
    errors = []
    seen = set()
    for i, e in enumerate(CATALOG):
        where = f"CATALOG[{i}] ({e.get('id', '?')})"
        for f in _REQUIRED_FIELDS:
            if f not in e:
                errors.append(f"{where}: нет поля {f!r}")
        eid = e.get("id")
        if eid in seen:
            errors.append(f"{where}: дубликат id")
        seen.add(eid)
        if not isinstance(e.get("official"), bool):
            errors.append(f"{where}: official должен быть bool")
        for f in ("stars", "pulls"):
            v = e.get(f)
            if not isinstance(v, int) or v < 0:
                errors.append(f"{where}: {f} должен быть неотрицательным int")
        img = e.get("image", "")
        if not img or " " in img:
            errors.append(f"{where}: кривой image {img!r}")
        for p in (e.get("ports") or "").split():
            if ":" not in p:
                errors.append(f"{where}: кривой порт {p!r} (нет ':')")
        for v in (e.get("volumes") or "").split():
            if ":" not in v:
                errors.append(f"{where}: кривой том {v!r} (нет ':')")
        for line in (e.get("envs") or "").splitlines():
            if line and "=" not in line:
                errors.append(f"{where}: кривой env {line!r} (нет '=')")
    return errors


if __name__ == "__main__":
    errs = validate_catalog()
    if errs:
        print("ОШИБКИ КАТАЛОГА:")
        for e in errs:
            print(" -", e)
        raise SystemExit(1)
    print(f"OK: {len(CATALOG)} записей, {len(categories())} категорий")
