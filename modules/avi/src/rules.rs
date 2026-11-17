//! Движок сигнатурных правил, работающий внутри процесса.
//!
//! Внешний `yara` на этой машине не установлен, и прошлая реализация молча
//! выключала детект по правилам целиком. Здесь — свой парсер подмножества
//! YARA-синтаксиса и многошаблонный поиск через автомат Ахо-Корасика, поэтому
//! правила работают всегда, без внешнего бинаря, за один проход по байтам.
//!
//! Поддерживается: `rule Имя { meta: score=... description="..." strings:
//! $a = "текст" nocase wide ascii fullword / $b = { 4D 5A ?? 00 } condition:
//! <выражение> }`. Выражение: `any of them`, `all of them`, `N of them`,
//! `any|all of ($a, $b*)`, `not`, `and`, `or`, скобки.
//!
//! Всё, что в этот набор не входит (регулярки, `filesize`, `uint16()`,
//! `for..of`, `import`), помечается в `unsupported` и оглашается при старте:
//! правило не может «молча не сработать» — лучше честный отказ, чем ложное
//! чувство защиты.

use aho_corasick::{AhoCorasick, AhoCorasickBuilder};
use anyhow::Context;
use std::path::Path;

#[derive(Debug)]
pub struct Pattern {
    pub name: String,
    pub bytes: Vec<u8>,
    /// По байту шаблона: 0 — фиксирован, 1 — младший нибб `?`, 2 — старший,
    /// 3 — полный `??`. Без этого `{ 4? }` стал бы «любой байт», то есть
    /// ложными срабатываниями там, где правило их не предполагает.
    pub mask: Option<Vec<u8>>,
    pub nocase: bool,
    pub wide: bool,
    pub ascii: bool,
    pub fullword: bool,
}

#[derive(Debug, Clone)]
pub enum Cond {
    Or(Vec<Cond>),
    And(Vec<Cond>),
    Not(Box<Cond>),
    /// Требуется, чтобы сработало минимум N из перечисленных имён
    /// (пустой список = «из всех строк правила»).
    Of { min: usize, names: Vec<String> },
    Name(String),
}

#[derive(Debug)]
pub struct Rule {
    pub name: String,
    pub score: f64,
    pub description: String,
    pub patterns: Vec<Pattern>,
    pub cond: Cond,
}

/// Один конкреный байтовый шаблон внутри автомата: (правило, строка).
#[derive(Debug, Clone, Copy)]
struct Unit {
    rule: usize,
    pattern: usize,
}

pub struct Engine {
    rules: Vec<Rule>,
    /// Индексы совпадений автомата — это позиции в его собственном списке
    /// шаблонов, поэтому каждый автомат держит свой параллельный список единиц;
    /// общий массив на оба индексы ломает вердикты (проверено тестом).
    sens_units: Vec<Unit>,
    insens_units: Vec<Unit>,
    masked_units: Vec<Unit>,
    sensitive: Option<AhoCorasick>,
    insensitive: Option<AhoCorasick>,
    pub sources: Vec<String>,
    pub unsupported: Vec<String>,
}

pub struct Hit {
    pub rule: String,
    pub score: f64,
    pub description: String,
    pub matched: Vec<String>,
}

const DEFAULT_RULE_SCORE: f64 = 0.65;

impl Engine {
    pub fn empty() -> Self {
        Self {
            rules: Vec::new(),
            sens_units: Vec::new(),
            insens_units: Vec::new(),
            masked_units: Vec::new(),
            sensitive: None,
            insensitive: None,
            sources: Vec::new(),
            unsupported: Vec::new(),
        }
    }

    pub fn rule_count(&self) -> usize {
        self.rules.len()
    }

    /// Собирает движок из текстов правил; `label` — откуда текст (для отчёта).
    pub fn add_source(&mut self, label: &str, text: &str) {
        self.sources.push(label.to_string());
        parse_source(text, label, &mut self.rules, &mut self.unsupported);
        self.rebuild();
    }

