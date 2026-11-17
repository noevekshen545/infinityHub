"""
Template Commands Module
Types: bash, python, curl, js, qa
Supports multi-type (e.g. "bash,js") — executes each in sequence.
"""

import os, sys, json, difflib, subprocess, logging, tempfile, random, re, shutil, traceback

logger = logging.getLogger("infinity")

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_FILE = os.path.join(BASE, "data", "templates.json")
VARIABLES_FILE = os.path.join(BASE, "data", "template_vars.json")

# ── Helpers ──

def _ensure(f, default):
    if not os.path.exists(f):
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(default, fh, ensure_ascii=False, indent=2)

def load_templates():
    _ensure(TEMPLATES_FILE, [])
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_templates(templates):
    _ensure(TEMPLATES_FILE, [])
    with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=2)

# ── Variables ──

def load_vars():
    _ensure(VARIABLES_FILE, {})
    with open(VARIABLES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_vars(vars_dict):
    _ensure(VARIABLES_FILE, {})
    with open(VARIABLES_FILE, "w", encoding="utf-8") as f:
        json.dump(vars_dict, f, ensure_ascii=False, indent=2)

def set_var(name, value):
    v = load_vars()
    v[name] = value
    save_vars(v)
    return v

def get_vars():
    return load_vars()

# ── CRUD ──

def add_template(name, cmd_type, command="", commands=None, description="", tags=None,
                 question="", answers=None, variables=None, follow_up=False):
    templates = load_templates()
    t = {
        "id": str(int(templates[-1]["id"]) + 1 if templates else 1),
        "name": name,
        "type": cmd_type,
        "description": description,
        "tags": tags or [],
        "follow_up": follow_up,
    }
    types = [x.strip() for x in cmd_type.split(",") if x.strip()]
    is_qa = "qa" in types
    if is_qa:
        t["question"] = question
        t["answers"] = answers or []
        t["variables"] = variables or {}
    elif commands and isinstance(commands, dict):
        # Multi-type with per-type commands
        t["commands"] = {k: v for k, v in commands.items() if k in types and v.strip()}
        # Still store combined command for backward compat
        t["command"] = "\n".join(
            f"#--{ct}--\n{commands.get(ct, '')}" for ct in types
            if commands.get(ct, "").strip()
        )
    else:
        t["command"] = command
    templates.append(t)
    save_templates(templates)
    logger.info(f"[TEMPLATE] Created: {name} type={cmd_type} id={t['id']}")
    return t

def delete_template(tid):
    templates = load_templates()
    templates = [t for t in templates if t["id"] != tid]
    save_templates(templates)
    logger.info(f"[TEMPLATE] Deleted id={tid}")

def update_template(tid, updates):
    templates = load_templates()
    for t in templates:
        if t["id"] == tid:
            t.update(updates)
            save_templates(templates)
            return t
    return None

# ── Matching ──

def match_template(text, threshold=0.6):
    templates = load_templates()
    if not templates:
        return None
    text_low = text.lower().strip()
    best = None
    best_r = 0
    for t in templates:
        if t["type"] == "qa":
            q = t.get("question", "").lower().strip()
            if q:
                if text_low == q or text_low.startswith(q) or q.startswith(text_low):
                    r = max(len(q) / max(len(text_low), 1), 0.7)
                else:
                    r = difflib.SequenceMatcher(None, text_low, q).ratio()
        else:
            r = difflib.SequenceMatcher(None, text_low, t["name"].lower()).ratio()
            for tag in t.get("tags", []):
                r = max(r, difflib.SequenceMatcher(None, text_low, tag.lower()).ratio())
        if r > best_r:
            best_r = r
            best = t
    logger.info(f"[TEMPLATE] match text='{text[:30]}' best='{best.get('name') if best else None}' ratio={best_r:.2f}")
    return best if best_r >= threshold else None

# ── Suggestions ──

def suggest(text, limit=3):
    templates = load_templates()
    scored = []
    text_low = text.lower().strip()
    for t in templates:
        if t["type"] == "qa":
            q = t.get("question", "").lower().strip()
            if q:
                r = difflib.SequenceMatcher(None, text_low, q).ratio()
                scored.append((r, q, t["id"]))
    scored.sort(key=lambda x: -x[0])
    return [{"question": q, "id": tid} for r, q, tid in scored[:limit] if r > 0.1]

# ── Variable substitution ──

def _substitute(text, template=None, args=None):
    v = load_vars()
    if args:
        text = text.replace("{args}", args).replace("{}", args)
    if template and isinstance(template, dict):
        for k, val in template.get("variables", {}).items():
            text = text.replace("{" + k + "}", str(val))
    for k, val in v.items():
        text = text.replace("{" + k + "}", str(val))
    return text

# ── Type-specific command extraction ──

def _extract_section(command, cmd_type):
    """Extract the relevant section for a given type from a multi-type command.

    Supports:
      #--bash-- / #--python-- / #--curl-- / #--js-- sections
      If no section markers found, returns the full command.
    """
    section_pattern = re.compile(r'^#--' + re.escape(cmd_type) + r'--\s*$', re.MULTILINE)
    sections = re.split(r'^#--\w+--\s*$', command, flags=re.MULTILINE)
    markers = re.findall(r'^#--(\w+)--\s*$', command, re.MULTILINE)
    if markers and sections:
        # Re-pair markers with their sections
        section_map = {}
        # sections[0] is content before first marker (usually empty)
        for i, marker in enumerate(markers):
            idx = i + 1
            section_map[marker] = sections[idx].strip() if idx < len(sections) else ""
        if cmd_type in section_map:
            return section_map[cmd_type]
    return command

# ── Single-type executor ──

def _exec_one(cmd_type, command):
    """Execute one command of a single type, return (success, output)."""
    command = _extract_section(command, cmd_type)
    logger.info(f"[TEMPLATE] _exec_one type={cmd_type} cmd='{command[:80]}'")
    try:
        if cmd_type == "bash":
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)

        elif cmd_type == "python":
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(command)
                p = f.name
            try:
                r = subprocess.run([sys.executable, p], capture_output=True, text=True, timeout=30)
            finally:
                os.unlink(p)

        elif cmd_type == "curl":
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)

        elif cmd_type == "js":
            node = shutil.which("node") or shutil.which("nodejs") or ""
            if not node:
                logger.error("[TEMPLATE] JS: node not found in PATH")
                return False, "Node.js not found in PATH. Install node or check PATH."
            # Shim browser globals so common code doesn't crash
            shim = """// JS template polyfill
const alert = console.log;
const document = { title: "", querySelector:()=>null, getElementById:()=>null };
const window = globalThis;
const fetch = globalThis.fetch;
const navigator = { userAgent: "Node.js" };
const location = { href: "", hostname: "localhost", port: "8080", protocol: "http:" };
const setTimeout = (fn, ms) => { if (typeof fn === 'function') fn(); return 0; };
const clearTimeout = () => {};
/* Use: console.log("ANIM:motion_name") to trigger avatar animation */
/* Use: console.log("VAR:name=value") to share variable with next type */
"""
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
                f.write(shim + "\n" + command)
                p = f.name
            try:
                r = subprocess.run([node, p], capture_output=True, text=True, timeout=30)
                logger.info(f"[TEMPLATE] JS done rc={r.returncode} stdout={r.stdout[:200]}")
            finally:
                os.unlink(p)

        else:
            return False, f"Unknown type: {cmd_type}"

        out = (r.stdout or r.stderr or "").strip()
        logger.info(f"[TEMPLATE] {cmd_type} rc={r.returncode} out='{out[:200]}'")
        return r.returncode == 0, out[:4000]

    except subprocess.TimeoutExpired:
        logger.error(f"[TEMPLATE] {cmd_type} timed out")
        return False, "Execution timed out"
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[TEMPLATE] {cmd_type} error: {e}\n{tb}")
        return False, f"{e}"

