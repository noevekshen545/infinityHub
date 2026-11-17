"""Catalog of ~100 popular Linux applications for the marketplace."""

CATALOG = [
    # ── Интернет ──────────────────────────────────────────────────────
    {"id":"firefox","name":"Firefox","description":"Веб-браузер Mozilla","icon":"firefox","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y firefox","size":"200MB","rating":4.8},
    {"id":"chromium","name":"Chromium","description":"Открытый браузер Google","icon":"chromium-browser","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y chromium-browser","size":"180MB","rating":4.7},
    {"id":"thunderbird","name":"Thunderbird","description":"Почтовый клиент Mozilla","icon":"thunderbird","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y thunderbird","size":"150MB","rating":4.5},
    {"id":"transmission","name":"Transmission","description":"BitTorrent клиент","icon":"transmission","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y transmission-gtk","size":"5MB","rating":4.4},
    {"id":"filezilla","name":"FileZilla","description":"FTP/SFTP клиент","icon":"filezilla","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y filezilla","size":"30MB","rating":4.3},
    {"id":"discord","name":"Discord","description":"Голосовой и текстовый чат","icon":"discord","category":"Интернет","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub com.discordapp.Discord","size":"150MB","rating":4.6},
    {"id":"telegram-desktop","name":"Telegram Desktop","description":"Клиент Telegram","icon":"telegram","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y telegram-desktop","size":"60MB","rating":4.7},
    {"id":"zoom","name":"Zoom","description":"Видеоконференции","icon":"zoom","category":"Интернет","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub us.zoom.Zoom","size":"120MB","rating":4.2},
    {"id":"qbittorrent","name":"qBittorrent","description":"Бесплатный BitTorrent клиент","icon":"qbittorrent","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y qbittorrent","size":"20MB","rating":4.5},
    {"id":"claws-mail","name":"Claws Mail","description":"Лёгкий почтовый клиент","icon":"claws-mail","category":"Интернет","install_type":"apt","install_cmd":"sudo apt install -y claws-mail","size":"10MB","rating":4.0},

    # ── Медиа ─────────────────────────────────────────────────────────
    {"id":"vlc","name":"VLC","description":"Универсальный медиаплеер","icon":"vlc","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y vlc","size":"80MB","rating":4.8},
    {"id":"audacity","name":"Audacity","description":"Редактор аудио","icon":"audacity","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y audacity","size":"50MB","rating":4.6},
    {"id":"obs-studio","name":"OBS Studio","description":"Запись и стриминг видео","icon":"obs","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y obs-studio","size":"100MB","rating":4.8},
    {"id":"mpv","name":"mpv","description":"Минималистичный видеоплеер","icon":"mpv","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y mpv","size":"15MB","rating":4.7},
    {"id":"rhythmbox","name":"Rhythmbox","description":"Музыкальный плеер GNOME","icon":"rhythmbox","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y rhythmbox","size":"30MB","rating":4.3},
    {"id":"clementine","name":"Clementine","description":"Музыкальный плеер","icon":"clementine","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y clementine","size":"25MB","rating":4.5},
    {"id":"handbrake","name":"HandBrake","description":"Конвертер видео","icon":"handbrake","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y handbrake","size":"40MB","rating":4.5},
    {"id":"krita","name":"Krita","description":"Цифровая живопись и иллюстрации","icon":"krita","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y krita","size":"120MB","rating":4.7},
    {"id":"blender","name":"Blender","description":"3D-моделирование и анимация","icon":"blender","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y blender","size":"200MB","rating":4.8},
    {"id":"inkscape","name":"Inkscape","description":"Векторный графический редактор","icon":"inkscape","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y inkscape","size":"60MB","rating":4.6},
    {"id":"gimp","name":"GIMP","description":"Редактор изображений","icon":"gimp","category":"Графика","install_type":"apt","install_cmd":"sudo apt install -y gimp","size":"80MB","rating":4.5},
    {"id":"darktable","name":"darktable","description":"Обработка фото (RAW)","icon":"darktable","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y darktable","size":"100MB","rating":4.4},
    {"id":"shotwell","name":"Shotwell","description":"Менеджер фотографий","icon":"shotwell","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y shotwell","size":"20MB","rating":4.2},
    {"id":"sound-juicer","name":"Sound Juicer","description":"Рипер CD","icon":"sound-juicer","category":"Медиа","install_type":"apt","install_cmd":"sudo apt install -y sound-juicer","size":"5MB","rating":4.0},

    # ── Графика ───────────────────────────────────────────────────────
    {"id":"pinta","name":"Pinta","description":"Простой редактор изображений","icon":"pinta","category":"Графика","install_type":"apt","install_cmd":"sudo apt install -y pinta","size":"15MB","rating":4.3},
    {"id":"kolourpaint","name":"KolourPaint","description":"Рисование и редактирование","icon":"kolourpaint","category":"Графика","install_type":"apt","install_cmd":"sudo apt install -y kolourpaint","size":"10MB","rating":4.1},
    {"id":"xfig","name":"Xfig","description":"Векторный редактор","icon":"xfig","category":"Графика","install_type":"apt","install_cmd":"sudo apt install -y xfig","size":"5MB","rating":3.8},
    {"id":"shotcut","name":"Shotcut","description":"Видеоредактор","icon":"shotcut","category":"Графика","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub org.shotcut.Shotcut","size":"100MB","rating":4.4},
    {"id":"kdenlive","name":"Kdenlive","description":"Видеоредактор KDE","icon":"kdenlive","category":"Графика","install_type":"apt","install_cmd":"sudo apt install -y kdenlive","size":"80MB","rating":4.5},
    {"id":"synfig","name":"Synfig Studio","description":"2D-анимация","icon":"synfigstudio","category":"Графика","install_type":"apt","install_cmd":"sudo apt install -y synfig","size":"50MB","rating":4.2},
    {"id":"natron","name":"Natron","description":"Композитинг (аналог Nuke)","icon":"Natron","category":"Графика","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub fr.natron.Natron","size":"80MB","rating":4.3},

    # ── Офис ──────────────────────────────────────────────────────────
    {"id":"libreoffice","name":"LibreOffice","description":"Офисный пакет","icon":"libreoffice","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y libreoffice","size":"300MB","rating":4.5},
    {"id":"evince","name":"Evince","description":"Просмотр PDF","icon":"evince","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y evince","size":"15MB","rating":4.4},
    {"id":"okular","name":"Okular","description":"Универсальный просмотрщик","icon":"okular","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y okular","size":"30MB","rating":4.6},
    {"id":"calibre","name":"Calibre","description":"Менеджер электронных книг","icon":"calibre","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y calibre","size":"60MB","rating":4.7},
    {"id":"abiword","name":"AbiWord","description":"Текстовый процессор","icon":"abiword","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y abiword","size":"20MB","rating":4.1},
    {"id":"gnumeric","name":"Gnumeric","description":"Таблицы","icon":"gnumeric","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y gnumeric","size":"15MB","rating":4.2},
    {"id":"pdfarranger","name":"PDF Arranger","description":"Редактор PDF","icon":"pdfarranger","category":"Офис","install_type":"apt","install_cmd":"sudo apt install -y pdfarranger","size":"10MB","rating":4.3},
    {"id":"xmind","name":"XMind","description":"Ментальные карты","icon":"xmind","category":"Офис","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub net.xmind.XMind.Zen","size":"120MB","rating":4.4},

    # ── Разработка ────────────────────────────────────────────────────
    {"id":"vscode","name":"VS Code","description":"Редактор кода Microsoft","icon":"code","category":"Разработка","install_type":"apt","install_cmd":"sudo snap install code --classic","size":"300MB","rating":4.8},
    {"id":"vscodium","name":"VSCodium","description":"Открытый VS Code","icon":"codium","category":"Разработка","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub com.visualstudio.code-oss","size":"300MB","rating":4.7},
    {"id":"sublime-text","name":"Sublime Text","description":"Быстрый редактор кода","icon":"sublime-text","category":"Разработка","install_type":"apt","install_cmd":"sudo snap install sublime-text --classic","size":"50MB","rating":4.5},
    {"id":"geany","name":"Geany","description":"Лёгкий IDE","icon":"geany","category":"Разработка","install_type":"apt","install_cmd":"sudo apt install -y geany","size":"20MB","rating":4.3},
    {"id":"kate","name":"Kate","description":"Текстовый редактор KDE","icon":"kate","category":"Разработка","install_type":"apt","install_cmd":"sudo apt install -y kate","size":"30MB","rating":4.5},
    {"id":"atom","name":"Atom","description":"Редактор кода GitHub","icon":"atom","category":"Разработка","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub io.atom.Atom","size":"200MB","rating":4.3},
    {"id":"vim","name":"Vim","description":"Классический текстовый редактор","icon":"vim","category":"Разработка","install_type":"apt","install_cmd":"sudo apt install -y vim","size":"5MB","rating":4.6},
    {"id":"emacs","name":"Emacs","description":"Мощный текстовый редактор","icon":"emacs","category":"Разработка","install_type":"apt","install_cmd":"sudo apt install -y emacs","size":"40MB","rating":4.5},
    {"id":"gitkraken","name":"GitKraken","description":"GUI для Git","icon":"gitkraken","category":"Разработка","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub com.GitKraken.GitKraken","size":"150MB","rating":4.4},
    {"id":"docker","name":"Docker","description":"Контейнеризация","icon":"docker","category":"Разработка","install_type":"apt","install_cmd":"sudo apt install -y docker.io","size":"50MB","rating":4.7},
    {"id":"postman","name":"Postman","description":"API-тестирование","icon":"postman","category":"Разработка","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub com.getpostman.Postman","size":"200MB","rating":4.5},
    {"id":"remmina","name":"Remmina","description":"Удалённый доступ","icon":"remmina","category":"Разработка","install_type":"apt","install_cmd":"sudo apt install -y remmina","size":"30MB","rating":4.4},

    # ── Игры ──────────────────────────────────────────────────────────
    {"id":"steam","name":"Steam","description":"Игровая платформа Valve","icon":"steam","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y steam","size":"200MB","rating":4.6},
    {"id":"lutris","name":"Lutris","description":"Менеджер игр Linux","icon":"lutris","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y lutris","size":"30MB","rating":4.5},
    {"id":"wine","name":"Wine","description":"Запуск Windows-программ","icon":"wine","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y wine","size":"100MB","rating":4.3},
    {"id":"playonlinux","name":"PlayOnLinux","description":"Менеджер Windows-игр","icon":"playonlinux","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y playonlinux","size":"20MB","rating":4.1},
    {"id":"dosbox","name":"DOSBox","description":"Эмулятор DOS","icon":"dosbox","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y dosbox","size":"5MB","rating":4.2},
    {"id":"retroarch","name":"RetroArch","description":"Фронтенд эмуляторов","icon":"retroarch","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y retroarch","size":"20MB","rating":4.4},
    {"id":"mame","name":"MAME","description":"Эмулятор аркадных автоматов","icon":"mame","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y mame","size":"100MB","rating":4.0},
    {"id":"supertux","name":"SuperTux","description":"Платформер с пингвином","icon":"supertux","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y supertux","size":"50MB","rating":4.3},
    {"id":"0ad","name":"0 A.D.","description":"Стратегия в реальном времени","icon":"0ad","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y 0ad","size":"500MB","rating":4.4},
    {"id":"freeciv","name":"Freeciv","description":"Цивилизация (клон)","icon":"freeciv","category":"Игры","install_type":"apt","install_cmd":"sudo apt install -y freeciv","size":"30MB","rating":4.2},

    # ── Утилиты ───────────────────────────────────────────────────────
    {"id":"gparted","name":"GParted","description":"Редактор разделов","icon":"gparted","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y gparted","size":"10MB","rating":4.6},
    {"id":"htop","name":"htop","description":"Монитор процессов","icon":"htop","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y htop","size":"2MB","rating":4.8},
    {"id":"neofetch","name":"Neofetch","description":"Информация о системе","icon":"neofetch","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y neofetch","size":"1MB","rating":4.5},
    {"id":"baobab","name":"Baobab","description":"Анализатор дискового пространства","icon":"baobab","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y baobab","size":"5MB","rating":4.3},
    {"id":"file-roller","name":"File Roller","description":"Архиватор","icon":"file-roller","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y file-roller","size":"10MB","rating":4.4},
    {"id":"p7zip","name":"p7zip","description":"Архиватор 7z","icon":"p7zip","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y p7zip-full","size":"2MB","rating":4.5},
    {"id":"timeshift","name":"Timeshift","description":"Бэкапы системы","icon":"timeshift","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y timeshift","size":"15MB","rating":4.6},
    {"id":"veracrypt","name":"VeraCrypt","description":"Шифрование дисков","icon":"veracrypt","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y veracrypt","size":"20MB","rating":4.5},
    {"id":"bleachbit","name":"BleachBit","description":"Очистка системы","icon":"bleachbit","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y bleachbit","size":"10MB","rating":4.3},
    {"id":"grsync","name":"Grsync","description":"GUI для rsync","icon":"grsync","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y grsync","size":"5MB","rating":4.2},
    {"id":"purple","name":"Pidgin","description":"Мультиплатформенный чат","icon":"pidgin","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y pidgin","size":"20MB","rating":4.0},
    {"id":"galculator","name":"Galculator","description":"Калькулятор","icon":"galculator","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y galculator","size":"3MB","rating":4.1},
    {"id":"gnome-screenshot","name":"GNOME Screenshot","description":"Скриншоты","icon":"gnome-screenshot","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y gnome-screenshot","size":"3MB","rating":4.2},
    {"id":"flameshot","name":"Flameshot","description":"Продвинутые скриншоты","icon":"flameshot","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y flameshot","size":"5MB","rating":4.7},
    {"id":"speedcrunch","name":"SpeedCrunch","description":"Научный калькулятор","icon":"speedcrunch","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y speedcrunch","size":"3MB","rating":4.3},
    {"id":"stacer","name":"Stacer","description":"Оптимизация Ubuntu","icon":"stacer","category":"Утилиты","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub io.github.stacerorg.Stacer","size":"50MB","rating":4.4},
    {"id":"systemback","name":"Systemback","description":"Бэкап и восстановление","icon":"systemback","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y systemback","size":"10MB","rating":4.2},
    {"id":"conky","name":"Conky","description":"Системный монитор на рабочем столе","icon":"conky","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y conky","size":"5MB","rating":4.3},
    {"id":"xfce4-terminal","name":"XFCE4 Terminal","description":"Эмулятор терминала","icon":"utilities-terminal","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y xfce4-terminal","size":"5MB","rating":4.4},
    {"id":"tilix","name":"Tilix","description":"Тайлинговый терминал","icon":"tilix","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y tilix","size":"5MB","rating":4.5},
    {"id":"guake","name":"Guake","description":"Выпадающий терминал","icon":"guake","category":"Утилиты","install_type":"apt","install_cmd":"sudo apt install -y guake","size":"5MB","rating":4.3},

    # ── Система ───────────────────────────────────────────────────────
    {"id":"gnome-tweaks","name":"GNOME Tweaks","description":"Настройки GNOME","icon":"gnome-tweaks","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y gnome-tweaks","size":"5MB","rating":4.4},
    {"id":"dconf-editor","name":"dconf Editor","description":"Редактор dconf","icon":"dconf-editor","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y dconf-editor","size":"3MB","rating":4.2},
    {"id":"gnome-shell-extensions","name":"GNOME Shell Extensions","description":"Расширения GNOME","icon":"gnome-shell-extensions","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y gnome-shell-extensions","size":"5MB","rating":4.3},
    {"id":"neovim","name":"Neovim","description":"Современный Vim","icon":"nvim","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y neovim","size":"5MB","rating":4.6},
    {"id":"tmux","name":"tmux","description":"Мультиплексор терминала","icon":"tmux","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y tmux","size":"2MB","rating":4.7},
    {"id":"fish","name":"Fish","description":"Дружелюбная оболочка","icon":"fish","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y fish","size":"3MB","rating":4.5},
    {"id":"zsh","name":"Zsh","description":"Мощная оболочка","icon":"zsh","category":"Система","install_type":"apt","install_cmd":"sudo apt install -y zsh","size":"3MB","rating":4.6},

    # ── Наука ─────────────────────────────────────────────────────────
    {"id":"gnuplot","name":"GNUPLOT","description":"Графический plotter","icon":"gnuplot","category":"Наука","install_type":"apt","install_cmd":"sudo apt install -y gnuplot","size":"15MB","rating":4.3},
    {"id":"scilab","name":"Scilab","description":"Численные вычисления","icon":"scilab","category":"Наука","install_type":"apt","install_cmd":"sudo apt install -y scilab","size":"200MB","rating":4.2},
    {"id":"stellarium","name":"Stellarium","description":"Планетарий","icon":"stellarium","category":"Наука","install_type":"apt","install_cmd":"sudo apt install -y stellarium","size":"100MB","rating":4.5},
    {"id":"geogebra","name":"GeoGebra","description":"Математика и геометрия","icon":"geogebra","category":"Наука","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub org.geogebra.Calculator","size":"100MB","rating":4.4},
    {"id":"r","name":"R","description":"Статистика и анализ данных","icon":"r","category":"Наука","install_type":"apt","install_cmd":"sudo apt install -y r-base","size":"50MB","rating":4.5},
    {"id":"jupyter","name":"Jupyter Notebook","description":"Интерактивные блокноты","icon":"jupyter","category":"Наука","install_type":"apt","install_cmd":"sudo apt install -y jupyter","size":"30MB","rating":4.6},

    # ── Образование ───────────────────────────────────────────────────
    {"id":"anki","name":"Anki","description":"Карточки для запоминания","icon":"anki","category":"Образование","install_type":"apt","install_cmd":"sudo apt install -y anki","size":"50MB","rating":4.7},
    {"id":"gcompris","name":"GCompris","description":"Образовательные игры для детей","icon":"gcompris","category":"Образование","install_type":"apt","install_cmd":"sudo apt install -y gcompris","size":"100MB","rating":4.4},
    {"id":"tuxtype","name":"TuxTyping","description":"Обучение печати","icon":"tuxtype","category":"Образование","install_type":"apt","install_cmd":"sudo apt install -y tuxtype","size":"10MB","rating":4.2},
    {"id":"scratch","name":"Scratch","description":"Визуальное программирование","icon":"scratch","category":"Образование","install_type":"flatpak","install_cmd":"sudo flatpak install -y flathub edu.scratch.Scratch","size":"100MB","rating":4.5},
    {"id":"celestia","name":"Celestia","description":"Космический планетарий","icon":"celestia","category":"Образование","install_type":"apt","install_cmd":"sudo apt install -y celestia","size":"30MB","rating":4.3},
    {"id":"kalzium","name":"Kalzium","description":"Периодическая таблица","icon":"kalzium","category":"Образование","install_type":"apt","install_cmd":"sudo apt install -y kalzium","size":"20MB","rating":4.1},
]

# Ratings-based sort for Top 100
def get_top100():
    return sorted(CATALOG, key=lambda x: x.get("rating", 0), reverse=True)[:100]

# Category filter
def get_by_category(category):
    if category == "all":
        return CATALOG
    return [a for a in CATALOG if a.get("category") == category]

# Search
def search_catalog(query):
    q = query.lower()
    return [a for a in CATALOG if q in a.get("name","").lower() or q in a.get("description","").lower() or q in a.get("id","").lower()]