    pub fn add_file(&mut self, path: &Path) -> anyhow::Result<()> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("не удалось прочитать правило {}", path.display()))?;
        self.add_source(&path.display().to_string(), &text);
        Ok(())
    }

    fn rebuild(&mut self) {
        self.sens_units.clear();
        self.insens_units.clear();
        self.masked_units.clear();

        let mut sens: Vec<Vec<u8>> = Vec::new();
        let mut insens: Vec<Vec<u8>> = Vec::new();

        for (ri, rule) in self.rules.iter().enumerate() {
            for (pi, pat) in rule.patterns.iter().enumerate() {
                let unit = Unit {
                    rule: ri,
                    pattern: pi,
                };

                if pat.mask.is_some() {
                    self.masked_units.push(unit);
                    continue;
                }

                for variant in variants(pat) {
                    if pat.nocase {
                        insens.push(variant);
                        self.insens_units.push(unit);
                    } else {
                        sens.push(variant);
                        self.sens_units.push(unit);
                    }
                }
            }
        }

        self.sensitive = build(&sens, false);
        self.insensitive = build(&insens, true);
    }

    /// Возвращает сработавшие правила; имена идут в отчёт как причина находки.
    pub fn scan(&self, haystack: &[u8]) -> Vec<Hit> {
        if self.rules.is_empty() || haystack.is_empty() {
            return Vec::new();
        }

        // (rule, pattern) -> true
        let mut fired: Vec<Vec<bool>> = self
            .rules
            .iter()
            .map(|r| vec![false; r.patterns.len()])
            .collect();

        for (aho, units) in [
            (self.sensitive.as_ref(), &self.sens_units),
            (self.insensitive.as_ref(), &self.insens_units),
        ] {
            let Some(aho) = aho else { continue };

            for m in aho.find_overlapping_iter(haystack) {
                let Some(&unit) = units.get(m.pattern().as_usize()) else {
                    continue;
                };
                if self.rules[unit.rule].patterns[unit.pattern].fullword
                    && !fullword_ok(haystack, m.start(), m.end())
                {
                    continue;
                }
                fired[unit.rule][unit.pattern] = true;
            }
        }

        for &unit in &self.masked_units {
            if fired[unit.rule][unit.pattern] {
                continue;
            }
            let pat = &self.rules[unit.rule].patterns[unit.pattern];
            if masked_find(haystack, pat) {
                fired[unit.rule][unit.pattern] = true;
            }
        }

        let mut hits = Vec::new();

        for (ri, rule) in self.rules.iter().enumerate() {
            let matched: Vec<String> = rule
                .patterns
                .iter()
                .zip(fired[ri].iter())
                .filter(|(_, ok)| **ok)
                .map(|(p, _)| p.name.clone())
                .collect();

            if eval(&rule.cond, &rule.patterns, &fired[ri]) {
                hits.push(Hit {
                    rule: rule.name.clone(),
                    score: rule.score,
                    description: rule.description.clone(),
                    matched,
                });
            }
        }

        hits.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        hits
    }
}

/// `fold_case` включает ASCII-сворачивание регистра — это то, чем в YARA
/// является модификатор `nocase`.
fn build(patterns: &[Vec<u8>], fold_case: bool) -> Option<AhoCorasick> {
    if patterns.is_empty() {
        return None;
    }

    // Standard — единственный режим, допускающий перекрывающиеся совпадения:
    // левый-первый (LeftmostFirst) терял бы шаблон, оказавшийся внутри чужого
    // совпадения, и правило молча не срабатывало бы.
    AhoCorasickBuilder::new()
        .ascii_case_insensitive(fold_case)
        .build(patterns)
        .ok()
}

/// `wide`/`ascii` дают по одному варианту байтов, `nocase` обрабатывает автомат.
fn variants(pat: &Pattern) -> Vec<Vec<u8>> {
    let mut out = Vec::new();

    if pat.ascii {
        out.push(pat.bytes.clone());
    }

    if pat.wide {
        out.push(
            pat.bytes
                .iter()
                .flat_map(|b| [*b, 0u8])
                .collect::<Vec<u8>>(),
        );
    }

    if out.is_empty() {
        out.push(pat.bytes.clone());
    }

    out
}

