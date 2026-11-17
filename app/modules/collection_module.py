import os, re, json, sqlite3, zipfile, base64, threading, queue, time, concurrent.futures
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from xml.etree import ElementTree as ET

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker_pool
import swallow

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collection.db")
COLLECTION_DB = _DEFAULT_DB
_OVERRIDE_DB = None

# ─── FLIBUSTA GENRE MAP ─────────────────────────────────────────────────

GENRE_NAMES = {
    # Фантастика и фэнтези
    "sf": "Научная фантастика", "sf_history": "Альтернативная история", "sf_action": "Боевая фантастика",
    "sf_epic": "Эпическая фантастика", "sf_heroic": "Героическая фантастика", "sf_detective": "Детективная фантастика",
    "sf_cyberpunk": "Киберпанк", "sf_space": "Космическая фантастика", "sf_social": "Социальная фантастика",
    "sf_horror": "Фантастика ужасов", "sf_techno": "Технофэнтези", "sf_fantasy_city": "Городское фэнтези",
    "sf_fantasy_heroic": "Героическое фэнтези", "sf_fantasy_dark": "Тёмное фэнтези", "sf_fantasy_epic": "Эпическое фэнтези",
    "sf_fantasy_humor": "Юмористическое фэнтези", "sf_fantasy_history": "Историческое фэнтези",
    "sf_fantasy_parody": "Пародийное фэнтези", "sf_fantasy_horror": "Фэнтези ужасов", "sf_fantasy": "Фэнтези",
    "sf_humor": "Юмористическая фантастика", "sf_mystic": "Мистическая фантастика", "sf_irony": "Ироническая фантастика",
    "sf_litrpg": "ЛитРПГ", "sf_postapocalyptic": "Постапокалипсис", "sf_stimpank": "Стимпанк",
    "sf_space_opera": "Космическая опера", "sf_althistory": "Альтернативная история",
    "sf_technofantasy": "Технофэнтези", "sf_erotic": "Эротическая фантастика",
    "sf_all": "Фантастика (общее)", "sf_rus": "Русская фантастика", "sf_su": "Советская фантастика",
    "sf_writing": "Фантастика", "sf_love": "Любовная фантастика",
    "fantasy": "Фэнтези", "fantasy_action": "Боевое фэнтези", "fantasy_dark": "Тёмное фэнтези",
    "fantasy_epic": "Эпическое фэнтези", "fantasy_heroic": "Героическое фэнтези", "fantasy_history": "Историческое фэнтези",
    "fantasy_rus": "Русское фэнтези", "fantasy_det": "Детективное фэнтези",
    "epic_fantasy": "Эпическое фэнтези", "heroic_fantasy": "Героическое фэнтези",
    "dark_fantasy": "Тёмное фэнтези", "urban_fantasy": "Городское фэнтези",
    "slavic_fantasy": "Славянское фэнтези", "popadanec": "Попаданцы",
    "litrpg": "ЛитРПГ", "popadancy": "Попаданцы", "steampunk": "Стимпанк",
    "cyberpunk": "Киберпанк", "postapocalyptic": "Постапокалипсис",
    "dystopia": "Антиутопия", "magic": "Магия", "wuxia": "Уся (Wuxia)",

    # Детективы и триллеры
    "det": "Детектив", "det_action": "Боевик", "det_police": "Полицейский детектив",
    "det_irony": "Иронический детектив", "det_history": "Исторический детектив", "det_espionage": "Шпионский детектив",
    "det_crime": "Криминальный детектив", "det_political": "Политический детектив", "det_maniac": "Маньяки",
    "det_hard": "Крутой детектив", "det_classic": "Классический детектив", "det_short": "Детективные рассказы",
    "det_artifact": "Артефакт-детектив", "det_cozy": "Уютный детектив", "det_other": "Прочий детектив",
    "det_rus": "Русский детектив", "det_su": "Советский детектив", "det_all": "Детектив (общее)",
    "thriller": "Триллер", "thriller_psychological": "Психологический триллер",
    "thriller_legal": "Юридический триллер", "thriller_medical": "Медицинский триллер",
    "thriller_techno": "Технотриллер", "thriller_mystery": "Мистический триллер",
    "horror": "Ужасы", "horror_mystic": "Мистика", "horror_fantasy": "Фэнтези ужасов",
    "horror_occult": "Оккультные ужасы", "horror_vampires": "Вампиры",
    "mystery": "Мистика", "mistika": "Мистика",

    # Любовные романы
    "love": "Любовные романы", "love_contemporary": "Современные любовные романы", "love_history": "Исторические любовные романы",
    "love_erotica": "Эротика", "love_short": "Любовные рассказы", "love_detective": "Любовный детектив",
    "love_fantasy": "Любовное фэнтези", "love_sf": "Любовная фантастика",
    "love_sf_fantasy": "Любовное фэнтези/фантастика", "love_conremporary": "Современные любовные романы",
    "love_hard": "Остросюжетные любовные романы", "love_rus": "Русские любовные романы",
    "love_slash": "Слэш", "love_femslash": "Фемслэш",
    "erotica": "Эротика", "erotic_fantasy": "Эротическое фэнтези",
    "romance": "Романтика", "romance_contemporary": "Современный роман",
    "romance_fantasy": "Романтическое фэнтези", "romance_historical": "Исторический роман",

    # Проза
    "prose": "Проза", "prose_classic": "Классическая проза", "prose_history": "Историческая проза",
    "prose_contemporary": "Современная проза", "prose_counter": "Контркультура", "prose_rus_classic": "Русская классика",
    "prose_su_classics": "Советская классика", "prose_military": "Военная проза", "prose_epic": "Эпическая проза",
    "prose_abs": "Абсурдистская проза", "prose_game": "Игровая проза",
    "prose_magic": "Магическая проза", "prose_sentimental": "Сентиментальная проза",
    "prose_teen": "Подростковая проза", "prose_neformatny": "Неформатная проза",
    "prose_root": "Проза",
    "contemporary": "Современная проза", "prose_history": "Историческая проза",
    "classics": "Классика", "drama": "Драма", "tragedy": "Трагедия",

    # Приключения
    "adv_animal": "Природа и животные", "adv_geo": "Путешествия и география", "adv_history": "Исторические приключения",
    "adv_indian": "Индейцы", "adv_maritime": "Морские приключения", "adv_western": "Вестерн",
    "adv": "Приключения", "adv_all": "Приключения (общее)", "adv_military": "Военные приключения",
    "adv_modern": "Современные приключения", "adv_rus": "Русские приключения", "adv_su": "Советские приключения",

    # Детская литература
    "child": "Детская литература", "child_tale": "Сказки", "child_verse": "Детские стихи",
    "child_prose": "Детская проза", "child_sf": "Детская фантастика", "child_det": "Детский детектив",
    "child_adv": "Приключения для детей", "child_education": "Детская образовательная", "child_poetry": "Детские стихи",
    "child_animals": "Детские книги о животных", "child_history": "Детская историческая",
    "child_horror": "Детские ужасы", "child_nature": "Детские о природе",
    "child_baby": "Для самых маленьких", "child_3": "Детям до 3 лет",
    "child_4": "Детям 4-6 лет", "child_9": "Детям 7-10 лет",
    "age_elementary": "Младший школьный возраст", "age_high": "Старший школьный возраст",
    "age_wide": "Для всех возрастов", "kids": "Для детей",
    "young_adult": "Молодёжная литература", "ya": "Young Adult",

    # Поэзия
    "poetry": "Поэзия", "poetry_classic": "Классическая поэзия", "poetry_modern": "Современная поэзия",
    "poem": "Поэмы", "poetry_classical": "Классическая поэзия", "poetry_east": "Восточная поэзия",
    "poetry_rus_classical": "Русская классическая поэзия", "poetry_rus_modern": "Русская современная поэзия",
    "poetry_su": "Советская поэзия", "song_poetry": "Песенная поэзия",
    "lyrics": "Лирика", "verse": "Стихи", "poem": "Поэмы",

    # Юмор
    "humor": "Юмор", "humor_prose": "Юмористическая проза", "humor_verse": "Юмористические стихи",
    "humor_fantasy": "Юмористическое фэнтези", "humor_det": "Юмористический детектив",
    "humor_all": "Юмор (общее)", "humor_anecdote": "Анекдоты", "humor_parody": "Пародия",
    "humor_satire": "Сатира", "humor_sf_fantasy": "Юмористическая фантастика",
    "comedy": "Комедия", "satire": "Сатира", "parody": "Пародия",

    # История и биографии
    "history": "История", "history_modern": "Новейшая история", "history_world": "Всемирная история",
    "history_russia": "История России", "history_ancient": "Древний мир", "history_middle": "Средневековье",
    "history_war": "Военная история", "history_asia": "История Азии", "history_europe": "История Европы",
    "history_usa": "История США", "history_africa": "История Африки",
    "biography": "Биографии и мемуары", "biography_war": "Биографии военных",
    "biography_history": "Биографии исторических личностей", "biography_politic": "Биографии политиков",
    "biography_art": "Биографии деятелей искусства", "biography_sci": "Биографии учёных",
    "biogr_arts": "Биографии деятелей искусства", "biogr_historical": "Биографии исторических личностей",
    "biogr_leaders": "Биографии лидеров", "biogr_professionals": "Биографии профессионалов",
    "biogr_sports": "Биографии спортсменов", "biogr_travel": "Биографии путешественников",
    "memoirs": "Мемуары", "autobiography": "Автобиография",

    # Религия и эзотерика
    "religion": "Религия", "religion_rel": "Религиоведение", "religion_esoterics": "Эзотерика",
    "religion_self": "Самосовершенствование", "religion_orthodoxy": "Православие", "religion_islam": "Ислам",
    "religion_buddhism": "Буддизм", "religion_christianity": "Христианство", "religion_paganism": "Язычество",
    "religion_catholicism": "Католицизм", "religion_protestantism": "Протестантизм",
    "religion_hinduism": "Индуизм", "religion_judaism": "Иудаизм",
    "religion_occult": "Оккультизм", "religion_spirituality": "Духовность",
    "buddhism": "Буддизм", "atheism": "Атеизм", "astrology": "Астрология",
    "esoterics": "Эзотерика", "esoterica": "Эзотерика",
    "esoteric": "Эзотерика", "palmistry": "Хиромантия",

    # Психология
    "psy": "Психология", "psy_personal": "Личностный рост", "psy_relations": "Психология отношений",
    "psy_child": "Детская психология", "psy_general": "Общая психология", "psy_sex": "Сексология",
    "psy_social": "Социальная психология",
    "psy_childs": "Детская психология", "psy_generic": "Психология (общее)",
    "psychology": "Психология", "popular_psychology": "Популярная психология",
    "self_help": "Самопомощь",

    # Наука
    "sci": "Наука", "sci_math": "Математика", "sci_phys": "Физика", "sci_chem": "Химия", "sci_bio": "Биология",
    "sci_astronomy": "Астрономия", "sci_medicine": "Медицина", "sci_tech": "Технические науки",
    "sci_history": "История науки", "sci_popular": "Научно-популярная", "sci_computer": "Компьютерная литература",
    "sci_linguistic": "Лингвистика", "sci_philosophy": "Философия", "sci_culture": "Культурология",
    "sci_politics": "Политология", "sci_economics": "Экономика", "sci_law": "Юриспруденция",
    "sci_pedagogy": "Педагогика", "sci_social": "Социология",
    "sci_archeology": "Археология", "sci_ecology": "Экология", "sci_geography": "География",
    "sci_botany": "Ботаника", "sci_zoo": "Зоология", "sci_biology": "Биология",
    "science": "Наука", "physics": "Физика", "biology": "Биология",
    "philosophy": "Философия",

    # Компьютеры
    "comp": "Компьютеры", "comp_hard": "Аппаратное обеспечение", "comp_soft": "Программное обеспечение",
    "comp_prog": "Программирование", "comp_web": "Веб-дизайн", "comp_net": "Компьютерные сети",
    "comp_db": "Базы данных", "comp_os": "Операционные системы", "comp_games": "Компьютерные игры",
    "comp_security": "Компьютерная безопасность", "comp_www": "Веб-технологии",
    "computers": "Компьютеры", "programming": "Программирование",

    # Дом и семья
    "home": "Домоводство", "home_cooking": "Кулинария", "home_garden": "Сад и огород",
    "home_handicraft": "Рукоделие", "home_pets": "Домашние животные", "home_sex": "Секс в браке",
    "home_entertain": "Развлечения", "home_health": "Здоровье", "home_sport": "Спорт",
    "home_diy": "Сделай сам", "home_family": "Семья", "home_fashion": "Мода и стиль",
    "home_beauty": "Красота",
    "home_building": "Строительство", "home_child": "Воспитание детей",
    "home_hunt": "Охота", "home_tourism": "Туризм", "home_crafts": "Ремёсла",
    "cooking": "Кулинария", "garden": "Сад и огород", "sport": "Спорт",
    "health": "Здоровье", "medicine": "Медицина",

    # Справочная литература
    "ref": "Справочная литература", "ref_dict": "Словари", "ref_encyc": "Энциклопедии", "ref_guide": "Путеводители",
    "ref_job": "Профессии и ремёсла", "ref_art": "Искусство", "ref_photo": "Фотография", "ref_music": "Музыка",
    "ref_theatre": "Театр", "ref_cinema": "Кино", "ref_arch": "Архитектура", "ref_paint": "Живопись",
    "ref_sci_popular": "Научно-популярные справочники",

    # Деловая литература
    "business": "Деловая литература", "business_economics": "Экономика и бизнес", "business_marketing": "Маркетинг",
    "business_management": "Менеджмент", "business_finance": "Финансы", "business_account": "Бухгалтерия",
    "business_job": "Карьера", "business_sales": "Продажи", "business_advertising": "Реклама",
    "business_realestate": "Недвижимость", "business_law": "Бизнес-право",

    # Дизайн и искусство
    "design": "Дизайн", "design_web": "Веб-дизайн", "design_graphic": "Графический дизайн",
    "design_interior": "Интерьерный дизайн", "design_landscape": "Ландшафтный дизайн",
    "design_industrial": "Промышленный дизайн", "design_fashion": "Дизайн одежды",
    "art": "Искусство", "art_history": "История искусства", "music": "Музыка",
    "cinema": "Кино", "theatre": "Театр", "photo": "Фотография", "painting": "Живопись",
    "architecture": "Архитектура",

    # Антикварная литература
    "antique": "Антикварная литература", "antique_european": "Античная европейская", "antique_east": "Античная восточная",
    "antique_myth": "Античная мифология", "antique_russian": "Древнерусская литература",
    "antique_ant": "Античная литература", "antique_myths": "Античные мифы",

    # Зарубежная литература
    "foreign_adventure": "Зарубежные приключения", "foreign_contemporary": "Зарубежная современная проза",
    "foreign_detective": "Зарубежный детектив", "foreign_fantasy": "Зарубежное фэнтези",
    "foreign_love": "Зарубежные любовные романы", "foreign_prose": "Зарубежная проза",
    "foreign_sf": "Зарубежная фантастика", "foreign_social": "Зарубежная социальная проза",
    "foreign_military": "Зарубежная военная проза", "foreign_humor": "Зарубежный юмор",
    "foreign_action": "Зарубежный боевик", "foreign_antique": "Зарубежная античная литература",
    "foreign_children": "Зарубежная детская литература", "foreign_poetry": "Зарубежная поэзия",
    "foreign_psychology": "Зарубежная психология",
    "foreign_publicism": "Зарубежная публицистика", "foreign_religion": "Зарубежная религиозная литература",

    # Прочее
    "publicism": "Публицистика", "fanfiction": "Фанфик", "fan_translation": "Перевод фанфика",
    "network_literature": "Сетевая литература", "samizdat": "Самиздат",
    "beginning_authors": "Начинающие авторы", "autor_collection": "Авторский сборник",
    "nonfiction": "Non-fiction", "non_fiction": "Non-fiction",
    "essay": "Эссе", "essays": "Эссе",
    "interview": "Интервью", "letters": "Письма",
    "criticism": "Критика", "literary_criticism": "Литературная критика",
    "journalism": "Журналистика",
    # Дополнительные коды (русскоязычные и альтернативные)
    "about_musicians": "О музыкантах", "accounting": "Бухгалтерия", "action": "Боевик",
    "adult": "Для взрослых", "adv_history_avant": "Исторические приключения (авантюрные)",
    "adv_story": "Приключенческий рассказ", "adventure": "Приключения",
    "adventure_fantasy": "Приключенческое фэнтези", "alexievich": "Алексиевич",
    "antichnaya": "Античная литература", "aphorism_quote": "Афоризмы и цитаты",
    "aphorisms": "Афоризмы", "apocalypse": "Апокалипсис",
    "architecture_book": "Архитектура", "art_criticism": "Искусствоведение",
    "art_instr": "Изобразительное искусство", "art_world_culture": "Мировая культура",
    "auto_business": "Автобизнес", "auto_regulations": "Автонормативы",
    "back_to_ussr": "Ностальгия по СССР", "banking": "Банковское дело",
    "bdsm": "БДСМ", "belarus": "Беларусь", "biografii_i_memuary": "Биографии и мемуары",
    "biograpy": "Биография", "biologiya_i_himiya": "Биология и химия",
    "biz_accounting": "Бизнес-бухгалтерия", "biz_beogr": "Бизнес-география",
    "biz_economics": "Бизнес-экономика", "biz_life": "Бизнес и жизнь",
    "biz_management": "Бизнес-менеджмент",
    "boevaya_fantastika": "Боевая фантастика", "boevoe_fentezi": "Боевое фэнтези",
    "boyar_anime": "Бояръ-Аниме", "cats": "Кошки",
    "cf_fantasy_city": "Городское фэнтези", "child_all": "Детская литература (всё)",
    "child_characters": "Детские персонажи", "child_classical": "Детская классика",
    "child_folklore": "Детский фольклор", "child_rus": "Русская детская литература",
    "child_su": "Советская детская литература", "child_tale_rus": "Русские народные сказки",
    "children": "Детям", "chris_edu": "Христианское образование",
    "chris_fiction": "Христианская проза", "chris_orthodoxy": "Православие",
    "chris_pravoslavie": "Православие", "ci_history": "История (общее)",
    "cine": "Кино", "cinema_theatre": "Кино и театр",
    "city_fantasy": "Городское фэнтези", "collection": "Сборник",
    "comfort": "Уютное чтение", "comics": "Комиксы",
    "comp_osnet": "ОС и сети", "comp_programming": "Программирование",
    "compilation": "Компиляция", "computer_translation": "Компьютерный перевод",
    "criminal_detective": "Криминальный детектив", "detakaya_proza": "Детская проза",
    "detectiv": "Детектив", "detective": "Детектив",
    "detective_fentezi": "Детективное фэнтези", "detective_police": "Полицейский детектив",
    "detektive": "Детектив", "detektivnaya": "Детективная",
    "detskaya_pazvivayushaya_literatura": "Развивающая литература",
    "detskie_detektivy": "Детские детективы",
    "detskie_knigi_zarubezhnye": "Зарубежные детские книги",
    "devushkimonstry": "Девушки-монстры", "dieselpunk": "Дизельпанк",
    "dissident": "Диссидентская литература", "dlya_podrostkov": "Для подростков",
    "dogs": "Собаки", "dokumentalnaya_literatura": "Документальная литература",
    "domashnie_zhivotnye": "Домашние животные", "dorama": "Дорама",
    "dragon_fantasy": "Фэнтези про драконов", "dragons": "Драконы",
    "drakony": "Драконы", "drama_antique": "Античная драма",
    "dramaturgy": "Драматургия", "dramaturgy_all": "Драматургия (все)",
    "drevnevostochnaya": "Древневосточная литература", "dystopian": "Антиутопия",
    "ebook": "Электронная книга", "economics": "Экономика",
    "economics_ref": "Экономические справочники", "entert_humor": "Развлекательный юмор",
    "entertainment": "Развлечения", "epic": "Эпос",
    "epic_poetry": "Эпическая поэзия", "epistolary_fiction": "Эпистолярный жанр",
    "equ_history": "История (общее)", "eroticheskie_rasskazy": "Эротические рассказы",
    "eroticheskie_romany": "Эротические романы",
    "eroticheskoe_fantasy": "Эротическое фэнтези",
    "everyday_fantasy": "Бытовое фэнтези",
    "evoljuciya_i_antropologiya": "Эволюция и антропология",
    "ex_su_publication": "Издание СССР", "experimental_poetry": "Экспериментальная поэзия",
    "extravaganza": "Феерия", "fable": "Басня",
    "fairy_fantasy": "Фейри-фэнтези", "fairy_tale": "Сказка",
    "family": "Семья", "family_health": "Семейное здоровье",
    "family_humor": "Семейный юмор", "family_parenting": "Воспитание детей",
    "family_pregnancy": "Беременность", "family_relations": "Семейные отношения",
    "fanfiction_het": "Гетеро-фанфик", "fantasy_alt_hist": "Альтернативная история (фэнтези)",
    "fantasy_fight": "Боевое фэнтези", "fantasy_wuxia": "Уся",
    "fashion_style": "Мода и стиль", "femslash": "Фемслэш",
    "ficion": "Художественная литература", "fiction": "Художественная литература",
    "folk_songs": "Народные песни", "folk_tale": "Народная сказка",
    "folklor": "Фольклор", "folklore": "Фольклор", "folklore_rus": "Русский фольклор",
    "foreign_business": "Зарубежный бизнес", "foreign_comp": "Зарубежные компьютеры",
    "foreign_desc": "Зарубежная документальная", "foreign_dramaturgy": "Зарубежная драматургия",
    "foreign_edu": "Зарубежная образовательная", "foreign_home": "Зарубежное домоводство",
    "foreign_language": "Иностранный язык", "foreign_novel": "Зарубежный роман",
    "foreign_other": "Зарубежная прочая", "foreign_publication": "Зарубежное издание",
    "foreign_su_publication": "Зарубежное издание (СССР)",
    "funfiction": "Фанфик", "garem": "Гарем",
    "geo_guide": "Географический путеводитель", "geo_guides": "Путеводители",
    "geography_book": "География", "geroicheskaya": "Героическая",
    "geroicheskoe_fentezi": "Героическое фэнтези", "gita": "Гита",
    "global_economy": "Глобальная экономика", "gothic_novel": "Готический роман",
    "great_story": "Великая история",
    "health_alt_medicine": "Нетрадиционная медицина", "health_men": "Мужское здоровье",
    "health_nutrition": "Питание и диеты", "health_psy": "Психология здоровья",
    "health_rel": "Здоровье и релаксация", "health_self_help": "Здоровье: самопомощь",
    "health_sex": "Сексуальное здоровье", "health_women": "Женское здоровье",
    "histor_military": "Военная история", "historical_adventure": "Исторические приключения",
    "historical_fantasy": "Историческое фэнтези",
    "history_australia": "История Австралии",
    "history_middle_east": "История Ближнего Востока",
    "history_military_science": "Военно-историческая наука",
    "home_collecting": "Коллекционирование", "home_handiwork": "Рукоделие",
    "home_metalwork": "Металлообработка", "home_mushrooms": "Грибы",
    "horror_usa": "Американские ужасы", "hronoopera": "Хроноопера",
    "humor_prose_contemporary": "Современная юмористическая проза",
    "in_retelling": "В пересказе", "in_verse": "В стихах",
    "industries": "Промышленность", "ironicheskie": "Иронические",
    "istoricheskaya": "Историческая",
    "istoricheskaya_literatura": "Историческая литература",
    "istoricheskoe": "Историческое",
    "istoriya_ekonomiki": "История экономики", "job_hunting": "Поиск работы",
    "kiberpank": "Киберпанк", "kinematograf_teatr": "Кинематограф и театр",
    "klassicheskaya_literatura_russkaya": "Русская классика",
    "klassicheskie": "Классические", "klassika_priklyuchenij": "Классика приключений",
    "knigi_detektivy_zarubezhnye": "Зарубежные детективы",
    "knigi_fentezi_zarubezhnye": "Зарубежное фэнтези",
    "knigi_o_voyne": "Книги о войне",
    "knigi_sovremennaya_proza_zarubezhnaya": "Зарубежная современная проза",
    "korotkiye": "Короткие", "kosmicheskaya": "Космическая",
    "kriminal_detective": "Криминальный детектив", "kriminologiya": "Криминология",
    "kulturologiya": "Культурология", "legkaya_proza": "Лёгкая проза",
    "limerick": "Лиммерик", "literature": "Литература",
    "literature_18": "Литература XVIII века", "literature_19": "Литература XIX века",
    "literature_20": "Литература XX века", "literature_21": "Литература XXI века",
    "literature_adv": "Приключенческая литература",
    "literature_antology": "Литературная антология",
    "literature_books": "Книги о литературе",
    "literature_british": "Британская литература",
    "literature_classics": "Классическая литература",
    "literature_critic": "Литературная критика",
    "literature_drama": "Литературная драма",
    "literature_erotica": "Эротическая литература",
    "literature_essay": "Литературное эссе",
    "literature_fairy": "Литературная сказка",
    "literature_history": "История литературы",
    "literature_men_advent": "Мужские приключения",
    "literature_poetry": "Поэзия",
    "literature_political": "Политическая литература",
    "literature_religion": "Религиозная литература",
    "literature_rus_classsic": "Русская классика",
    "literature_sea": "Морская литература",
    "literature_short": "Малая проза",
    "literature_su_classics": "Советская классика",
    "literature_usa": "Американская литература",
    "literature_war": "Военная литература",
    "literature_western": "Вестерн",
    "literature_women": "Женская литература",
    "literature_world": "Мировая литература",
    "local_lore_study": "Краеведение",
    "love_contrmporary": "Современные любовные романы",
    "lsf_humor": "Юмористическая фантастика", "lubov": "Любовь",
    "lyubovno_fantasticheskiye": "Любовно-фантастические",
    "magic_school": "Магическая школа", "magician_book": "Книга о магах",
    "management": "Менеджмент", "marketing": "Маркетинг",
    "mify_legendy_epos": "Мифы и легенды", "military": "Военное дело",
    "military_arts": "Военное искусство", "military_history": "Военная история",
    "military_memoirs": "Военные мемуары", "military_special": "Спецслужбы",
    "military_weapon": "Оружие", "modern_tale": "Современная сказка",
    "music_dancing": "Музыка и танцы", "music_history": "История музыки",
    "muzei_kollektsii": "Музеи и коллекции", "mystical_thriller": "Мистический триллер",
    "narrative": "Повествование", "nauchnaya": "Научная",
    "nauchno_populyarnaya_literatura": "Научно-популярная литература",
    "nauchpop_prochee": "Научпоп (прочее)", "nejrobiologiya": "Нейробиология",
    "nemetckij_yazyk": "Немецкий язык", "newspapers": "Газеты",
    "nigma": "Загадка", "nobel": "Нобелевская", "none": "Нет",
    "nonf_biography": "Non-fiction: биографии",
    "nonf_criticism": "Non-fiction: критика", "nonf_memory": "Non-fiction: мемуары",
    "nonf_military": "Non-fiction: военное", "nonf_publicism": "Non-fiction: публицистика",
    "nonfiction_crime": "Non-fiction: криминал", "nonfiction_edu": "Non-fiction: образование",
    "nonfiction_folklor": "Non-fiction: фольклор",
    "nonfiction_gender": "Non-fiction: гендер",
    "nonfiction_law": "Non-fiction: право",
    "nonfiction_philosophy": "Non-fiction: философия",
    "nonfiction_politics": "Non-fiction: политика",
    "nonfiction_social_sci": "Non-fiction: социология",
    "nonfiction_social_work": "Non-fiction: соцработа",
    "nonfiction_sociology": "Non-fiction: социология",
    "nonfiction_spec_group": "Non-fiction: спецгруппы",
    "nonfiction_true_accounts": "Non-fiction: реальные истории",
    "notes": "Заметки", "novel": "Роман",
    "nsf": "NSF", "null": "Нет",
    "obschaya_psihologiya": "Общая психология",
    "old_rus_publication": "Старое русское издание",
    "oratorskoye_iskusstvo": "Ораторское искусство",
    "org_behavior": "Организационное поведение",
    "ostrosyuzhetnyye": "Остросюжетные", "other": "Другое",
    "outdoors_conservation": "Охрана природы",
    "outdoors_fauna": "Фауна", "outdoors_hunt_fish": "Охота и рыбалка",
    "outdoors_nature_writing": "Природоведение",
    "outdoors_travel": "Путешествия",
    "palindromes": "Палиндромы", "paper_work": "Документоведение",
    "paranormal": "Паранормальное", "pedagogy_book": "Педагогика",
    "people": "Народы", "performance": "Перформанс",
    "periodic": "Периодика", "personal_finance": "Личные финансы",
    "photography": "Фотография",
    "poetry_for_classical": "Классическая поэзия",
    "poetry_for_modern": "Современная поэзия", "poeziya": "Поэзия",
    "policeyskie": "Полицейские", "popadantsy": "Попаданцы",
    "popadantsy_v_magicheskie_miry": "Попаданцы в магические миры",
    "popadantsy_vo_vremeni": "Попаданцы во времени",
    "popular_business": "Популярный бизнес",
    "populyarnaya_istoriya": "Популярная история",
    "populyarno": "Популярное", "porn": "Порно",
    "postcards": "Открытки", "priklucheniya": "Приключения",
    "priklucheniya_knigi": "Книги о приключениях", "proce": "Проза",
    "professional_law": "Профессиональное право",
    "proverbs": "Пословицы", "psf_etc": "Прочее",
    "psy_alassic": "Психология (классика)",
    "psy_sex_and_family": "Секс и семья (психология)",
    "psy_theraphy": "Психотерапия", "publicistika": "Публицистика",
    "puteshestviyah": "Путешествия", "rarity": "Редкость",
    "real_estate": "Недвижимость", "realrpg": "RealRPG",
    "ref_ref": "Справочная", "ref_self_tutor": "Самоучитель",
    "reference": "Справочная", "rel_boddizm": "Буддизм",
    "religion_budda": "Буддизм", "religion_east": "Восточные религии",
    "riddles": "Загадки", "roman": "Роман",
    "romance_multicultural": "Мультикультурный роман",
    "romance_romantic_suspense": "Романтический саспенс",
    "romance_sf": "Романтическая фантастика",
    "rus_publication": "Российское издание", "rusreal": "Русский реализм",
    "russian": "Русская литература",
    "russian_contemporary": "Современная русская литература",
    "russian_fantasy": "Русское фэнтези",
    "russkaya_literatura": "Русская литература",
    "russkaya_poeziya": "Русская поэзия", "russkoe": "Русское",
    "sagas": "Саги",
    "samorazvitiye_lichnostny_rost": "Саморазвитие и личностный рост",
    "scenarios": "Сценарии",
    "sci_biochem": "Биохимия", "sci_biophys": "Биофизика",
    "sci_build": "Строительство", "sci_business": "Бизнес-наука",
    "sci_cosmos": "Космос", "sci_crib": "Шпаргалки",
    "sci_economy": "Экономика", "sci_evolutionism": "Эволюционизм",
    "sci_fleet": "Флот", "sci_geo": "Геология",
    "sci_history_0": "История (до н.э.)", "sci_history_15": "История XV века",
    "sci_history_18": "История XVIII века", "sci_history_20": "История XX века",
    "sci_history_21": "История XXI века", "sci_history_4": "История IV века",
    "sci_juris": "Юриспруденция",
    "sci_medicine_alternative": "Альтернативная медицина",
    "sci_metal": "Металлургия", "sci_orgchem": "Органическая химия",
    "sci_oriental": "Востоковедение", "sci_paleontology": "Палеонтология",
    "sci_philology": "Филология", "sci_physchem": "Физхимия",
    "sci_psychology": "Психология (научная)", "sci_radio": "Радиотехника",
    "sci_religion": "Религиоведение",
    "sci_social_studies": "Обществознание",
    "sci_state": "Государство и право",
    "sci_tech_rockets": "Ракетостроение",
    "sci_textbook": "Учебник", "sci_theories": "Теории",
    "sci_transport": "Транспорт", "sci_veterinary": "Ветеринария",
    "science_archaeology": "Археология", "science_biolog": "Биология",
    "science_earth": "Науки о Земле",
    "science_history_philosophy": "История философии",
    "science_medicine": "Медицина", "science_psy": "Психология",
    "screenplays": "Киносценарии",
    "seksualnyye_rukovodstva": "Сексуальные руководства",
    "sexual_perversion": "Сексуальные перверсии",
    "sf_actoin": "Боевая фантастика", "sf_cyber_punk": "Киберпанк",
    "sf_etc": "Фантастика (прочее)",
    "sf_fantasy_action": "Боевое фэнтези",
    "sf_fantasy_fight": "Боевое фэнтези",
    "sf_fantasy_irony": "Ироническое фэнтези",
    "sf_gantasy": "Фэнтези",
    "sf_history_avant": "Авантюрная альтернативная история",
    "sf_lirpg": "ЛитРПГ", "sf_litlrpg": "ЛитРПГ",
    "sf_litprg": "ЛитРПГ", "sf_litrp": "ЛитРПГ",
    "sf_paleontological": "Палеонтологическая фантастика",
    "sf_realrpg": "RealRPG",
    "sf_sf_fantasy": "Фантастика/Фэнтези",
    "sf_sf_fantasy_city": "Городское фэнтези",
    "sf_technofantas": "Технофэнтези",
    "short_story": "Короткий рассказ", "skazki": "Сказки",
    "sketch": "Скетч", "slash": "Слэш",
    "small_business": "Малый бизнес", "socialnaya": "Социальная",
    "sociology_book": "Социология", "sovetskaya": "Советская",
    "sovremennye": "Современные", "sovremennyye": "Современные",
    "sr_fantasy": "Фэнтези", "stimpank": "Стимпанк",
    "stock": "Биржа", "story": "Рассказ",
    "su_publication": "Советское издание", "svaboda": "Свобода",
    "tale": "Сказка",
    "tbg_computers": "Учебная: компьютеры",
    "tbg_higher": "Учебная: высшее образование",
    "tbg_school": "Учебная: школа",
    "tbg_secondary": "Учебная: среднее образование",
    "teens_health": "Подростки: здоровье",
    "teens_history": "Подростки: история",
    "teens_literature": "Подростки: литература",
    "teens_sf": "Подростки: фантастика",
    "teens_social": "Подростки: социальное",
    "thriller_archaeological": "Археологический триллер",
    "thriller_police": "Полицейский триллер",
    "thriller_psychology": "Психологический триллер",
    "trade": "Торговля", "translation": "Перевод",
    "travel": "Путешествия", "travel_africa": "Путешествия: Африка",
    "travel_asia": "Путешествия: Азия",
    "travel_europe": "Путешествия: Европа",
    "travel_ex_ussr": "Путешествия: бывший СССР",
    "travel_guidebook_series": "Путеводители (серии)",
    "travel_lat_am": "Путешествия: Латинская Америка",
    "travel_notes": "Путевые заметки", "travel_polar": "Полярные путешествия",
    "travel_spec": "Спецпутешествия", "trillery": "Триллеры",
    "umori_i_satira": "Юмор и сатира",
    "umoristicheskaya_proza": "Юмористическая проза",
    "umoristicheskoe": "Юмористическое", "unfinished": "Незаконченное",
    "unfunished": "Незаконченное", "unknown": "Неизвестно",
    "unrecognised": "Неопознано",
    "upbringing_book": "Воспитание", "uzhasy": "Ужасы",
    "vampire_book": "Книги о вампирах", "vampiry": "Вампиры",
    "vaudeville": "Водевиль", "vers_libre": "Верлибр",
    "visual_arts": "Изобразительное искусство",
    "voennoe_delo_specsluzhby": "Военное дело и спецслужбы",
    "vsemirnaya_istoriya": "Всемирная история",
    "women_single": "Одинокие женщины",
    "zarubezhnaya": "Зарубежная литература",
    "zarubezhnaya_klassika": "Зарубежная классика",
    "zarubezhnaya_kultura_iskusstvo": "Зарубежная культура и искусство",
    "zarubezhnaya_obrazovatelnaya_literatura": "Зарубежная образовательная литература",
    "zarubezhnaya_poeziya": "Зарубежная поэзия",
    "zarubezhnaya_prikladnaya_literatura": "Зарубежная прикладная литература",
    "zarubezhnaya_starinnaya": "Зарубежная старинная литература",
    "zarubezhnye": "Зарубежные",
    "zarubezhnyy_umor": "Зарубежный юмор",
    "zarubezhnyye": "Зарубежные",
    "zombi_story": "Зомби-апокалипсис", "zoologiya": "Зоология",
}