# ── Execution ──

def execute_template(template, args=None):
    cmd_type = template["type"]
    follow_up = template.get("follow_up", False)

    logger.info(f"[TEMPLATE] execute id={template['id']} name='{template['name']}' type={cmd_type} follow_up={follow_up}")

    if cmd_type == "qa":
        answers = template.get("answers", [])
        if not answers:
            return {"success": True, "output": "(нет ответов)", "follow_up": follow_up}
        answer = random.choice(answers)
        answer = _substitute(answer, template, args)
        logger.info(f"[TEMPLATE] QA answer='{answer[:100]}'")
        return {"success": True, "output": answer, "follow_up": follow_up}

    # Multi-type: comma-separated (e.g. "bash,js")
    types = [t.strip() for t in cmd_type.split(",") if t.strip()]
    logger.info(f"[TEMPLATE] types={types}")

    outputs = []
    overall_ok = True
    all_anims = []
    shared_vars = {}  # accumulate VAR:name=value across types
    # Check if template has per-type commands dict
    commands_dict = template.get("commands") or {}
    for ct in types:
        # QA внутри multi-type обрабатывается как подбор случайного ответа
        if ct == "qa":
            answers = template.get("answers", [])
            if not answers:
                outputs.append(f"[qa] (нет ответов)")
            else:
                ans = random.choice(answers)
                ans = _substitute(ans, template, args)
                outputs.append(f"[qa] {ans}")
            continue

        # Get command for this specific type, fallback to combined "command" field
        cmd = commands_dict.get(ct) if isinstance(commands_dict, dict) else None
        if cmd is None:
            cmd = template.get("command", "")
        # Substitute template vars AND shared vars from previous types
        cmd = _substitute(cmd, template, args)
        # Also substitute shared variables
        for k, val in shared_vars.items():
            cmd = cmd.replace("{" + k + "}", str(val))
        ok, out = _exec_one(ct, cmd)
        # Extract markers: ANIM:name and VAR:name=value
        anim_lines = []
        var_lines = []
        clean_lines = []
        for line in out.split("\n"):
            if line.startswith("ANIM:"):
                anim_name = line[5:].strip()
                if anim_name:
                    anim_lines.append(anim_name)
            elif line.startswith("VAR:"):
                var_line = line[4:].strip()
                if "=" in var_line:
                    v_name, v_val = var_line.split("=", 1)
                    v_name = v_name.strip()
                    v_val = v_val.strip()
                    if v_name:
                        shared_vars[v_name] = v_val
                        var_lines.append(f"{v_name}={v_val}")
            else:
                clean_lines.append(line)
        filtered_out = "\n".join(clean_lines)
        if anim_lines:
            all_anims.extend(anim_lines)
            logger.info(f"[TEMPLATE] animations detected: {anim_lines}")
        if var_lines:
            logger.info(f"[TEMPLATE] vars captured: {var_lines}")
        outputs.append(f"[{ct}] {filtered_out}")
        if not ok:
            overall_ok = False

    result_text = "\n".join(outputs)
    result = {"success": overall_ok, "output": result_text[:4000], "follow_up": follow_up}
    if all_anims:
        result["animations"] = all_anims
    if shared_vars:
        result["shared_vars"] = shared_vars
    logger.info(f"[TEMPLATE] result success={overall_ok} out='{result_text[:200]}' anims={all_anims} vars={shared_vars}")
    return result