fn fullword_ok(hay: &[u8], start: usize, end: usize) -> bool {
    let before_ok = start
        .checked_sub(1)
        .map(|i| !is_word_byte(hay[i]))
        .unwrap_or(true);
    let after_ok = hay.get(end).map(|b| !is_word_byte(*b)).unwrap_or(true);
    before_ok && after_ok
}

fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'\\' || b == b'/'
}

/// Посимвольный поиск шаблона с нибб-маской (`{ 4D ?? 5? }`).
fn masked_find(hay: &[u8], pat: &Pattern) -> bool {
    let Some(mask) = &pat.mask else {
        return false;
    };
    if pat.bytes.is_empty() || pat.bytes.len() > hay.len() {
        return false;
    }

    // Якорь — первый полностью фиксированный байт: без него пришлось бы
    // сравнивать каждый байт файла со всем шаблоном целиком.
    let anchor = mask.iter().position(|m| *m == 0);
    let first = anchor.map(|a| pat.bytes[a]);

    for base in 0..=(hay.len() - pat.bytes.len()) {
        if let Some(want) = first {
            if hay[base + anchor.unwrap()] != want {
                continue;
            }
        }

        let mut ok = true;
        for (i, m) in mask.iter().enumerate() {
            if *m == 3 {
                continue;
            }
            let want = pat.bytes[i];
            let got = hay[base + i];
            let mut hi = want >> 4;
            let mut lo = want & 0x0f;
            if *m & 2 != 0 {
                hi = 0xff;
            }
            if *m & 1 != 0 {
                lo = 0xff;
            }
            let eq = (hi == 0xff || got >> 4 == hi) && (lo == 0xff || got & 0x0f == lo);
            if !eq {
                ok = false;
                break;
            }
        }

        if ok {
            return true;
        }
    }

    false
}

fn eval(cond: &Cond, patterns: &[Pattern], fired: &[bool]) -> bool {
    match cond {
        Cond::Or(parts) => parts.iter().any(|p| eval(p, patterns, fired)),
        Cond::And(parts) => parts.iter().all(|p| eval(p, patterns, fired)),
        Cond::Not(inner) => !eval(inner, patterns, fired),
        Cond::Name(name) => patterns
            .iter()
            .position(|p| p.name == *name)
            .map(|i| fired[i])
            .unwrap_or(false),
        Cond::Of { min, names } => {
            let pool: Vec<usize> = if names.is_empty() {
                (0..patterns.len()).collect()
            } else {
                names
                    .iter()
                    .filter_map(|n| patterns.iter().position(|p| &p.name == n))
                    .collect()
            };
            if pool.is_empty() {
                return false;
            }
            let need = if *min == usize::MAX { pool.len() } else { *min };
            pool.iter().filter(|i| fired[**i]).count() >= need
        }
    }
}

// ---------------- парсер ----------------

fn parse_source(
    text: &str,
    label: &str,
    rules: &mut Vec<Rule>,
    unsupported: &mut Vec<String>,
) {
    let cleaned = strip_comments(text);
    let bytes: Vec<char> = cleaned.chars().collect();
    let mut i = 0usize;

    while i < bytes.len() {
        i = skip_ws(&bytes, i);
        if !starts_at(&bytes, i, "rule") || !word_end(&bytes, i + 4) {
            // Пустой импорт/комментарий между правилами — молча идём дальше,
            // но не по правилу с непонятной конструкцией (это ниже).
            let Some(next) = find_token(&bytes, i, "rule") else {
                break;
            };
            i = next;
            continue;
        }

        i += 4;
        let save = i;
        let parsed = parse_rule(&bytes, &mut i, label, unsupported);
        match parsed {
            Some(r) => rules.push(r),
            None => i = save,
        }
    }
}