# ─── GENRE GROUPS for sidebar navigation ──────────────────────────────────
GENRE_GROUPS = {
    "Фантастика и фэнтези": [
        "sf", "sf_history", "sf_action", "sf_epic", "sf_heroic", "sf_detective",
        "sf_cyberpunk", "sf_space", "sf_social", "sf_horror", "sf_techno",
        "sf_fantasy", "sf_fantasy_city", "sf_fantasy_heroic", "sf_fantasy_dark",
        "sf_fantasy_epic", "sf_fantasy_humor", "sf_fantasy_history",
        "sf_fantasy_parody", "sf_fantasy_horror",
        "sf_humor", "sf_mystic", "sf_irony", "sf_litrpg",
        "sf_postapocalyptic", "sf_stimpank", "sf_space_opera", "sf_althistory",
        "sf_technofantasy", "sf_erotic", "sf_all", "sf_rus", "sf_su", "sf_love",
        "sf_writing", "sf_actoin", "sf_cyber_punk", "sf_etc",
        "sf_fantasy_action", "sf_fantasy_fight", "sf_fantasy_irony",
        "sf_gantasy", "sf_history_avant", "sf_lirpg", "sf_litlrpg",
        "sf_litprg", "sf_litrp", "sf_paleontological", "sf_realrpg",
        "sf_sf_fantasy", "sf_sf_fantasy_city", "sf_technofantas",
        "fantasy", "fantasy_action", "fantasy_dark", "fantasy_epic", "fantasy_heroic",
        "fantasy_history", "fantasy_rus", "fantasy_det",
        "epic_fantasy", "heroic_fantasy", "dark_fantasy", "urban_fantasy",
        "slavic_fantasy", "popadanec", "popadancy",
        "litrpg", "steampunk", "cyberpunk", "postapocalyptic", "dystopia",
        "magic", "wuxia",
        "boevaya_fantastika", "boevoe_fentezi", "geroicheskaya",
        "geroicheskoe_fentezi", "kosmicheskaya", "kiberpank", "stimpank",
        "city_fantasy", "sr_fantasy", "cf_fantasy_city",
        "fantasy_alt_hist", "fantasy_fight", "fantasy_wuxia",
        "russian_fantasy", "historical_fantasy", "dragon_fantasy",
        "drakony", "dragons", "everyday_fantasy", "fairy_fantasy",
        "magic_school", "magician_book", "devushkimonstry", "boyar_anime",
        "realrpg", "dieselpunk", "apocalypse", "hronoopera",
        "popadantsy", "popadantsy_v_magicheskie_miry", "popadantsy_vo_vremeni",
    ],
    "Детективы и триллеры": [
        "det", "det_action", "det_police", "det_irony", "det_history",
        "det_espionage", "det_crime", "det_political", "det_maniac",
        "det_hard", "det_classic", "det_short", "det_artifact", "det_cozy",
        "det_other", "det_rus", "det_su", "det_all",
        "thriller", "thriller_psychological", "thriller_legal",
        "thriller_medical", "thriller_techno", "thriller_mystery",
        "horror", "horror_mystic", "horror_fantasy",
        "horror_occult", "horror_vampires", "mystery", "mistika",
        "action", "criminal_detective", "detectiv", "detective",
        "detective_fentezi", "detective_police", "detektive", "detektivnaya",
        "kriminal_detective", "policeyskie", "trillery", "uzhasy",
        "horror_usa", "thriller_archaeological", "thriller_police",
        "thriller_psychology", "mystical_thriller", "ostrosyuzhetnyye",
        "ironicheskie", "kriminologiya",
    ],
    "Любовные романы": [
        "love", "love_contemporary", "love_history", "love_erotica",
        "love_short", "love_detective", "love_fantasy", "love_sf",
        "love_sf_fantasy", "love_hard", "love_rus",
        "love_slash", "love_femslash",
        "erotica", "erotic_fantasy",
        "romance", "romance_contemporary", "romance_fantasy", "romance_historical",
        "eroticheskie_rasskazy", "eroticheskie_romany", "eroticheskoe_fantasy",
        "lubov", "lyubovno_fantasticheskiye", "romance_multicultural",
        "romance_romantic_suspense", "romance_sf", "love_contrmporary",
        "bdsm", "porn", "garem", "slash", "femslash",
        "seksualnyye_rukovodstva", "sexual_perversion", "adult",
    ],
    "Проза": [
        "prose", "prose_classic", "prose_history", "prose_contemporary",
        "prose_counter", "prose_rus_classic", "prose_su_classics",
        "prose_military", "prose_epic", "prose_abs", "prose_game",
        "prose_magic", "prose_sentimental", "prose_teen", "prose_neformatny",
        "prose_root", "contemporary", "drama", "tragedy", "classics",
        "proce", "rusreal", "russian", "russian_contemporary",
        "russkaya_literatura", "russkoe", "sovetskaya", "sovremennye", "sovremennyye",
        "klassicheskaya_literatura_russkaya", "klassicheskie", "legkaya_proza",
        "istoricheskaya", "istoricheskaya_literatura", "istoricheskoe",
        "socialnaya", "literature", "literature_classics", "literature_rus_classsic",
        "literature_su_classics", "literature_world", "literature_british",
        "literature_usa", "literature_contemporary", "literature_drama",
        "literature_poetry", "literature_war", "literature_history",
        "literature_short", "literature_18", "literature_19", "literature_20",
        "literature_21", "literature_women", "literature_men_advent",
        "literature_sea", "literature_western", "literature_fairy",
        "literature_erotica", "literature_essay", "literature_political",
        "literature_religion", "literature_critic", "literature_adv",
        "literature_antology", "literature_books",
        "drama_antique", "dramaturgy", "dramaturgy_all", "epic",
        "epistolary_fiction", "gothic_novel", "nobel", "novel", "roman",
        "sagas", "scenarios", "sketch", "vaudeville", "dorama",
    ],
    "Приключения": [
        "adv", "adv_animal", "adv_geo", "adv_history", "adv_indian",
        "adv_maritime", "adv_western", "adv_all", "adv_military",
        "adv_modern", "adv_rus", "adv_su",
        "adv_story", "adv_history_avant",
        "priklucheniya", "priklucheniya_knigi", "klassika_priklyuchenij",
        "historical_adventure", "puteshestviyah",
        "travel", "travel_africa", "travel_asia", "travel_europe",
        "travel_ex_ussr", "travel_guidebook_series", "travel_lat_am",
        "travel_notes", "travel_polar", "travel_spec",
        "outdoors_travel", "outdoors_conservation", "outdoors_fauna",
        "outdoors_hunt_fish", "outdoors_nature_writing",
        "geo_guide", "geo_guides", "geography_book",
    ],
    "Детская литература": [
        "child", "child_tale", "child_verse", "child_prose", "child_sf",
        "child_det", "child_adv", "child_education", "child_poetry",
        "child_animals", "child_history", "child_horror", "child_nature",
        "child_baby", "child_3", "child_4", "child_9",
        "child_all", "child_characters", "child_classical", "child_folklore",
        "child_rus", "child_su", "child_tale_rus",
        "age_elementary", "age_high", "age_wide",
        "kids", "young_adult", "ya",
        "children", "dlya_podrostkov", "detskaya_pazvivayushaya_literatura",
        "detskie_detektivy", "detskie_knigi_zarubezhnye",
        "skazki", "fairy_tale", "folk_tale", "tale", "narrative",
        "modern_tale", "fable",
    ],
    "Поэзия": [
        "poetry", "poetry_classic", "poetry_modern", "poem",
        "poetry_classical", "poetry_east", "poetry_rus_classical",
        "poetry_rus_modern", "poetry_su", "song_poetry", "lyrics", "verse",
        "poeziya", "russkaya_poeziya",
        "poetry_for_classical", "poetry_for_modern",
        "epic_poetry", "experimental_poetry", "limerick",
        "palindromes", "vers_libre", "folk_songs",
    ],
    "Юмор": [
        "humor", "humor_prose", "humor_verse", "humor_fantasy", "humor_det",
        "humor_all", "humor_anecdote", "humor_parody", "humor_satire",
        "humor_sf_fantasy", "comedy", "satire", "parody",
        "umori_i_satira", "umoristicheskaya_proza", "umoristicheskoe",
        "entert_humor", "family_humor", "entertainment", "comfort",
        "humor_prose_contemporary", "black_humor",
    ],
    "История и биографии": [
        "history", "history_modern", "history_world", "history_russia",
        "history_ancient", "history_middle", "history_war",
        "history_asia", "history_europe", "history_usa", "history_africa",
        "history_australia", "history_middle_east", "history_military_science",
        "biography", "biography_war", "biography_history", "biography_politic",
        "biography_art", "biography_sci",
        "biogr_arts", "biogr_historical", "biogr_leaders", "biogr_professionals",
        "biogr_sports", "biogr_travel",
        "memoirs", "autobiography",
        "biografii_i_memuary", "biograpy",
        "nonf_biography", "nonf_memory", "nonf_military",
        "vsemirnaya_istoriya", "istoriya_ekonomiki", "populyarnaya_istoriya",
        "histor_military", "military_history", "equ_history", "ci_history",
        "local_lore_study", "people", "rarity",
        "alexievich", "back_to_ussr", "belarus", "cossacks", "svaboda",
        "old_rus_publication", "su_publication", "ex_su_publication",
        "rus_publication",
    ],
    "Религия и эзотерика": [
        "religion", "religion_rel", "religion_esoterics", "religion_self",
        "religion_orthodoxy", "religion_islam", "religion_buddhism",
        "religion_christianity", "religion_paganism",
        "religion_catholicism", "religion_protestantism",
        "religion_hinduism", "religion_judaism",
        "religion_occult", "religion_spirituality",
        "buddhism", "atheism", "astrology",
        "esoterics", "esoterica", "esoteric", "palmistry",
    ],
    "Психология": [
        "psy", "psy_personal", "psy_relations", "psy_child", "psy_general",
        "psy_sex", "psy_social", "psy_childs", "psy_generic",
        "psychology", "popular_psychology", "self_help",
    ],
    "Наука и образование": [
        "sci", "sci_math", "sci_phys", "sci_chem", "sci_bio",
        "sci_astronomy", "sci_medicine", "sci_tech",
        "sci_history", "sci_popular", "sci_computer",
        "sci_linguistic", "sci_philosophy", "sci_culture",
        "sci_politics", "sci_economics", "sci_law",
        "sci_pedagogy", "sci_social",
        "sci_archeology", "sci_ecology", "sci_geography",
        "sci_botany", "sci_zoo", "sci_biology",
        "science", "physics", "biology", "philosophy",
    ],
    "Компьютеры и IT": [
        "comp", "comp_hard", "comp_soft", "comp_prog", "comp_web",
        "comp_net", "comp_db", "comp_os", "comp_games", "comp_security",
        "comp_www", "computers", "programming",
    ],
    "Дом и семья": [
        "home", "home_cooking", "home_garden", "home_handicraft", "home_pets",
        "home_sex", "home_entertain", "home_health", "home_sport",
        "home_diy", "home_family", "home_fashion", "home_beauty",
        "home_building", "home_child", "home_hunt", "home_tourism", "home_crafts",
        "cooking", "garden", "sport", "health", "medicine",
    ],
    "Деловая литература": [
        "business", "business_economics", "business_marketing", "business_management",
        "business_finance", "business_account", "business_job", "business_sales",
        "business_advertising", "business_realestate", "business_law",
    ],
    "Искусство и дизайн": [
        "design", "design_web", "design_graphic", "design_interior",
        "design_landscape", "design_industrial", "design_fashion",
        "art", "art_history", "music", "cinema", "theatre", "photo",
        "painting", "architecture",
        "ref_art", "ref_photo", "ref_music", "ref_theatre", "ref_cinema",
        "ref_arch", "ref_paint",
    ],
    "Справочная литература": [
        "ref", "ref_dict", "ref_encyc", "ref_guide", "ref_job",
        "ref_sci_popular",
    ],
    "Зарубежная литература": [
        "foreign_adventure", "foreign_contemporary", "foreign_detective",
        "foreign_fantasy", "foreign_love", "foreign_prose",
        "foreign_sf", "foreign_social", "foreign_military", "foreign_humor",
        "foreign_action", "foreign_antique", "foreign_children",
        "foreign_poetry", "foreign_psychology",
        "foreign_publicism", "foreign_religion",
    ],
    "Зарубежная литература": [
        "foreign_adventure", "foreign_contemporary", "foreign_detective",
        "foreign_fantasy", "foreign_love", "foreign_prose",
        "foreign_sf", "foreign_social", "foreign_military", "foreign_humor",
        "foreign_action", "foreign_antique", "foreign_children",
        "foreign_poetry", "foreign_psychology",
        "foreign_publicism", "foreign_religion",
        "foreign_business", "foreign_comp", "foreign_desc", "foreign_dramaturgy",
        "foreign_edu", "foreign_home", "foreign_language", "foreign_novel",
        "foreign_other", "foreign_publication", "foreign_su_publication",
        "zarubezhnaya", "zarubezhnaya_klassika", "zarubezhnaya_kultura_iskusstvo",
        "zarubezhnaya_obrazovatelnaya_literatura", "zarubezhnaya_poeziya",
        "zarubezhnaya_prikladnaya_literatura", "zarubezhnaya_starinnaya",
        "zarubezhnye", "zarubezhnyy_umor", "zarubezhnyye",
        "knigi_detektivy_zarubezhnye", "knigi_fentezi_zarubezhnye",
        "knigi_sovremennaya_proza_zarubezhnaya",
    ],
    "Антикварная литература": [
        "antique", "antique_european", "antique_east", "antique_myth",
        "antique_russian", "antique_ant", "antique_myths",
        "antichnaya", "drevnevostochnaya", "in_retelling",
    ],
    "Биографии и мемуары": [
        "biography", "biography_war", "biography_history", "biography_politic",
        "biography_art", "biography_sci", "biogr_arts", "biogr_historical",
        "biogr_leaders", "biogr_professionals", "biogr_sports", "biogr_travel",
        "memoirs", "autobiography", "biografii_i_memuary", "biograpy",
        "nonf_biography", "nonf_memory", "nonf_military",
    ],
    "Учебная литература": [
        "tbg_computers", "tbg_higher", "tbg_school", "tbg_secondary",
        "paper_work", "sci_textbook", "reference", "ref_self_tutor",
        "pedagogy_book", "upbringing_book",
    ],
    "Прочее": [
        "publicism", "fanfiction", "fan_translation",
        "network_literature", "samizdat",
        "beginning_authors", "autor_collection",
        "nonfiction", "non_fiction",
        "essay", "essays", "interview", "letters",
        "criticism", "literary_criticism", "journalism",
        "publicistika", "dokumentalnaya_literatura", "nonf_publicism",
        "nonfiction_crime", "nonfiction_edu", "nonfiction_folklor",
        "nonfiction_gender", "nonfiction_law", "nonfiction_philosophy",
        "nonfiction_politics", "nonfiction_social_sci", "nonfiction_social_work",
        "nonfiction_sociology", "nonfiction_spec_group", "nonfiction_true_accounts",
        "dissident", "periodic", "newspapers", "postcards", "notes",
        "collection", "compilation", "funfiction", "fanfiction_het",
        "translation", "computer_translation", "in_verse",
        "unfinished", "unfunished", "unrecognised", "unknown",
        "oratorskoye_iskusstvo", "none", "null", "other", "fiction", "ficion",
        "story", "great_story", "nigma", "ebook",
        "comics", "aphorisms", "aphorism_quote", "proverbs", "riddles",
    ],
}

def get_genre_group(code):
    for group_name, codes in GENRE_GROUPS.items():
        if code in codes:
            return group_name
    return "Другое"

def genre_name(code):
    """Flibusta genre code → readable Russian name."""
    return GENRE_NAMES.get(code, code)

# ─── WSL PATH CONVERTER ──────────────────────────────────────────────────

def to_wsl_path(p):
    """Привести путь к виду текущей ОС.

    Исторически функция называется to_wsl_path и безусловно превращала
    E:\\a\\b → /mnt/e/a/b, в том числе на чистом Windows, где таких путей
    не существует. В find_archive() она использовалась как «запасной»
    вариант поиска, из-за чего на Windows каждый поиск архива делал лишний
    os.path.isfile() по несуществующему пути.

    Теперь используется единый path_compat, который различает WSL и
    Windows и не трогает нативные Linux-пути.

    Имя функции сохранено: на неё ссылаются find_archive() и внешние вызовы.
    """
    if not p or not isinstance(p, str):
        return p
    try:
        from path_compat import to_native
        return to_native(p)
    except Exception:
        swallow.note('collection_module.to_wsl_path:L827')
        return p


def _path_variants(p):
    """Все разумные варианты пути для поиска файла на текущей ОС.

    Возвращает исходный путь плюс его нативную форму. Порядок важен:
    первый элемент — то, что вернул сервер/настройка, он и есть канонический.
    """
    if not p or not isinstance(p, str):
        return []
    out = [p]
    alt = to_wsl_path(p)
    if alt and alt != p:
        out.append(alt)
    return out