fn parse_rule(
    b: &[char],
    i: &mut usize,
    label: &str,
    unsupported: &mut Vec<String>,
) -> Option<Rule> {
    let mut name = String::new();
    *i = skip_ws(b, *i);
    while *i < b.len() && (b[*i].is_ascii_alphanumeric() || b[*i] == '_') {
        name.push(b[*i]);
        *i += 1;
    }

    // tag-блок после имени
    while *i < b.len() && b[*i] != '{' {
        *i += 1;
    }
    if *i >= b.len() {
        unsupported.push(format!("{label}: правило «{name}» без тела"));
        return None;
    }
    *i += 1; // '{'

    let mut score = DEFAULT_RULE_SCORE;
    let mut description = String::new();
    let mut patterns: Vec<Pattern> = Vec::new();
    let mut cond_src = String::new();
    let mut section = String::new();
    let mut depth = 1usize;

    while *i < b.len() && depth > 0 {
        *i = skip_ws(b, *i);
        if *i >= b.len() {
            break;
        }
        let c = b[*i];
        if c == '{' {
            depth += 1;
            *i += 1;
            continue;
        }
        if c == '}' {
            depth -= 1;
            *i += 1;
            continue;
        }

        let word = read_ident(b, i);
        match word.as_str() {
            "meta" => {
                expect(b, i, ':');
                section = "meta".into();
            }
            "strings" => {
                expect(b, i, ':');
                section = "strings".into();
            }
            "condition" => {
                expect(b, i, ':');
                section = "condition".into();
                cond_src = read_until_brace(b, i);
            }
            "" => {
                *i += 1;
            }
            other => match section.as_str() {
                "meta" => {
                    let key = other.to_string();
                    skip_to(b, i, '=');
                    *i += 1;
                    let value = read_value(b, i);
                    if key == "score" {
                        if let Ok(v) = value.trim().parse::<f64>() {
                            score = v.clamp(0.0, 1.0);
                        }
                    } else if key == "description" {
                        description = value.trim_matches('"').to_string();
                    }
                }
                "strings" => {
                    let mut pat = String::from(other);
                    if pat.is_empty() {
                        *i += 1;
                        continue;
                    }
                    if !pat.starts_with('$') {
                        pat.insert(0, '$');
                    }
                    *i = skip_ws(b, *i);
                    if b.get(*i) == Some(&'=') {
                        *i += 1;
                    }
                    match read_string_def(b, i) {
                        Some(mut p) => {
                            p.name = pat;
                            patterns.push(p);
                        }
                        None => {
                            unsupported.push(format!(
                                "{label}: {name} {pat} — конструкция не поддержана (регэкспы, jump, скобки)"
                            ));
                            skip_line(b, i);
                        }
                    }
                }
                _ => {
                    *i += 1;
                }
            },
        }
    }

    if patterns.is_empty() {
        unsupported.push(format!("{label}: {name} — нет поддерживаемых строк"));
        return None;
    }

    let cond = match parse_cond(cond_src.replace('$', "").as_str(), &patterns) {
        Ok(c) => c,
        Err(e) => {
            unsupported.push(format!("{label}: {name} — условие не разобрано ({e})"));
            return None;
        }
    };

    Some(Rule {
        name,
        score,
        description,
        patterns,
        cond,
    })
}

/// Условия пишутся без `$` (он теряется при токенизации), имена сопоставляются
/// по суффиксу, поэтому список имён строк передаётся отдельно.
fn parse_cond(src: &str, patterns: &[Pattern]) -> Result<Cond, String> {
    let s = src.trim();
    if s.is_empty() {
        return Err("пустое".into());
    }

    let mut toks: Vec<String> = Vec::new();
    let mut cur = String::new();
    for c in s.chars() {
        if c.is_whitespace() || c == ',' {
            if !cur.is_empty() {
                toks.push(cur.clone());
                cur.clear();
            }
        } else if c == '(' || c == ')' {
            if !cur.is_empty() {
                toks.push(cur.clone());
                cur.clear();
            }
            toks.push(c.to_string());
        } else {
            cur.push(c);
        }
    }
    if !cur.is_empty() {
        toks.push(cur);
    }

    let names: Vec<String> = patterns.iter().map(|p| p.name[1..].to_string()).collect();
    let mut p = CondParser {
        toks,
        i: 0,
        names,
    };
    let out = p.or()?;
    if p.i != p.toks.len() {
        return Err(format!("мусор после условия: {:?}", p.toks[p.i..].join(" ")));
    }
    Ok(out)
}

struct CondParser {
    toks: Vec<String>,
    i: usize,
    names: Vec<String>,
}

impl CondParser {
    fn peek(&self) -> Option<&str> {
        self.toks.get(self.i).map(|s| s.as_str())
    }
    fn bump(&mut self) -> Option<String> {
        let t = self.toks.get(self.i).cloned();
        if t.is_some() {
            self.i += 1;
        }
        t
    }
    fn eat(&mut self, word: &str) -> bool {
        if self.peek() == Some(word) {
            self.i += 1;
            return true;
        }
        false
    }

    fn or(&mut self) -> Result<Cond, String> {
        let mut parts = vec![self.and()?];
        while self.eat("or") {
            parts.push(self.and()?);
        }
        Ok(if parts.len() == 1 {
            parts.pop().unwrap()
        } else {
            Cond::Or(parts)
        })
    }

    fn and(&mut self) -> Result<Cond, String> {
        let mut parts = vec![self.unary()?];
        while self.eat("and") {
            parts.push(self.unary()?);
        }
        Ok(if parts.len() == 1 {
            parts.pop().unwrap()
        } else {
            Cond::And(parts)
        })
    }

    fn unary(&mut self) -> Result<Cond, String> {
        if self.eat("not") {
            return Ok(Cond::Not(Box::new(self.unary()?)));
        }
        self.primary()
    }

    fn primary(&mut self) -> Result<Cond, String> {
        if self.eat("(") {
            // Список имён после `any of (...)` или вложенное выражение.
            if let Some(c) = self.try_of_list()? {
                return Ok(c);
            }
            let inner = self.or()?;
            if !self.eat(")") {
                return Err("нет закрывающей скобки".into());
            }
            return Ok(inner);
        }

        let tok = self.bump().ok_or_else(|| "условие оборвалось".to_string())?;
        let lower = tok.to_lowercase();

        match lower.as_str() {
            "any" | "all" => {
                if !self.eat("of") {
                    return Err("после any/all ожидалось of".into());
                }
                if self.eat("them") {
                    return Ok(Cond::Of {
                        min: if lower == "all" { usize::MAX } else { 1 },
                        names: Vec::new(),
                    });
                }
                self.named_of(if lower == "all" { usize::MAX } else { 1 })
            }
            _ => {
                if let Some(n) = lower.parse::<usize>().ok() {
                    if self.eat("of") {
                        if self.eat("them") {
                            return Ok(Cond::Of {
                                min: n,
                                names: Vec::new(),
                            });
                        }
                        return self.named_of(n);
                    }
                    return Err("после числа ожидалось of".into());
                }
                if self.names.iter().any(|x| x.eq_ignore_ascii_case(&lower)) {
                    return Ok(Cond::Name(format!("${lower}")));
                }
                Err(format!("неизвестный элемент условия «{tok}»"))
            }
        }
    }

    /// `($a, $b*)` — с расшариванием префиксных масок имён.
    fn named_of(&mut self, min: usize) -> Result<Cond, String> {
        if !self.eat("(") {
            return Err("ожидалась (".into());
        }
        let mut out = Vec::new();
        loop {
            let Some(tok) = self.bump() else {
                return Err("список имён оборвался".into());
            };
            if tok == ")" {
                break;
            }
            if tok == "," {
                continue;
            }
            let name = tok.trim_start_matches('$').to_lowercase();
            if let Some(stripped) = name.strip_suffix('*') {
                let mut expanded = false;
                for n in &self.names {
                    if n.to_lowercase().starts_with(stripped) {
                        out.push(format!("${n}"));
                        expanded = true;
                    }
                }
                if !expanded {
                    return Err(format!("нет строки с префиксом {stripped}"));
                }
            } else if self.names.iter().any(|n| n.to_lowercase() == name) {
                out.push(format!("${name}"));
            } else {
                return Err(format!("нет строки ${name}"));
            }
        }
        Ok(Cond::Of { min, names: out })
    }