def set_db_path(path):
    global COLLECTION_DB, _OVERRIDE_DB, _db_initialized
    if not path or path == _DEFAULT_DB:
        _OVERRIDE_DB = None
        COLLECTION_DB = _DEFAULT_DB
    else:
        _OVERRIDE_DB = path
        COLLECTION_DB = path
    _db_initialized = False
    # Кэшированное RO-соединение belonged к ПРЕДЫДУЩЕЙ базе. Раньше здесь
    # ничего не сбрасывалось, и get_db() продолжал отдавать старое
    # соединение — после переключения БД интерфейс показывал данные
    # предыдущей базы (или падал), пока не перезапустишь сервер.
    _close_ro_conn()  # re-init schema on next connect
BATCH_SIZE = 50
MAX_WORKERS = 4
PAGE_SIZE = 50

FB2_NS = 'http://www.gribuser.ru/xml/fictionbook/2.0'
FB2 = {'fb': FB2_NS}

_scan_lock = threading.Lock()
_scan_thread = None
_scan_stop = threading.Event()
_db_initialized = False

# ─── DB ────────────────────────────────────────────────────────────────────

def _ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS archives (
            id INTEGER PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            size INTEGER,
            books_count INTEGER DEFAULT 0,
            base_path TEXT
        );
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            file_path TEXT NOT NULL,
            archive_id INTEGER NOT NULL,
            file_size INTEGER,
            lang TEXT,
            series_id INTEGER,
            seq_number TEXT,
            description TEXT,
            year TEXT,
            extra TEXT DEFAULT '{}',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (archive_id) REFERENCES archives(id)
        );
        CREATE TABLE IF NOT EXISTS authors (
            id INTEGER PRIMARY KEY,
            full_name TEXT UNIQUE NOT NULL,
            first_name TEXT,
            last_name TEXT,
            middle_name TEXT
        );
        CREATE TABLE IF NOT EXISTS genres (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS book_authors (
            book_id INTEGER,
            author_id INTEGER,
            PRIMARY KEY (book_id, author_id)
        );
        CREATE TABLE IF NOT EXISTS book_genres (
            book_id INTEGER,
            genre_id INTEGER,
            PRIMARY KEY (book_id, genre_id)
        );
        CREATE TABLE IF NOT EXISTS covers (
            book_id INTEGER PRIMARY KEY,
            image_data BLOB,
            mime_type TEXT DEFAULT 'image/jpeg'
        );
        CREATE TABLE IF NOT EXISTS book_content (
            book_id INTEGER PRIMARY KEY,
            content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scan_progress (
            id INTEGER PRIMARY KEY,
            archives_path TEXT,
            total_archives INTEGER DEFAULT 0,
            processed_archives INTEGER DEFAULT 0,
            total_books INTEGER DEFAULT 0,
            current_archive TEXT,
            status TEXT DEFAULT 'idle',
            started_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
        CREATE INDEX IF NOT EXISTS idx_books_archive ON books(archive_id);
        CREATE INDEX IF NOT EXISTS idx_books_series ON books(series_id);
        CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(full_name);
        CREATE INDEX IF NOT EXISTS idx_book_authors_book ON book_authors(book_id);
        CREATE INDEX IF NOT EXISTS idx_book_authors_author ON book_authors(author_id);
        CREATE INDEX IF NOT EXISTS idx_book_genres_book ON book_genres(book_id);
        CREATE INDEX IF NOT EXISTS idx_book_genres_genre ON book_genres(genre_id);
    """)
    for alt in [
        "ALTER TABLE archives ADD COLUMN base_path TEXT",
        "ALTER TABLE books ADD COLUMN year TEXT",
        "ALTER TABLE books ADD COLUMN extra TEXT DEFAULT '{}'",
        "ALTER TABLE genres ADD COLUMN name TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(alt)
        except sqlite3.OperationalError:
            swallow.note('collection_module._ensure_schema:L961')
            pass
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
                title, content_text,
                tokenize='unicode61 remove_diacritics 1'
            )
        """)
    except sqlite3.OperationalError:
        swallow.note('collection_module._ensure_schema:L971')
        pass
    conn.commit()
    global _db_initialized
    _db_initialized = True

_db_pool = None

# ─── READ-ONLY CONNECTION POOL ─────────────────────────────────────────────
# Внешняя база (flibusta.db, 690 тыс. книг) раньше обслуживалась ОДНИМ
# кэшированным соединением на весь модуль. Замер 2026-11-02: быстрый запрос,
# отправленный пока выполнялся тяжёлый, ждал его конца 6,14 с вместо 0,076 с —
# одна страница каталога блокировала всю остальную выборку. Теперь соединения
# выдаются из пула, как и для базы по умолчанию.
_RO_POOL_SIZE = 8
_db_ro_pool = None
_db_ro_pool_path = None

# PRAGMA для RO-соединения. Замер на реальной базе (скан titles по LIKE):
#   по умолчанию 252 мс, cache_size=-65536 (64 МБ) 148 мс, +temp_store=MEMORY.
# Кэш страниц тратится на 935-МБ таблицу books, поэтому 64 МБ на соединение —
# первое, что заметно; mmap оставлен умеренным (256 МБ), чтобы 8 соединений
# не забирали гигабайты адресного пространства впустую.
_RO_PRAGMAS = (
    "PRAGMA cache_size = -65536",
    "PRAGMA mmap_size = 268435456",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA busy_timeout = 5000",
)


def _sqlite_ro_uri(db_path: str) -> str:
    """Правильно собрать SQLite-URI для БД в режиме только чтение.

    Раньше здесь было 'file://' + db_path, что на Windows даёт
        file://C:\\data\\flibusta.db?mode=ro&immutable=1
    и SQLite отвечает «invalid uri authority: C:\\...» — внешняя БД
    не открывалась, а интерфейс отдавал 500-ю ошибку.

    Требования к URI файловой БД:
      * file:/// — именно три слэша для абсолютного локального пути;
      * разделители — прямые слэши (обратные в URI недопустимы, а
        «C:» после file:// читается как authority, т.е. «хост»);
      * спецсимволы пути (пробел, #, ?) percent-экранируются.

    Флаг immutable=1 убран намеренно: он запрещает SQLite проверять
    изменения файла и пропускает WAL, из-за чего база в режиме WAL
    читается как повреждённая. Остаётся обычный mode=ro.
    """
    from urllib.parse import quote
    p = str(db_path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p          # C:/data -> /C:/data
    # safe='/:' — двоеточие диска и разделители оставляем как есть,
    # экранируем только реальные спецсимволы (пробел, #, ?, %).
    return "file://" + quote(p, safe="/:") + "?mode=ro"


class _RoPool:
    """Пул read-only соединений к внешней базе.

    Поколение (generation) защищает от возврата соединения в уже отброшенный
    пул: после смены пути БД старые соединения должны закрываться, а не
    попадать обратно в очередь.
    """

    def __init__(self, db_path: str, size: int = _RO_POOL_SIZE):
        self.db_path = db_path
        self.size = size
        self._idle = queue.Queue(maxsize=size)
        self._lock = threading.Lock()
        self._live = 0
        self._closed = False
        self._generation = 0
        # sqlite3.Connection — C-тип без __dict__, атрибут на него не повесить:
        # поколение храним по id(conn) и снимаем при возврате.
        self._conn_gen = {}

    # ── внутреннее ────────────────────────────────────────────────────────
    def _new_conn(self):
        url = _sqlite_ro_uri(self.db_path)
        try:
            conn = sqlite3.connect(url, timeout=5, uri=True, check_same_thread=False)
        except sqlite3.OperationalError as e:
            # БД в режиме WAL может не открыться read-only, если рядом
            # нет -shm-файла. Откатываемся на обычное подключение.
            print(f"[collection] RO open failed ({e}), fallback to RW")
            conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in _RO_PRAGMAS:
            try:
                conn.execute(pragma)
            except Exception:
                swallow.note('collection_module._new_conn:L1064')
                pass
        conn.execute("SELECT 1")
        return conn

    # ── публичное ─────────────────────────────────────────────────────────
    def acquire(self, timeout: float = 30.0):
        with self._lock:
            if self._closed:
                raise RuntimeError("RO pool is closed")
            generation = self._generation
            try:
                conn = self._idle.get_nowait()
                # Соединение из очереди тоже принадлежит текущему поколению:
                # без этой записи release() считал его чужим и закрывал, и пул
                # открывал новое подключение на каждое получение.
                self._conn_gen[id(conn)] = generation
                return conn
            except queue.Empty:
                # Пул пуст — это штатная ветка «открыть новое соединение»,
                # а не сбой: предупреждение писалось при каждом холодном
                # запросе и ничего не сообщало.
                pass
            can_open = self._live < self.size
            if can_open:
                self._live += 1
        if can_open:
            try:
                conn = self._new_conn()
            except Exception:
                with self._lock:
                    self._live -= 1
                raise
            with self._lock:
                self._conn_gen[id(conn)] = generation
            return conn
        # Все соединения заняты: ждём, пока одно вернётся.
        conn = self._idle.get(timeout=timeout)
        return conn

    def release(self, conn):
        if conn is None:
            return
        stale = False
        with self._lock:
            gen = self._conn_gen.pop(id(conn), None)
            if self._closed or gen != self._generation:
                stale = True
                self._live = max(0, self._live - 1)
        if stale:
            try:
                conn.close()
            except Exception:
                swallow.note('collection_module.release:L1115')
                pass
            return
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            swallow.note('collection_module.release:L1121')
            with self._lock:
                self._live = max(0, self._live - 1)
            try:
                conn.close()
            except Exception:
                swallow.note('collection_module.release:L1127')
                pass

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._live = 0
            self._conn_gen.clear()
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                swallow.note('collection_module.close:L1140')
                break
            try:
                conn.close()
            except Exception:
                swallow.note('collection_module.close:L1145')
                pass


_ro_pool_lock = threading.Lock()


def _get_ro_pool(db_path: str) -> _RoPool:
    """Пул для пути; при смене базы старый закрывается."""
    global _db_ro_pool, _db_ro_pool_path
    with _ro_pool_lock:
        if _db_ro_pool is None or _db_ro_pool_path != db_path:
            old = _db_ro_pool
            _db_ro_pool = _RoPool(db_path)
            _db_ro_pool_path = db_path
            if old is not None:
                old.close()
                print(f"[collection] RO pool switched: {db_path}")
            else:
                print(f"[collection] RO pool created: {db_path}")
        return _db_ro_pool


def _close_ro_conn():
    """Закрыть пул RO-соединений (при смене пути БД)."""
    global _db_ro_pool, _db_ro_pool_path
    with _ro_pool_lock:
        old = _db_ro_pool
        _db_ro_pool = None
        _db_ro_pool_path = None
    if old is not None:
        old.close()


# ─── SEARCH SIDECAR ────────────────────────────────────────────────────────
# Поиск по внешней базе раньше был `WHERE title LIKE '%q%' OR author LIKE '%q%'`
# поверх LEFT JOIN: планировщик сканировал 690 тыс. строк books и подтягивал
# авторов к каждой, живой замер давал 9,6–15,7 с. Индексы на внешнюю базу
# создать нельзя (она открыта только для чтения), поэтому рядом с collection.db
# строится маленький trigram-индекс одних названий. Trigram выбран потому, что
# он сохраняет текущую семантику («подстрока, без учёта регистра», в том числе
# для кириллицы), в отличие от unicode61, который ищет только по словам.
# Индекс ленивый: первый поиск запускает сборку в фоне, до её конца работает
# обычная SQL-выборка. Подпись (путь + размер + mtime) защищает от чтения
# индекса, построенного по другой базе.
_SEARCH_SIDECAR_VERSION = "trigram2"
_SEARCH_INDEX_PATH = os.path.join(os.path.dirname(_DEFAULT_DB), "collection_search.db")
_search_index_lock = threading.Lock()
_search_index_building = set()
_search_index_retry_after = {}


def _db_signature(db_path):
    try:
        st = os.stat(db_path)
    except OSError:
        swallow.note('collection_module._db_signature:L1201')
        return ""
    return ("%s|%s|%s|%s" % (_SEARCH_SIDECAR_VERSION, os.path.abspath(db_path),
                             st.st_size, st.st_mtime_ns))


def _open_search_index():
    return sqlite3.connect(_SEARCH_INDEX_PATH, timeout=2, check_same_thread=False)


def _search_index_ready(signature):
    """Открытое соединение с готовым индексом или None."""
    if not signature or not os.path.isfile(_SEARCH_INDEX_PATH):
        return None
    conn = None
    try:
        conn = _open_search_index()
        sig = conn.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
        ver = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        if sig and sig[0] == signature and ver and ver[0] == _SEARCH_SIDECAR_VERSION:
            return conn
    except Exception:
        swallow.note('collection_module._search_index_ready:L1223')
        pass
    try:
        if conn is not None:
            conn.close()
    except Exception:
        swallow.note('collection_module._search_index_ready:L1229')
        pass
    return None


def _build_search_index(db_path, signature):
    """Собрать trigram-индекс названий во временный файл и подменить им старый."""
    tmp = "%s.tmp.%s.%s" % (_SEARCH_INDEX_PATH, os.getpid(), threading.get_ident())
    db = src = None
    try:
        db = sqlite3.connect(tmp)
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.execute("CREATE VIRTUAL TABLE titles USING fts5(title, tokenize='trigram')")
        db.execute("CREATE VIRTUAL TABLE authors_fts USING fts5(full_name, tokenize='trigram')")
        src = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True, timeout=10)
        cur = src.execute("SELECT id, title FROM books WHERE title IS NOT NULL")
        total = 0
        while True:
            chunk = cur.fetchmany(5000)
            if not chunk:
                break
            db.executemany("INSERT INTO titles(rowid, title) VALUES (?, ?)", chunk)
            total += len(chunk)
        acur = src.execute("SELECT id, full_name FROM authors WHERE full_name IS NOT NULL")
        authors = 0
        while True:
            chunk = acur.fetchmany(5000)
            if not chunk:
                break
            db.executemany("INSERT INTO authors_fts(rowid, full_name) VALUES (?, ?)", chunk)
            authors += len(chunk)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                       [("signature", signature),
                        ("version", _SEARCH_SIDECAR_VERSION),
                        ("books", str(total)),
                        ("authors", str(authors))])
        db.commit()
        db.close(); db = None
        src.close(); src = None
        # На Windows подмена занятого файла даёт PermissionError: поисковые
        # соединения короткоживущие, поэтому нескольких повторов достаточно.
        for _attempt in range(20):
            try:
                os.replace(tmp, _SEARCH_INDEX_PATH)
                with _search_index_lock:
                    _search_index_retry_after.pop(signature, None)
                print(f"[collection] search index ready: {total} titles")
                return True
            except PermissionError:
                swallow.note('collection_module._build_search_index:L1280')
                time.sleep(0.05)
        print("[collection] search index: file is busy, will retry on next search")
        with _search_index_lock:
            _search_index_retry_after[signature] = time.time() + 30
        return False
    except Exception as e:
        print(f"[collection] search index build failed: {e}")
        with _search_index_lock:
            _search_index_retry_after[signature] = time.time() + 60
        return False
    finally:
        for c in (db, src):
            try:
                if c is not None:
                    c.close()
            except Exception:
                swallow.note('collection_module._build_search_index:L1297')
                pass
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            swallow.note('collection_module._build_search_index:L1303')
            pass
        with _search_index_lock:
            _search_index_building.discard(signature)


def _ensure_search_index(db_path, signature):
    """Соединение с индексом; при отсутствии/устаревании запускает сборку."""
    conn = _search_index_ready(signature)
    if conn is not None:
        return conn
    # Локальной базе хватает собственной books_fts; побочный индекс нужен
    # только для внешней (flibusta) базы.
    if db_path == _DEFAULT_DB or not signature:
        return None
    with _search_index_lock:
        if _search_index_retry_after.get(signature, 0) > time.time():
            return None
        start = signature not in _search_index_building
        if start:
            _search_index_building.add(signature)
    if start:
        print(f"[collection] search index building: {db_path}")
        threading.Thread(target=_build_search_index, args=(db_path, signature),
                         name="collection-search-index", daemon=True).start()
    return None


def get_default_db():
    """Всегда использует collection.db на ext4 (для записи) через SQLitePool."""
    global _db_pool
    if _db_pool is None:
        _db_pool = worker_pool.SQLitePool(_DEFAULT_DB, max_connections=5, timeout=5)
    return _db_pool.acquire()

def get_db():
    global _db_initialized
    db_path = COLLECTION_DB
    # Если БД по умолчанию (на ext4) — всё работает
    if db_path == _DEFAULT_DB:
        conn = get_default_db()
        if not _db_initialized:
            try:
                _ensure_schema(conn)
                _db_initialized = True
            except Exception as e:
                print(f"[collection] schema init failed: {e}")
        return conn
    conn = _get_ro_pool(db_path).acquire()
    _db_initialized = True
    return conn

@contextmanager
def db_connection():
    db_path = COLLECTION_DB
    conn = get_db()
    try:
        yield conn
    finally:
        if db_path == _DEFAULT_DB:
            if _db_pool is not None:
                _db_pool.release(conn)
            else:
                conn.close()
        else:
            pool = _db_ro_pool
            if pool is not None:
                pool.release(conn)
            else:
                try:
                    conn.close()
                except Exception:
                    swallow.note('collection_module.db_connection:L1375')
                    pass

def init_db():
    conn = get_default_db()
    try:
        _ensure_schema(conn)
    finally:
        if _db_pool is not None:
            _db_pool.release(conn)
        else:
            conn.close()

# ─── SCAN PROGRESS ─────────────────────────────────────────────────────────

def get_scan_progress():
    conn = get_default_db()
    try:
        row = conn.execute("SELECT * FROM scan_progress WHERE id = 1").fetchone()
        return dict(row) if row else None
    finally:
        if _db_pool is not None:
            _db_pool.release(conn)
        else:
            conn.close()

def update_scan_progress(path='', total=-1, processed=-1, books=-1, current='', status='running'):
    conn = get_default_db()
    try:
        ts = datetime.now().isoformat()
        existing = conn.execute("SELECT * FROM scan_progress WHERE id=1").fetchone()
        if existing:
            path = path if path else existing['archives_path']
            total = total if total >= 0 else existing['total_archives']
            processed = processed if processed >= 0 else existing['processed_archives']
            books = books if books >= 0 else existing['total_books']
            current = current if current else existing['current_archive']
        conn.execute("""
            INSERT OR REPLACE INTO scan_progress
            (id, archives_path, total_archives, processed_archives, total_books,
             current_archive, status, updated_at, started_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT started_at FROM scan_progress WHERE id=1), ?))
        """, (path, total, processed, books, current, status, ts, ts))
        conn.commit()
    finally:
        if _db_pool is not None:
            _db_pool.release(conn)
        else:
            conn.close()

def clear_scan_progress():
    conn = get_default_db()
    try:
        conn.execute("DELETE FROM scan_progress WHERE id = 1")
        conn.commit()
    finally:
        if _db_pool is not None:
            _db_pool.release(conn)
        else:
            conn.close()

# ─── FB2 PARSING ───────────────────────────────────────────────────────────

def _f(elem, *paths):
    for p in paths:
        if isinstance(p, tuple):
            path, ns = p[0], p[1] if len(p) > 1 else None
            if isinstance(ns, str):
                ns = {'fb': ns}
            r = elem.find(path, ns) if ns else elem.find(path)
        else:
            r = elem.find(p)
        if r is not None:
            return r
    return None

def extract_cover_from_fb2(root):
    ti = _f(root, ('.//fb:title-info', FB2), './/title-info')
    if ti is None:
        return None, None
    coverpage = _f(ti, ('fb:coverpage', FB2), 'coverpage')
    if coverpage is None:
        return None, None
    img = _f(coverpage, ('fb:image', FB2), 'image')
    if img is None:
        return None, None
    href = (img.get('{http://www.w3.org/1999/xlink}href', '')
            or img.get('l:href', '') or img.get('href', ''))
    if not href:
        return None, None
    img_id = href.lstrip('#')
    if not img_id:
        return None, None
    binary = _f(root, (f'.//fb:binary[@id="{img_id}"]', FB2),
                f'.//binary[@id="{img_id}"]')
    if binary is not None and binary.text:
        try:
            data = base64.b64decode(binary.text.strip())
            mime = binary.get('content-type', 'image/jpeg')
            return data, mime
        except Exception:
            swallow.note('collection_module.extract_cover_from_fb2:L1477')
            pass
    return None, None

def extract_body_text(root):
    texts = []
    for body in (root.findall('.//fb:body', FB2) or root.findall('.//body') or []):
        for elem in body.iter():
            if isinstance(elem.tag, str):
                if elem.text:
                    t = elem.text.strip()
                    if t:
                        texts.append(t)
                if elem.tail:
                    t = elem.tail.strip()
                    if t:
                        texts.append(t)
    return '\n'.join(texts)

def parse_fb2(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        swallow.note('collection_module.parse_fb2:L1500')
        return None

    data = {
        'title': '', 'authors': [], 'genres': [], 'series': None,
        'seq_number': None, 'lang': None, 'description': '',
        'body_text': '', 'cover_data': None, 'cover_mime': None, 'extra': {},
    }

    ti = _f(root, ('.//fb:title-info', FB2), './/title-info')
    if ti is not None:
        bt = _f(ti, ('fb:book-title', FB2), 'book-title')
        if bt is not None and bt.text:
            data['title'] = bt.text.strip()

        for author in (ti.findall('fb:author', FB2) or ti.findall('author') or []):
            fn = _f(author, ('fb:first-name', FB2), 'first-name')
            ln = _f(author, ('fb:last-name', FB2), 'last-name')
            mn = _f(author, ('fb:middle-name', FB2), 'middle-name')
            parts = [
                (ln.text.strip() if ln is not None and ln.text else ''),
                (fn.text.strip() if fn is not None and fn.text else ''),
                (mn.text.strip() if mn is not None and mn.text else ''),
            ]
            full = ' '.join(p for p in parts if p)
            if full:
                data['authors'].append({
                    'full_name': full,
                    'first_name': parts[1],
                    'last_name': parts[0],
                    'middle_name': parts[2],
                })

        translators = []
        for tr in (ti.findall('fb:translator', FB2) or ti.findall('translator') or []):
            fn = _f(tr, ('fb:first-name', FB2), 'first-name')
            ln = _f(tr, ('fb:last-name', FB2), 'last-name')
            tr_parts = [
                (ln.text.strip() if ln is not None and ln.text else ''),
                (fn.text.strip() if fn is not None and fn.text else ''),
            ]
            tr_full = ' '.join(p for p in tr_parts if p)
            if tr_full:
                translators.append(tr_full)
        if translators:
            data['extra']['translators'] = translators

        for genre in (ti.findall('fb:genre', FB2) or ti.findall('genre') or []):
            if genre.text:
                data['genres'].append(genre.text.strip())

        seq = _f(ti, ('fb:sequence', FB2), 'sequence')
        if seq is not None:
            data['series'] = seq.get('name', '').strip() or None
            data['seq_number'] = seq.get('number', '').strip() or None

        lang = _f(ti, ('fb:lang', FB2), 'lang')
        if lang is not None and lang.text:
            data['lang'] = lang.text.strip()

        ann = _f(ti, ('fb:annotation', FB2), 'annotation')
        if ann is not None:
            ann_texts = []
            for p in (ann.findall('.//fb:p', FB2) or ann.findall('.//p') or []):
                if p.text:
                    ann_texts.append(p.text.strip())
            data['description'] = ' '.join(ann_texts)

        kw = _f(ti, ('fb:keywords', FB2), 'keywords')
        if kw is not None and kw.text:
            data['extra']['keywords'] = kw.text.strip()

        dt = _f(ti, ('fb:date', FB2), 'date')
        if dt is not None and dt.text:
            data['extra']['date'] = dt.text.strip()

    pi = _f(root, ('.//fb:publish-info', FB2), './/publish-info')
    if pi is not None:
        for field, key in [
            ('fb:publisher', 'publisher'), ('publisher', 'publisher'),
            ('fb:city', 'city'), ('city', 'city'),
            ('fb:year', 'year'), ('year', 'year'),
            ('fb:isbn', 'isbn'), ('isbn', 'isbn'),
        ]:
            el = _f(pi, (field, FB2) if field.startswith('fb:') else (field,))
            if el is not None and el.text:
                data['extra'][key] = el.text.strip()

    di = _f(root, ('.//fb:document-info', FB2), './/document-info')
    if di is not None:
        for field, key in [
            ('fb:id', 'doc_id'), ('id', 'doc_id'),
            ('fb:version', 'version'), ('version', 'version'),
        ]:
            el = _f(di, (field, FB2) if field.startswith('fb:') else (field,))
            if el is not None and el.text:
                data['extra'][key] = el.text.strip()

    data['body_text'] = extract_body_text(root)
    data['cover_data'], data['cover_mime'] = extract_cover_from_fb2(root)
    return data

# ─── ARCHIVE LOCATION ──────────────────────────────────────────────────────

# Недоступный сетевой каталог (WSL-шара) «висит» на каждом os.path.isfile:
# листинг страницы проверяет 50 архивов, и живой замер давал 2 с на запрос.
# Отрицательный ответ кэшируем на минуту — за это время шара либо вернётся
# (кэш истечёт и попробуем снова), либо останется недоступной, и листинг
# перестаёт платить за каждую книгу.
_BASE_REACH_TTL = 300.0
_base_reach_cache = {}


def _base_reachable(path):
    now = time.time()
    cached = _base_reach_cache.get(path)
    if cached and cached[0] > now:
        return cached[1]
    try:
        ok = os.path.isdir(path)
    except OSError:
        ok = False
    _base_reach_cache[path] = (time.time() + _BASE_REACH_TTL, ok)
    return ok


def _archive_base_paths():
    """Все каталоги, где может лежать архив: из БД и из прогресса скана."""
    bases = set()
    try:
        with db_connection() as conn:
            for row in conn.execute(
                    "SELECT DISTINCT base_path FROM archives "
                    "WHERE base_path IS NOT NULL AND base_path != ''"):
                bases.update(_path_variants(row[0]))
    except Exception:
        swallow.note('collection_module._archive_base_paths:L1636')
    try:
        sp = get_scan_progress()
        if sp and sp.get('archives_path'):
            bases.update(_path_variants(sp['archives_path']))
    except Exception:
        swallow.note('collection_module._archive_base_paths:L1642')
    return sorted(bases)


def warm_archive_bases():
    """Заранее измерить доступность каталогов архивов.

    Недоступная WSL-шара висит на `os.path.isdir` около секунды на каждый
    путь, а листинг проверяет до четырёх путей — первый заход после старта
    стоил 8 с. Прогрев платит эту цену в фоне, до первого запроса каталога;
    сами запросы читают только кэш.
    """
    bases = _archive_base_paths()
    if not bases:
        return 0
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(bases))) as pool:
        list(pool.map(_base_reachable, bases))
    return len(bases)


def find_archive(filename, archives_dir=None, fast=False):
    """Найти архив. fast=True — только заведомо доступные каталоги.

    Чтение книги (fast=False) обязано попытаться даже после неудачной
    проверки: шару могли поднять минуту назад, и книга должна открыться.
    Листингу же достаточно кэша доступности, иначе каждая страница платит
    сетевыми таймаутами.
    """
    def skip(base):
        return fast and not _base_reachable(base)

    with db_connection() as conn:
        row = conn.execute(
            "SELECT base_path FROM archives WHERE filename = ?", (filename,)
        ).fetchone()
        if row and row['base_path']:
            raw = row['base_path']
            for base in _path_variants(raw):
                if skip(base):
                    continue
                p = os.path.join(base, filename)
                if os.path.isfile(p):
                    return p

    if archives_dir:
        for ad in _path_variants(archives_dir):
            if skip(ad):
                continue
            direct = os.path.join(ad, filename)
            if os.path.isfile(direct):
                return direct

    sp = get_scan_progress()
    if sp and sp['archives_path']:
        for base in _path_variants(sp['archives_path']):
            if skip(base):
                continue
            p = os.path.join(base, filename)
            if os.path.isfile(p):
                return p

    if archives_dir:
        for ad in _path_variants(archives_dir):
            if fast:
                if skip(ad):
                    continue
            elif not os.path.isdir(ad):
                continue
            for root_dir, _, files in os.walk(ad):
                if filename in files:
                    return os.path.join(root_dir, filename)
    return None