    /// `(a and b)` внутри `any of (...)` — это не список имён: откатываемся и
    /// парсим как выражение.
    fn try_of_list(&mut self) -> Result<Option<Cond>, String> {
        let save = self.i;
        let mut depth = 1usize;
        let mut is_name_list = false;
        while let Some(t) = self.toks.get(self.i) {
            match t.as_str() {
                "(" => depth += 1,
                ")" => {
                    depth -= 1;
                    if depth == 0 {
                        self.i += 1;
                        break;
                    }
                }
                "and" | "or" | "not" => {
                    self.i = save;
                    return Ok(None);
                }
                _ => {
                    let name = t.trim_start_matches('$').to_lowercase();
                    if self
                        .names
                        .iter()
                        .any(|n| n.to_lowercase() == name || name.ends_with('*'))
                    {
                        is_name_list = true;
                    }
                }
            }
            self.i += 1;
        }
        if !is_name_list {
            self.i = save;
            return Ok(None);
        }
        self.i = save;
        Ok(Some(self.named_of(1)?))
    }
}

// ---------------- лексика ----------------

fn strip_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let b: Vec<char> = text.chars().collect();
    let mut i = 0;
    let mut in_str = false;
    while i < b.len() {
        if in_str {
            out.push(b[i]);
            if b[i] == '"' && i > 0 && b[i - 1] != '\\' {
                in_str = false;
            }
            i += 1;
            continue;
        }
        match b[i] {
            '"' => {
                out.push('"');
                in_str = true;
                i += 1;
            }
            '/' if i + 1 < b.len() && b[i + 1] == '/' => {
                while i < b.len() && b[i] != '\n' {
                    i += 1;
                }
            }
            '/' if i + 1 < b.len() && b[i + 1] == '*' => {
                i += 2;
                while i + 1 < b.len() && !(b[i] == '*' && b[i + 1] == '/') {
                    i += 1;
                }
                i = (i + 2).min(b.len());
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    out
}

fn skip_ws(b: &[char], mut i: usize) -> usize {
    while i < b.len() && b[i].is_whitespace() {
        i += 1;
    }
    i
}

fn starts_at(b: &[char], i: usize, w: &str) -> bool {
    b.len() >= i + w.len() && b[i..i + w.len()].iter().collect::<String>() == w
}

fn word_end(b: &[char], i: usize) -> bool {
    !b.get(i).map(|c| c.is_ascii_alphanumeric() || *c == '_').unwrap_or(false)
}

fn read_ident(b: &[char], i: &mut usize) -> String {
    *i = skip_ws(b, *i);
    let mut s = String::new();
    while *i < b.len() && (b[*i].is_ascii_alphanumeric() || b[*i] == '_' || b[*i] == '$') {
        s.push(b[*i]);
        *i += 1;
    }
    s.to_lowercase()
}

fn find_token(b: &[char], from: usize, w: &str) -> Option<usize> {
    (from..b.len()).find(|&i| starts_at(b, i, w) && word_end(b, i + w.len()) && (i == 0 || !b[i-1].is_ascii_alphanumeric()))
}

fn expect(b: &[char], i: &mut usize, c: char) {
    *i = skip_ws(b, *i);
    if b.get(*i) == Some(&c) {
        *i += 1;
    }
}

fn skip_to(b: &[char], i: &mut usize, c: char) {
    while *i < b.len() && b[*i] != c {
        *i += 1;
    }
}

fn skip_line(b: &[char], i: &mut usize) {
    while *i < b.len() && b[*i] != '\n' {
        *i += 1;
    }
}

fn read_value(b: &[char], i: &mut usize) -> String {
    *i = skip_ws(b, *i);
    let mut s = String::new();
    if b.get(*i) == Some(&'"') {
        s.push('"');
        *i += 1;
        while *i < b.len() && b[*i] != '"' {
            s.push(b[*i]);
            *i += 1;
        }
        s.push('"');
        *i += 1;
        return s;
    }
    while *i < b.len() && !b[*i].is_whitespace() && b[*i] != '\n' {
        s.push(b[*i]);
        *i += 1;
    }
    s
}

fn read_until_brace(b: &[char], i: &mut usize) -> String {
    let mut s = String::new();
    let mut depth = 0usize;
    while *i < b.len() {
        let c = b[*i];
        if c == '(' {
            depth += 1;
        }
        if c == ')' && depth > 0 {
            depth -= 1;
        }
        if c == '}' && depth == 0 {
            break;
        }
        s.push(c);
        *i += 1;
    }
    s
}

fn read_string_def(b: &[char], i: &mut usize) -> Option<Pattern> {
    // Имя строки уже съедено вызывающим; здесь — значение после '='.
    *i = skip_ws(b, *i);
    let mut pat = Pattern {
        name: String::new(),
        bytes: Vec::new(),
        mask: None,
        nocase: false,
        wide: false,
        ascii: false,
        fullword: false,
    };

    match b.get(*i) {
        Some('"') => {
            *i += 1;
            while *i < b.len() && b[*i] != '"' {
                if b[*i] == '\\' && *i + 1 < b.len() {
                    *i += 1;
                    pat.bytes.push(unescape(b[*i]));
                    *i += 1;
                    continue;
                }
                let c = b[*i];
                if (c as u32) < 128 {
                    pat.bytes.push(c as u8);
                } else {
                    pat.bytes.extend_from_slice(&c.to_string().into_bytes());
                }
                *i += 1;
            }
            *i += 1;
            pat.ascii = true;
        }
        Some('{') => {
            *i += 1;
            // Собираем посимвольно: два нибба (или `?`) на байт, как в YARA.
            let mut nibbles: Vec<Option<u8>> = Vec::new();
            while *i < b.len() && b[*i] != '}' {
                let c = b[*i];
                if c.is_whitespace() {
                    *i += 1;
                    continue;
                }
                if c == '?' {
                    nibbles.push(None);
                    nibbles.push(None);
                    *i += 1;
                    if b.get(*i) == Some(&'?') {
                        *i += 1;
                    }
                    continue;
                }
                if c.is_ascii_hexdigit() {
                    *i += 1;
                    let hi = c.to_digit(16).unwrap() as u8;
                    let lo = match b.get(*i) {
                        Some(x) if x.is_ascii_hexdigit() => {
                            *i += 1;
                            Some(x.to_digit(16).unwrap() as u8)
                        }
                        Some('?') => {
                            *i += 1;
                            None
                        }
                        _ => Some(0),
                    };
                    nibbles.push(Some(hi));
                    nibbles.push(lo);
                    continue;
                }
                // Скобки `[1-4]`, jumps `[-8]` и прочее в подмножество не входят.
                return None;
            }
            *i += 1;

            if nibbles.len() % 2 != 0 || nibbles.is_empty() {
                return None;
            }

            let mut mask = Vec::with_capacity(nibbles.len() / 2);
            for pair in nibbles.chunks(2) {
                let hi = pair[0];
                let lo = pair[1];
                let mut m = 0u8;
                if hi.is_none() {
                    m |= 2;
                }
                if lo.is_none() {
                    m |= 1;
                }
                mask.push(m);
                pat.bytes.push((hi.unwrap_or(0) << 4) | lo.unwrap_or(0));
            }

            if mask.iter().all(|m| *m == 0) {
                pat.mask = None;
            } else {
                pat.mask = Some(mask);
            }
        }
        _ => return None,
    }

    // модификаторы до конца строки
    while *i < b.len() && b[*i] != '\n' {
        let save = *i;
        let w = read_ident(b, i);
        match w.as_str() {
            "nocase" => pat.nocase = true,
            "wide" => pat.wide = true,
            "ascii" => pat.ascii = true,
            "fullword" => pat.fullword = true,
            // Идентификатор не модификатор: отдаём его главному циклу, иначе
            // `condition:` съедается здесь, а его тела разбираются как строки.
            _ => {
                *i = if w.is_empty() { save + 1 } else { save };
                if !w.is_empty() {
                    break;
                }
            }
        }
        *i = skip_ws(b, *i);
    }

    if !pat.wide && pat.mask.is_none() {
        pat.ascii = true;
    }

    Some(pat)
}

fn unescape(c: char) -> u8 {
    match c {
        'n' => b'\n',
        'r' => b'\r',
        't' => b'\t',
        '0' => 0,
        other => other as u8,
    }
}

#[cfg(test)]
mod tests {
    use super::Engine;

    fn engine(text: &str) -> Engine {
        let mut e = Engine::empty();
        e.add_source("test", text);
        e
    }

    #[test]
    fn text_rule_matches_case_sensitively() {
        let e = engine(r#"rule Demo { strings: $a = "hello" condition: $a }"#);
        assert_eq!(e.scan(b"say hello now").len(), 1);
        assert_eq!(e.scan(b"say HeLLo now").len(), 0);
    }

    #[test]
    fn nocase_and_wide_variants_match() {
        let e = engine(
            r#"rule N { strings: $a = "hello" nocase wide condition: $a }"#,
        );
        assert_eq!(e.scan(b"h\0e\0l\0l\0o\0").len(), 1);
        assert_eq!(e.scan(b"H\0E\0L\0L\0O\0").len(), 1);
    }

    #[test]
    fn fullword_rejects_substring_hit() {
        let e = engine(r#"rule F { strings: $a = "cmd" fullword condition: $a }"#);
        assert_eq!(e.scan(b"run cmd /c").len(), 1);
        assert_eq!(e.scan(b"run cmdline").len(), 0);
    }

    #[test]
    fn hex_wildcard_matches_half_nibble() {
        let e = engine(r#"rule H { strings: $a = { 4D ?? 5? 00 } condition: $a }"#);
        assert_eq!(e.scan(b"xxM\xffP\x00").len(), 1);
        // старший нибб не 5 — совпадения быть не должно
        assert_eq!(e.scan(b"xxM\xff`\x00").len(), 0);
    }

    #[test]
    fn conditions_any_all_and_counts() {
        let src = r#"rule A { strings: $a = "one" $b = "two" condition: any of them }
rule B { strings: $a = "one" $b = "two" condition: all of them }
rule C { strings: $a = "one" $b = "two" $c = "three" condition: 2 of them }
rule D { strings: $a = "one" $b = "two" condition: $a and not $b }"#;
        let e = engine(src);
        assert_eq!(e.rules.len(), 4);
        let names: Vec<String> = e.scan(b"one and two").iter().map(|h| h.rule.clone()).collect();
        assert_eq!(names, vec!["A", "B", "C"]);
        let names: Vec<String> = e.scan(b"only one").iter().map(|h| h.rule.clone()).collect();
        assert_eq!(names, vec!["A", "D"]);
    }

    #[test]
    fn score_and_description_come_from_meta() {
        let e = engine(r#"rule S { meta: score = 0.9 description = "тест" strings: $a = "x" condition: $a }"#);
        let hits = e.scan(b"xxx");
        assert_eq!(hits[0].score, 0.9);
        assert_eq!(hits[0].description, "тест");
    }

    #[test]
    fn regex_is_reported_not_silently_dropped() {
        let mut e = Engine::empty();
        e.add_source("test", r#"rule R { strings: $a = /cmd.*/ condition: $a }"#);
        assert!(e.rules.is_empty());
        assert!(e.unsupported.iter().any(|u| u.contains("не поддержана")));
    }

    #[test]
    fn comments_and_blank_lines_are_skipped() {
        let e = engine(
            "// хвост\n/* блок } { */\nrule K { strings: $a = \"zap\" // trailing }\n condition: $a }",
        );
        assert_eq!(e.rule_count(), 1);
        assert_eq!(e.scan(b"zap").len(), 1);
    }
}