# ─── SCANNING ──────────────────────────────────────────────────────────────

def _insert_batch(conn, buffer, store_content=False):
    for data, file_path, file_size, archive_id in buffer:
        series_id = None
        if data['series']:
            conn.execute("INSERT OR IGNORE INTO series (name) VALUES (?)", (data['series'],))
            row = conn.execute("SELECT id FROM series WHERE name = ?", (data['series'],)).fetchone()
            if row:
                series_id = row[0]

        extra_json = json.dumps(data.get('extra', {}), ensure_ascii=False)
        conn.execute("""
            INSERT INTO books (title, file_path, archive_id, file_size,
                               lang, series_id, seq_number, description, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (data['title'], file_path, archive_id, file_size,
              data['lang'], series_id, data['seq_number'], data['description'], extra_json))
        book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for author in data['authors']:
            conn.execute(
                "INSERT OR IGNORE INTO authors (full_name, first_name, last_name, middle_name) "
                "VALUES (?, ?, ?, ?)",
                (author['full_name'], author['first_name'],
                 author['last_name'], author['middle_name']))
            author_id = conn.execute(
                "SELECT id FROM authors WHERE full_name = ?", (author['full_name'],)
            ).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO book_authors (book_id, author_id) VALUES (?, ?)",
                         (book_id, author_id))

        for genre in data['genres']:
            conn.execute("INSERT OR IGNORE INTO genres (code) VALUES (?)", (genre,))
            genre_id = conn.execute(
                "SELECT id FROM genres WHERE code = ?", (genre,)
            ).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)",
                         (book_id, genre_id))

        if store_content and data['body_text']:
            conn.execute("INSERT OR IGNORE INTO book_content (book_id, content) VALUES (?, ?)",
                         (book_id, data['body_text']))

        if data['cover_data']:
            conn.execute(
                "INSERT OR IGNORE INTO covers (book_id, image_data, mime_type) VALUES (?, ?, ?)",
                (book_id, data['cover_data'], data['cover_mime']))

def list_zip_files(dir_path):
    files = []
    try:
        for f in os.listdir(dir_path):
            if f.lower().endswith('.zip'):
                files.append(os.path.join(dir_path, f))
    except (PermissionError, OSError):
        swallow.note('collection_module.list_zip_files:L1693')
        pass
    return sorted(files)

def scan_archives(archives_path, progress_callback=None, store_content=False):
    if not os.path.isdir(archives_path):
        msg = f"Папка не найдена: {archives_path}"
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    zip_files = list_zip_files(archives_path)
    if not zip_files:
        msg = f"ZIP-архивы не найдены в {archives_path}"
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    total_archives = len(zip_files)
    init_db()
    update_scan_progress(path=archives_path, total=total_archives, status='running')

    processed = 0
    total_books = 0

    for idx, zip_path in enumerate(zip_files, 1):
        if _scan_stop.is_set():
            update_scan_progress(status='stopped')
            return

        zname = os.path.basename(zip_path)
        if progress_callback:
            progress_callback(zname, idx - 1, total_archives, total_books, 'scanning')

        with db_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM archives WHERE filename = ?", (zname,)
            ).fetchone()
            if existing:
                count = conn.execute(
                    "SELECT books_count FROM archives WHERE id = ?", (existing['id'],)
                ).fetchone()['books_count']
                total_books += count
                processed += 1
                if progress_callback:
                    progress_callback(zname, processed, total_archives, total_books, 'skipped')
                continue

            conn.execute(
                "INSERT INTO archives (filename, size, base_path) VALUES (?, ?, ?)",
                (zname, os.path.getsize(zip_path), archives_path))
            archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            books_in_archive = 0
            buffer = []

            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for info in zf.infolist():
                        if not info.filename.lower().endswith(('.fb2', '.fb2.zip')):
                            continue
                        try:
                            xml_bytes = zf.read(info.filename)
                        except Exception:
                            swallow.note('collection_module.scan_archives:L1757')
                            continue

                        if info.filename.lower().endswith('.fb2.zip'):
                            try:
                                with zipfile.ZipFile(BytesIO(xml_bytes)) as inner:
                                    inner_names = inner.namelist()
                                    if inner_names:
                                        xml_bytes = inner.read(inner_names[0])
                            except Exception:
                                swallow.note('collection_module.scan_archives:L1767')
                                pass

                        data = parse_fb2(xml_bytes)
                        if not data or not data['title']:
                            continue

                        buffer.append((data, info.filename, info.file_size, archive_id))
                        books_in_archive += 1
                        if len(buffer) >= BATCH_SIZE:
                            _insert_batch(conn, buffer, store_content)
                            buffer = []

                    if buffer:
                        _insert_batch(conn, buffer, store_content)

                conn.execute("UPDATE archives SET books_count = ? WHERE id = ?",
                             (books_in_archive, archive_id))
                conn.commit()
                total_books += books_in_archive
                processed += 1
                if progress_callback:
                    progress_callback(zname, processed, total_archives, total_books, 'completed')

            except zipfile.BadZipFile:
                # архив снят из БД и помечен 'error' — но почему именно, без этой
                # строки в логе неотличимо от любого другого битого zip
                swallow.note('collection_module.scan_archives:L1792')
                conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
                conn.commit()
                if progress_callback:
                    progress_callback(zname, processed, total_archives, total_books, 'error')
            except Exception:
                swallow.note('collection_module.scan_archives:L1800')
                conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
                conn.commit()
                if progress_callback:
                    progress_callback(zname, processed, total_archives, total_books, 'error')

    if progress_callback:
        progress_callback('', processed, total_archives, total_books, 'completed')
    update_scan_progress(status='completed')

def scan_archives_parallel(archives_path, progress_callback=None, store_content=False):
    if not os.path.isdir(archives_path):
        msg = f"Папка не найдена: {archives_path}"
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    zip_files = list_zip_files(archives_path)
    if not zip_files:
        msg = f"ZIP-архивы не найдены в {archives_path}"
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    total_archives = len(zip_files)
    init_db()
    update_scan_progress(path=archives_path, total=total_archives, status='running')

    with db_connection() as conn:
        processed_set = set()
        for row in conn.execute("SELECT filename FROM archives").fetchall():
            processed_set.add(row['filename'])

    remaining = [z for z in zip_files if os.path.basename(z) not in processed_set]
    skipped = len(zip_files) - len(remaining)
    total_books = 0
    if skipped > 0:
        with db_connection() as conn:
            row = conn.execute("SELECT SUM(books_count) FROM archives").fetchone()
            total_books = row[0] or 0

    processed = skipped
    processed_lock = threading.Lock()
    books_lock = threading.Lock()

    def process_archive(zip_path):
        nonlocal total_books, processed
        zname = os.path.basename(zip_path)
        if progress_callback:
            with processed_lock:
                progress_callback(zname, processed, total_archives, total_books, 'scanning')

        with db_connection() as conn:
            conn.execute(
                "INSERT INTO archives (filename, size, base_path) VALUES (?, ?, ?)",
                (zname, os.path.getsize(zip_path), archives_path))
            archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            books_in_archive = 0
            buffer = []

            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for info in zf.infolist():
                        if not info.filename.lower().endswith(('.fb2', '.fb2.zip')):
                            continue
                        try:
                            xml_bytes = zf.read(info.filename)
                        except Exception:
                            swallow.note('collection_module.process_archive:L1868')
                            continue

                        if info.filename.lower().endswith('.fb2.zip'):
                            try:
                                with zipfile.ZipFile(BytesIO(xml_bytes)) as inner:
                                    inner_names = inner.namelist()
                                    if inner_names:
                                        xml_bytes = inner.read(inner_names[0])
                            except Exception:
                                swallow.note('collection_module.process_archive:L1878')
                                pass

                        data = parse_fb2(xml_bytes)
                        if not data or not data['title']:
                            continue

                        buffer.append((data, info.filename, info.file_size, archive_id))
                        books_in_archive += 1
                        if len(buffer) >= BATCH_SIZE:
                            _insert_batch(conn, buffer, store_content)
                            buffer = []

                    if buffer:
                        _insert_batch(conn, buffer, store_content)

                conn.execute("UPDATE archives SET books_count = ? WHERE id = ?",
                             (books_in_archive, archive_id))
                conn.commit()

                with books_lock:
                    total_books += books_in_archive
                with processed_lock:
                    processed += 1

                if progress_callback:
                    with processed_lock:
                        progress_callback(zname, processed, total_archives, total_books, 'completed')

            except Exception:
                try:
                    conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
                    conn.commit()
                except Exception:
                    swallow.note('collection_module.process_archive:L1912')
                    pass
                with processed_lock:
                    processed += 1
                if progress_callback:
                    with processed_lock:
                        progress_callback(zname, processed, total_archives, total_books, 'error')

    thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = [thread_pool.submit(process_archive, zp) for zp in remaining]
    concurrent.futures.wait(futures)
    thread_pool.shutdown()

    if progress_callback:
        progress_callback('', processed, total_archives, total_books, 'completed')
    update_scan_progress(status='completed')

# ─── BACKGROUND SCAN ───────────────────────────────────────────────────────

def start_scan(archives_path, parallel=True, store_content=False):
    global _scan_thread
    with _scan_lock:
        if _scan_thread and _scan_thread.is_alive():
            return False
        _scan_stop.clear()
        if parallel:
            _scan_thread = threading.Thread(
                target=scan_archives_parallel,
                args=(archives_path,),
                kwargs={'store_content': store_content},
                daemon=True)
        else:
            _scan_thread = threading.Thread(
                target=scan_archives,
                args=(archives_path,),
                kwargs={'store_content': store_content},
                daemon=True)
        _scan_thread.start()
    return True

def stop_scan():
    _scan_stop.set()

def get_scan_status():
    with _scan_lock:
        running = _scan_thread is not None and _scan_thread.is_alive()
    prog = get_scan_progress()
    if prog:
        prog['running'] = running
        return prog
    return {'running': running, 'status': 'idle'}

# ─── QUERIES ───────────────────────────────────────────────────────────────

def build_book_row(row):
    return dict(row) if row else None

def get_stats():
    with db_connection() as conn:
        books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        authors = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        genres = conn.execute("SELECT COUNT(*) FROM genres").fetchone()[0]
        series = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        archives = conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
    return {'books': books, 'authors': authors, 'genres': genres, 'series': series, 'archives': archives}

# ─── Список id для пагинации ───────────────────────────────────────────────
# ORDER BY id DESC LIMIT ? OFFSET ? на 690k строках — не переход по индексу,
# а проход по b-tree на offset элементов: p13000 стоил 3,1 с. Отсортированный
# список id занимает 5,5 МБ (array('q')), строится один раз в фоне и
# превращает любой OFFSET в срез массива.
_ID_INDEX_LOCK = threading.Lock()
_ID_INDEX = {}            # db_path -> (signature, array('q'))
_ID_INDEX_BUILDING = set()


def _stat_signature(db_path):
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _build_id_index(db_path, signature):
    from array import array
    conn = None
    try:
        ids = array('q')
        try:
            conn = sqlite3.connect(_sqlite_ro_uri(db_path), timeout=5, uri=True)
            conn.execute("SELECT 1")
        except sqlite3.OperationalError:
            if conn is not None:
                conn.close()
            conn = sqlite3.connect(db_path, timeout=5)
        ids.extend(row[0] for row in conn.execute(
            "SELECT id FROM books ORDER BY id DESC"))
        with _ID_INDEX_LOCK:
            if _stat_signature(db_path) == signature:
                _ID_INDEX[db_path] = (signature, ids)
    except Exception:
        swallow.note('collection_module._build_id_index:L2014')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                swallow.note('collection_module._build_id_index:L2020')
        with _ID_INDEX_LOCK:
            _ID_INDEX_BUILDING.discard(db_path)


def _id_index(db_path):
    """Отсортированные id всех книг или None, пока идёт фоновое построение."""
    signature = _stat_signature(db_path)
    if signature is None:
        return None
    with _ID_INDEX_LOCK:
        cached = _ID_INDEX.get(db_path)
        if cached and cached[0] == signature:
            return cached[1]
        if db_path in _ID_INDEX_BUILDING:
            return None
        _ID_INDEX_BUILDING.add(db_path)
    threading.Thread(target=_build_id_index, args=(db_path, signature),
                     daemon=True, name="collection-id-index").start()
    return None


def warm_id_index():
    """Запустить построение списка id заранее (вызывается из прогрева)."""
    return _id_index(COLLECTION_DB) is not None

def _books_page_ids(conn, page, page_size, genre_id=None, author_id=None,
                    series_id=None, search=None):
    """Id страницы и общее число книг без полного скана `books`.

    Прежний листинг сканировал таблицу books (935 МБ) на каждый запрос: на
    холодном кэше это десятки секунд, и восемь таких запросов вычерпывали
    весь RO-пул. Здесь сначала берутся только id (rowid/индексы), а тела
    ~50 строк гидратируются отдельно.
    """
    offset = (page - 1) * page_size
    filters = (genre_id, author_id, series_id)
    if not search and not any(filters):
        id_index = _id_index(COLLECTION_DB)
        if id_index is not None:
            return len(id_index), list(id_index[offset:offset + page_size])
        total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM books ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset)).fetchall()]
        return total, ids
    if not search and genre_id and not author_id and not series_id:
        total = conn.execute(
            "SELECT COUNT(DISTINCT book_id) FROM book_genres WHERE genre_id = ?",
            (genre_id,)).fetchone()[0]
        ids = [r[0] for r in conn.execute(
            "SELECT book_id FROM book_genres WHERE genre_id = ? "
            "GROUP BY book_id ORDER BY book_id DESC LIMIT ? OFFSET ?",
            (genre_id, page_size, offset)).fetchall()]
        return total, ids
    if not search and author_id and not genre_id and not series_id:
        total = conn.execute(
            "SELECT COUNT(DISTINCT book_id) FROM book_authors WHERE author_id = ?",
            (author_id,)).fetchone()[0]
        ids = [r[0] for r in conn.execute(
            "SELECT book_id FROM book_authors WHERE author_id = ? "
            "GROUP BY book_id ORDER BY book_id DESC LIMIT ? OFFSET ?",
            (author_id, page_size, offset)).fetchall()]
        return total, ids
    if not search and series_id and not genre_id and not author_id:
        total = conn.execute(
            "SELECT COUNT(*) FROM books WHERE series_id = ?",
            (series_id,)).fetchone()[0]
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM books WHERE series_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (series_id, page_size, offset)).fetchall()]
        return total, ids

    if search and not any(filters):
        total, ids = _search_match_ids(conn, search)
        return total, ids[offset:offset + page_size]

    joins = []
    where = []
    params = []
    if genre_id:
        joins.append("JOIN book_genres bg ON bg.book_id = b.id")
        where.append("bg.genre_id = ?")
        params.append(genre_id)
    if author_id:
        joins.append("JOIN book_authors ba ON ba.book_id = b.id")
        where.append("ba.author_id = ?")
        params.append(author_id)
    if series_id:
        where.append("b.series_id = ?")
        params.append(series_id)
    if search:
        joins.append("LEFT JOIN book_authors ba_s ON ba_s.book_id = b.id")
        joins.append("LEFT JOIN authors a ON a.id = ba_s.author_id")
        where.append("(b.title LIKE ? OR a.full_name LIKE ?)")
        params.extend([f'%{search}%', f'%{search}%'])
    where_clause = ' AND '.join(where)
    join_sql = ' '.join(joins)
    total = conn.execute(
        f"SELECT COUNT(DISTINCT b.id) FROM books b {join_sql} WHERE {where_clause}",
        params).fetchone()[0]
    ids = [r[0] for r in conn.execute(
        f"SELECT b.id FROM books b {join_sql} WHERE {where_clause} "
        "GROUP BY b.id ORDER BY b.id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]).fetchall()]
    return total, ids


def _hydrate_books(conn, ids, light=False):
    """Строки каталога для готового списка id, в том же порядке."""
    if not ids:
        return []
    rows = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        if light:
            sql = (f"SELECT b.id, b.title, b.file_path, b.file_size, b.lang, "
                   f"b.year, b.archive_id, b.seq_number, 0 as has_images "
                   f"FROM books b WHERE b.id IN ({marks})")
        else:
            sql = (f"SELECT b.id, b.title, b.file_path, b.file_size, b.lang, "
                   f"b.series_id, b.seq_number, b.year, b.description, "
                   f"0 as has_images, s.name as series_name, "
                   f"GROUP_CONCAT(DISTINCT a.full_name) as authors, "
                   f"CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover "
                   f"FROM books b "
                   f"LEFT JOIN series s ON b.series_id = s.id "
                   f"LEFT JOIN book_authors ba ON ba.book_id = b.id "
                   f"LEFT JOIN authors a ON a.id = ba.author_id "
                   f"LEFT JOIN covers c ON c.book_id = b.id "
                   f"WHERE b.id IN ({marks}) GROUP BY b.id")
        for r in conn.execute(sql, chunk).fetchall():
            rows[r["id"]] = dict(r)
    return [rows[i] for i in ids if i in rows]


def get_books(page=1, page_size=PAGE_SIZE, genre_id=None, author_id=None, series_id=None, search=None):
    with db_connection() as conn:
        total, ids = _books_page_ids(conn, page, page_size, genre_id, author_id,
                                     series_id, search)
        books_list = _hydrate_books(conn, ids)
    # merge has_images from cache
    book_ids = [b['id'] for b in books_list if b['id']]
    if book_ids:
        img_cache = check_books_have_images(book_ids)
        for b in books_list:
            b['has_images'] = img_cache.get(str(b['id']), False)
    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'books': books_list,
    }

def get_books_light(page=1, page_size=PAGE_SIZE, author_id=None, series_id=None, genre_id=None):
    """Быстрый список книг без JOIN для интеграции в общую библиотеку."""
    with db_connection() as conn:
        total, ids = _books_page_ids(conn, page, page_size, genre_id, author_id,
                                     series_id)
        books_list = _hydrate_books(conn, ids, light=True)
    book_ids = [b['id'] for b in books_list if b['id']]
    if book_ids:
        img_cache = check_books_have_images(book_ids)
        for b in books_list:
            b['has_images'] = img_cache.get(str(b['id']), False)
    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'books': books_list,
    }

def get_book(book_id):
    with db_connection() as conn:
        book = conn.execute("""
            SELECT b.*, s.name as series_name,
                   GROUP_CONCAT(DISTINCT a.full_name) as authors
            FROM books b
            LEFT JOIN series s ON b.series_id = s.id
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            WHERE b.id = ?
            GROUP BY b.id
        """, (book_id,)).fetchone()
        if not book:
            return None
        result = dict(book)

        archive = conn.execute(
            "SELECT filename, base_path FROM archives WHERE id = ?", (book['archive_id'],)
        ).fetchone()
        result['archive_filename'] = archive['filename'] if archive else None
        result['_base_path'] = archive['base_path'] if archive else None

        result['genre_list'] = [dict(r) for r in conn.execute("""
            SELECT g.* FROM genres g
            JOIN book_genres bg ON bg.genre_id = g.id
            WHERE bg.book_id = ?
        """, (book_id,)).fetchall()]

        result['author_list'] = [dict(r) for r in conn.execute("""
            SELECT a.* FROM authors a
            JOIN book_authors ba ON ba.author_id = a.id
            WHERE ba.book_id = ?
        """, (book_id,)).fetchall()]

        result['has_cover'] = conn.execute(
            "SELECT 1 FROM covers WHERE book_id = ?", (book_id,)
        ).fetchone() is not None

        try:
            extra = json.loads(book['extra'] or '{}')
            result['translators'] = extra.get('translators', [])
            result['keywords'] = extra.get('keywords', '')
            result['publisher'] = extra.get('publisher', '')
            result['isbn'] = extra.get('isbn', '')
        except Exception:
            swallow.note('collection_module.get_book:L2170')
            pass

    return result

_IMAGES_CACHE_PATH = os.path.expanduser("~/.infinity/collection_images_cache.json")
_images_cache_lock = threading.Lock()

def _load_images_cache():
    try:
        if os.path.exists(_IMAGES_CACHE_PATH):
            with open(_IMAGES_CACHE_PATH, 'r') as f:
                return json.load(f)
    except:
        swallow.note('collection_module._load_images_cache:L2184')
        pass
    return {}

def reset_images_cache():
    with _images_cache_lock:
        if os.path.exists(_IMAGES_CACHE_PATH):
            os.remove(_IMAGES_CACHE_PATH)
    return {'ok': True}

def _save_images_cache(cache):
    os.makedirs(os.path.dirname(_IMAGES_CACHE_PATH), exist_ok=True)
    with open(_IMAGES_CACHE_PATH, 'w') as f:
        json.dump(cache, f)

def scan_genre_images(genre_id):
    """Batch-scan all books in a genre for embedded images."""
    conn = get_default_db()
    rows = conn.execute(
        "SELECT DISTINCT b.id FROM books b JOIN book_genres bg ON bg.book_id = b.id WHERE bg.genre_id = ?",
        [genre_id]
    ).fetchall()
    conn.close()
    book_ids = [r['id'] for r in rows]
    check_books_have_images(book_ids)
    return {'ok': True, 'scanned': len(book_ids)}

def mark_book_has_images(book_id):
    with _images_cache_lock:
        cache = _load_images_cache()
        cache[str(book_id)] = True
        _save_images_cache(cache)
        return {'ok': True}

def check_books_have_images(book_ids):
    with _images_cache_lock:
        cache = _load_images_cache()
    result = {}
    need_check = []
    for bid in book_ids:
        sbid = str(bid)
        if sbid in cache:
            result[sbid] = cache[sbid]
        else:
            result[sbid] = False
            need_check.append(bid)
    if need_check:
        _BOOKS_DIR = None

        def books_dir():
            """Каталог с архивами. Раньше читался config.json напрямую и
            os.path.expanduser('~/books') — из-за этого модуль не видел
            ни переносимой папки ${ROOT}/data/books, ни подстановки путей,
            которые делает config_module. Теперь берём значение из
            единственного источника правды — config_module.get_config(),
            с откатом на каталог сервера.
            """
            nonlocal _BOOKS_DIR
            if _BOOKS_DIR is None:
                try:
                    from config_module import get_config
                    _BOOKS_DIR = get_config().get("books_dir", "") or os.path.expanduser("~/books")
                except Exception:
                    swallow.note('collection_module.books_dir:L2247')
                    _BOOKS_DIR = os.path.expanduser("~/books")
            return _BOOKS_DIR

        _BOOKS_DIR = books_dir()
        try:
            with db_connection() as conn:
                rows = conn.execute(
                    "SELECT b.id, b.file_path, a.filename as arch_filename "
                    "FROM books b JOIN archives a ON a.id = b.archive_id "
                    f"WHERE b.id IN ({','.join('?'*len(need_check))})",
                    need_check
                ).fetchall()
        except Exception:
            swallow.note('collection_module.check_books_have_images:L2261')
            rows = []
        updated = False

        def _probe(row):
            try:
                fp = find_archive(row['arch_filename'], _BOOKS_DIR, fast=True)
                if not fp:
                    return None
                with zipfile.ZipFile(fp, 'r') as zf:
                    try:
                        data = zf.read(row['file_path'])
                    except KeyError:
                        swallow.note('collection_module._probe:L2274')
                        for name in zf.namelist():
                            if name.lower().endswith('.fb2'):
                                data = zf.read(name)
                                break
                        else:
                            return None
                return data.count(b'<binary') >= 3
            except Exception:
                swallow.note('collection_module._probe:L2283')
                return None

        # Книги страницы лежат в разных архивах, и на холодном диске проверка
        # 50 штук подряд стоила ~0,3–0,4 с; потоки прячут задержку чтения.
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for row, has in zip(rows, pool.map(_probe, rows)):
                if has is None:
                    continue
                sbid = str(row['id'])
                result[sbid] = has
                cache[sbid] = has
                updated = True
        if updated:
            _save_images_cache(cache)
    return result

def get_book_has_images(book_id):
    return check_books_have_images([book_id]).get(str(book_id), False)

def get_genres():
    with db_connection() as conn:
        genres = conn.execute("""
            SELECT g.id, g.code, COALESCE(g.name, '') as name,
                   (SELECT COUNT(*) FROM book_genres bg WHERE bg.genre_id = g.id) as books_count
            FROM genres g
            ORDER BY g.code
        """).fetchall()
    result = []
    for g in genres:
        d = dict(g)
        d['name'] = d.get('name') or genre_name(d['code'])
        result.append(d)
    return result

def create_genre(code, name=''):
    with db_connection() as conn:
        try:
            conn.execute("INSERT OR IGNORE INTO genres (code, name) VALUES (?, ?)", (code, name or ''))
            return {'ok': True, 'id': conn.execute("SELECT id FROM genres WHERE code = ?", (code,)).fetchone()[0]}
        except Exception as e:
            swallow.note('collection_module.create_genre:L2324', e)
            return {'ok': False, 'error': str(e)}

def rename_genre(genre_id, name=None, code=None):
    with db_connection() as conn:
        try:
            if code:
                try:
                    conn.execute("ALTER TABLE genres ADD COLUMN code TEXT")
                except:
                    swallow.note('collection_module.rename_genre:L2334')
                    pass
                conn.execute("UPDATE genres SET code = ? WHERE id = ?", (code, genre_id))
            if name:
                try:
                    conn.execute("ALTER TABLE genres ADD COLUMN name TEXT DEFAULT ''")
                except:
                    swallow.note('collection_module.rename_genre:L2341')
                    pass
                conn.execute("UPDATE genres SET name = ? WHERE id = ?", (name, genre_id))
            if (code and conn.changes > 0) or (name and conn.changes > 0):
                return {'ok': True}
            return {'ok': False, 'error': 'No changes made'}
        except Exception as e:
            swallow.note('collection_module.rename_genre:L2348', e)
            return {'ok': False, 'error': str(e)}

def delete_genre(genre_id):
    with db_connection() as conn:
        try:
            conn.execute("DELETE FROM book_genres WHERE genre_id = ?", (genre_id,))
            conn.execute("DELETE FROM genres WHERE id = ?", (genre_id,))
            return {'ok': True}
        except Exception as e:
            swallow.note('collection_module.delete_genre:L2358', e)
            return {'ok': False, 'error': str(e)}

def merge_genres(source_id, target_id):
    with db_connection() as conn:
        try:
            # move all book_genre links from source to target
            conn.execute("INSERT OR IGNORE INTO book_genres (book_id, genre_id) SELECT book_id, ? FROM book_genres WHERE genre_id = ?", (target_id, source_id))
            conn.execute("DELETE FROM book_genres WHERE genre_id = ?", (source_id,))
            conn.execute("DELETE FROM genres WHERE id = ?", (source_id,))
            return {'ok': True}
        except Exception as e:
            swallow.note('collection_module.merge_genres:L2370', e)
            return {'ok': False, 'error': str(e)}

def get_authors(page=1, page_size=PAGE_SIZE):
    with db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        authors = conn.execute("""
            SELECT a.id, a.full_name
            FROM authors a
            ORDER BY a.last_name, a.full_name
            LIMIT ? OFFSET ?
        """, (page_size, (page - 1) * page_size)).fetchall()
    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'authors': [dict(a) for a in authors],
    }

def get_series(page=1, page_size=PAGE_SIZE):
    with db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        series = conn.execute("""
            SELECT s.id, s.name
            FROM series s
            ORDER BY s.name
            LIMIT ? OFFSET ?
        """, (page_size, (page - 1) * page_size)).fetchall()
    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'series': [dict(s) for s in series],
    }

def get_author_books(author_id, page=1, page_size=PAGE_SIZE):
    with db_connection() as conn:
        author = conn.execute("SELECT * FROM authors WHERE id = ?", (author_id,)).fetchone()
        if not author:
            return None
        total = conn.execute(
            "SELECT COUNT(*) FROM book_authors WHERE author_id = ?", (author_id,)
        ).fetchone()[0]
        books = conn.execute("""
            SELECT b.id, b.title, b.seq_number, b.year,
                   s.name as series_name, b.series_id,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN series s ON b.series_id = s.id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE ba.author_id = ?
            ORDER BY b.title
            LIMIT ? OFFSET ?
        """, (author_id, page_size, (page - 1) * page_size)).fetchall()
    return {
        'author': dict(author),
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'books': [dict(b) for b in books],
    }

def get_genre_books(genre_id, page=1, page_size=PAGE_SIZE):
    with db_connection() as conn:
        genre = conn.execute("SELECT * FROM genres WHERE id = ?", (genre_id,)).fetchone()
        if not genre:
            return None
        total = conn.execute(
            "SELECT COUNT(*) FROM book_genres WHERE genre_id = ?", (genre_id,)
        ).fetchone()[0]
        books = conn.execute("""
            SELECT b.id, b.title, b.year,
                   GROUP_CONCAT(DISTINCT a.full_name) as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            JOIN book_genres bg ON bg.book_id = b.id
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE bg.genre_id = ?
            GROUP BY b.id
            ORDER BY b.title
            LIMIT ? OFFSET ?
        """, (genre_id, page_size, (page - 1) * page_size)).fetchall()
    return {
        'genre': dict(genre),
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'books': [dict(b) for b in books],
    }

def get_series_books(series_id):
    with db_connection() as conn:
        series = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
        if not series:
            return None
        books = conn.execute("""
            SELECT b.id, b.title, b.seq_number, b.year,
                   GROUP_CONCAT(DISTINCT a.full_name) as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE b.series_id = ?
            GROUP BY b.id
            ORDER BY CAST(b.seq_number AS INTEGER), b.title
        """, (series_id,)).fetchall()
    return {
        'series': dict(series),
        'books': [dict(b) for b in books],
    }

# ─── CONTENT EXTRACTION ────────────────────────────────────────────────────

def get_book_content(book_id, archives_dir=None):
    book = get_book(book_id)
    if not book:
        print(f"[collection] book not found: {book_id}")
        return None

    archive = book.get('archive_filename')
    if not archive:
        print(f"[collection] archive filename missing for book {book_id}")
        return None

    archive_path = find_archive(archive, archives_dir)
    if not archive_path:
        base = book.get('_base_path', '?')
        print(f"[collection] archive not found: {archive} (base={base})")
        return None

    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            file_path = book['file_path']
            try:
                xml_bytes = zf.read(file_path)
            except KeyError:
                swallow.note('collection_module.get_book_content:L2514')
                for name in zf.namelist():
                    if name.lower().endswith('.fb2') or name.lower().endswith('.fb2.zip'):
                        xml_bytes = zf.read(name)
                        file_path = name
                        break
                else:
                    return None

            if file_path.lower().endswith('.fb2.zip'):
                try:
                    with zipfile.ZipFile(BytesIO(xml_bytes)) as inner:
                        inner_names = inner.namelist()
                        if inner_names:
                            xml_bytes = inner.read(inner_names[0])
                except Exception:
                    swallow.note('collection_module.get_book_content:L2530')
                    pass

        data = parse_fb2(xml_bytes)
        if not data:
            return None

        chapters = _fb2_extract_chapters(xml_bytes)
        all_text = '\n\n'.join(ch.get('text', '') for ch in chapters)

        result = {
            'id': book_id,
            'title': data['title'],
            'author': ', '.join(a['full_name'] for a in data['authors']),
            'annotation': data['description'],
            'genres': data['genres'],
            'lang': data['lang'],
            'format': 'fb2',
            'chapters': chapters,
            'full_text': all_text,
            'total_chapters': len(chapters),
        }

        return result

    except Exception:
        swallow.note('collection_module.get_book_content:L2556')
        return None

def _fb2_extract_chapters(xml_bytes):
    from lxml import etree
    chapters = []

    try:
        tree = etree.fromstring(xml_bytes)
    except Exception:
        swallow.note('collection_module._fb2_extract_chapters:L2566')
        return chapters

    ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}

    def extract_text(el):
        parts = []
        if el.text:
            parts.append(el.text)
        for child in el:
            if isinstance(child.tag, str):
                extract_text(child)
            if child.tail:
                parts.append(child.tail)
        return ''.join(parts)

    def process_section(section, depth=0):
        title_el = section.find('fb:title', ns) if ns else section.find('title')
        section_title = ''
        if title_el is not None:
            section_title = extract_text(title_el).strip()

        text_parts = []
        for child in section:
            if not isinstance(child.tag, str):
                continue
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'section':
                continue
            if tag in ('title', 'subtitle'):
                continue
            if tag == 'p':
                t = extract_text(child).strip()
                if t:
                    text_parts.append(t)
            elif tag == 'poem':
                t = extract_text(child).strip()
                if t:
                    text_parts.append(t)
            elif tag == 'empty-line':
                text_parts.append('')
            elif tag == 'cite':
                t = extract_text(child).strip()
                if t:
                    text_parts.append(t)

        section_text = '\n'.join(p for p in text_parts if p.strip()).strip()
        full_text = ''
        if section_title:
            full_text += f"{section_title}\n\n"
        if section_text:
            full_text += section_text

        if full_text.strip():
            chapters.append({
                'title': section_title or f'Раздел {len(chapters) + 1}',
                'text': full_text.strip()
            })

        for child in section:
            if not isinstance(child.tag, str):
                continue
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'section':
                process_section(child, depth + 1)

    bodies = tree.findall('.//fb:body', ns) if ns else tree.findall('.//body')
    for body in bodies:
        sections = body.findall('fb:section', ns) if ns else body.findall('section')
        if sections:
            for section in sections:
                process_section(section)
        else:
            body_text = []
            for elem in body.iter():
                if isinstance(elem.tag, str):
                    if elem.text:
                        t = elem.text.strip()
                        if t:
                            body_text.append(t)
                    if elem.tail:
                        t = elem.tail.strip()
                        if t:
                            body_text.append(t)
            bt = '\n'.join(body_text).strip()
            if bt:
                chapters.append({'title': 'Текст', 'text': bt})

    if not chapters:
        for body in bodies:
            body_text = []
            for elem in body.iter():
                if isinstance(elem.tag, str):
                    if elem.text:
                        t = elem.text.strip()
                        if t:
                            body_text.append(t)
                    if elem.tail:
                        t = elem.tail.strip()
                        if t:
                            body_text.append(t)
            bt = '\n'.join(body_text).strip()
            if bt:
                chapters.append({'title': 'Текст', 'text': bt})

    return chapters

def get_cover(book_id):
    with db_connection() as conn:
        cover = conn.execute(
            "SELECT image_data, mime_type FROM covers WHERE book_id = ?", (book_id,)
        ).fetchone()
    return dict(cover) if cover else None

# ─── SEARCH ────────────────────────────────────────────────────────────────

def _py_lower(value):
    # SQLite lower() складывает регистр только у ASCII, поэтому кириллица
    # («пушкин» против «Пушкин») без этой функции не находилась.
    return value.lower() if isinstance(value, str) else value


def _titles_for_ids(conn, ids):
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        out.extend(conn.execute(
            f"SELECT id, title FROM books WHERE id IN ({marks})", chunk).fetchall())
    return [(r[0], r[1]) for r in out]


def _title_matches(conn, q):
    """Пары (id, title) по подстроке без учёта регистра, включая кириллицу."""
    pattern = f"%{q.lower()}%"
    try:
        conn.create_function("py_lower", 1, _py_lower, deterministic=True)
        rows = conn.execute(
            "SELECT id, title FROM books WHERE py_lower(title) LIKE ?",
            (pattern,)).fetchall()
    except Exception as e:
        print(f"[collection] title match fallback: {e}")
        rows = conn.execute(
            "SELECT id, title FROM books WHERE title LIKE ?",
            (f"%{q}%",)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _author_book_ids(conn, q):
    """id книг, у которых автор подходит по подстроке.

    Раньше авторы подтягивались LEFT JOIN'ом к каждой из 690 тыс. строк
    (живой замер 2,2 с); отбор авторов отдельным подзапросом по индексу
    author_id занимает ~0,14 с.
    """
    pattern = f"%{q.lower()}%"
    sql = ("SELECT ba.book_id FROM book_authors ba WHERE ba.author_id IN "
           "(SELECT id FROM authors WHERE py_lower(full_name) LIKE ?)")
    try:
        conn.create_function("py_lower", 1, _py_lower, deterministic=True)
        rows = conn.execute(sql, (pattern,)).fetchall()
    except Exception as e:
        print(f"[collection] author match fallback: {e}")
        try:
            rows = conn.execute(sql.replace("py_lower(full_name)", "full_name"),
                                (f"%{q}%",)).fetchall()
        except Exception:
            swallow.note('collection_module._author_book_ids:L2733')
            return []
    return [r[0] for r in rows]


def _sidecar_matches(q):
    """Совпадения из trigram-индекса: (названия, id авторов) или None."""
    if len(q.strip()) < 3:
        return None        # trigram не индексирует строки короче трёх символов
    signature = _db_signature(COLLECTION_DB)
    conn = _ensure_search_index(COLLECTION_DB, signature)
    if conn is None:
        return None
    try:
        phrase = '"' + q.replace('"', '""') + '"'
        titles = conn.execute(
            "SELECT rowid, title FROM titles WHERE titles MATCH ?", (phrase,)).fetchall()
        authors = conn.execute(
            "SELECT rowid FROM authors_fts WHERE authors_fts MATCH ?", (phrase,)).fetchall()
        return [(r[0], r[1]) for r in titles], [r[0] for r in authors]
    except Exception as e:
        print(f"[collection] search index query failed: {e}")
        return None
    finally:
        conn.close()


def _search_match_ids(conn, q):
    """Все подходящие книги и их число, упорядоченные по названию.

    Порядок совпадает с прежним `ORDER BY b.title` (BINARY): Python сравнивает
    строки по кодовым точкам, а UTF-8 сохраняет этот порядок.
    """
    q = (q or "").strip()
    if not q:
        return 0, []
    sidecar = _sidecar_matches(q)
    if sidecar is not None:
        pairs, author_ids = sidecar
        if author_ids:
            marks = ",".join("?" * len(author_ids))
            author_books = [r[0] for r in conn.execute(
                f"SELECT book_id FROM book_authors WHERE author_id IN ({marks})",
                author_ids).fetchall()]
        else:
            author_books = []
    else:
        pairs = _title_matches(conn, q)
        author_books = _author_book_ids(conn, q)
    title_by_id = {}
    for bid, title in pairs:
        title_by_id[bid] = title
    for bid in author_books:
        title_by_id.setdefault(bid, None)
    missing = [bid for bid, title in title_by_id.items() if title is None]
    for bid, title in _titles_for_ids(conn, missing):
        title_by_id[bid] = title
    ids = sorted(title_by_id, key=lambda bid: (title_by_id[bid] or "", bid))
    return len(ids), ids


def _hydrate_search_rows(conn, ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"""
        SELECT b.id, b.title, b.seq_number, b.lang,
               s.name as series_name, b.series_id,
               GROUP_CONCAT(DISTINCT a.full_name) as authors,
               CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
        FROM books b
        LEFT JOIN series s ON b.series_id = s.id
        LEFT JOIN book_authors ba ON ba.book_id = b.id
        LEFT JOIN authors a ON a.id = ba.author_id
        LEFT JOIN covers c ON c.book_id = b.id
        WHERE b.id IN ({marks})
        GROUP BY b.id
    """, ids).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[bid] for bid in ids if bid in by_id]


def search_books(q, page=1, page_size=PAGE_SIZE):
    with db_connection() as conn:
        try:
            fts_available = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='books_fts'"
            ).fetchone() is not None
            if fts_available:
                count_row = conn.execute("SELECT COUNT(*) as cnt FROM books_fts").fetchone()
                fts_has_data = count_row and count_row['cnt'] > 0
            else:
                fts_has_data = False
        except Exception:
            swallow.note('collection_module.search_books:L2827')
            fts_has_data = False

        fts_used = False
        results = []

        if fts_has_data:
            try:
                terms = ' AND '.join(f'"{w}"' for w in q.strip().split() if w)
                if not terms:
                    terms = f'"{q}"'

                total = conn.execute("""
                    SELECT COUNT(*) as cnt FROM books_fts WHERE books_fts MATCH ?
                """, (terms,)).fetchone()['cnt']

                rows = conn.execute("""
                    SELECT f.rowid,
                           snippet(books_fts, 0, '<b>', '</b>', '...', 48) as title_snippet,
                           snippet(books_fts, 1, '<b>', '</b>', '...', 64) as content_snippet
                    FROM books_fts f
                    WHERE books_fts MATCH ?
                    ORDER BY rank
                    LIMIT ? OFFSET ?
                """, (terms, page_size, (page - 1) * page_size)).fetchall()

                book_ids = [r['rowid'] for r in rows]
                snippet_map = {r['rowid']: (r['content_snippet'] or r['title_snippet'] or '') for r in rows}

                if book_ids:
                    placeholders = ','.join('?' * len(book_ids))
                    books = conn.execute(f"""
                        SELECT b.id, b.title, b.seq_number, b.lang,
                               s.name as series_name, b.series_id,
                               GROUP_CONCAT(DISTINCT a.full_name) as authors,
                               CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
                        FROM books b
                        LEFT JOIN series s ON b.series_id = s.id
                        LEFT JOIN book_authors ba ON ba.book_id = b.id
                        LEFT JOIN authors a ON a.id = ba.author_id
                        LEFT JOIN covers c ON c.book_id = b.id
                        WHERE b.id IN ({placeholders})
                        GROUP BY b.id
                    """, book_ids).fetchall()

                    id_order = {bid: i for i, bid in enumerate(book_ids)}
                    books = sorted(books, key=lambda x: id_order.get(x['id'], 999))

                    for b in books:
                        bd = dict(b)
                        bd['snippet'] = snippet_map.get(b['id'], '')
                        results.append(bd)

                fts_used = True
            except Exception:
                swallow.note('collection_module.search_books:L2882')
                fts_has_data = False

        if not fts_used:
            total, ids = _search_match_ids(conn, q)
            page_ids = ids[(page - 1) * page_size: page * page_size]
            results = _hydrate_search_rows(conn, page_ids)

    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'results': results,
        'fts': fts_used,
    }
