"""Offline summaries for code files: what it is, what it defines, what it's about."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

LANGS = {
    "py": "Python", "ipynb": "Python notebook", "js": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript",
    "jsx": "React (JSX)", "ts": "TypeScript", "tsx": "React (TSX)", "gd": "GDScript", "gdshader": "Godot shader",
    "swift": "Swift", "c": "C", "h": "C header", "cpp": "C++", "hpp": "C++ header", "cc": "C++", "cs": "C#",
    "java": "Java", "kt": "Kotlin", "kts": "Kotlin script", "go": "Go", "rs": "Rust", "rb": "Ruby", "php": "PHP",
    "lua": "Lua", "sh": "Shell script", "zsh": "Shell script", "bash": "Shell script", "fish": "Fish script",
    "sql": "SQL", "html": "Web page", "htm": "Web page", "css": "Stylesheet", "scss": "Stylesheet",
    "sass": "Stylesheet", "less": "Stylesheet", "json": "JSON data", "yaml": "YAML config", "yml": "YAML config",
    "toml": "TOML config", "ini": "Config", "cfg": "Config", "xml": "XML", "vue": "Vue component",
    "svelte": "Svelte component", "dart": "Dart", "glsl": "Shader", "hlsl": "Shader", "shader": "Shader",
    "bat": "Batch script", "ps1": "PowerShell", "mcfunction": "Minecraft function", "mcmeta": "Minecraft pack",
}

# tag -> regex. A tag applies when its pattern appears; more hits rank it higher.
TAG_RULES = {
    "pathfinding": r"AStar|NavigationAgent|NavigationServer|navmesh|pathfind|dijkstra|a_star|\bastar\b|find_path|get_point_path",
    "movement": r"move_and_slide|move_and_collide|CharacterBody|KinematicBody|\bvelocity\b|MoveTowards|translate\(",
    "physics": r"RigidBody|PhysicsBody|PhysicsServer|\bcollision|Area2D|Area3D|RayCast|raycast|gravity",
    "player input": r"Input\.is_|Input\.get_|InputEvent|addEventListener\(\s*['\"]key|keydown|keyup|GetKey|getKey|pygame\.key",
    "UI": r"\bControl\b|Button|Label|innerHTML|querySelector|getElementById|SwiftUI|UIView|tkinter|QWidget|\bVBox|\bHBox|CanvasLayer",
    "audio": r"AudioStream|new Audio\(|AudioContext|AVAudio|playsound|pygame\.mixer|\.play\(\)\s*#?\s*sound|sfx",
    "animation": r"AnimationPlayer|AnimationTree|create_tween|\bTween\b|@keyframes|requestAnimationFrame|\.animate\(|withAnimation",
    "networking": r"HTTPRequest|fetch\(|XMLHttpRequest|WebSocket|\bsocket\b|requests\.(get|post)|urllib|axios|URLSession",
    "multiplayer": r"multiplayer|\brpc\b|@rpc|ENetMultiplayer|MultiplayerSpawner|peer_connected|Photon|Mirror\.",
    "3D": r"Node3D|\bSpatial\b|THREE\.|Vector3|Camera3D|MeshInstance3D|WebGLRenderer|SceneKit|RealityKit",
    "2D": r"Node2D|Sprite2D|AnimatedSprite|TileMap|getContext\(\s*['\"]2d|pygame\.display|SKScene",
    "shaders": r"shader_type|gl_FragColor|gl_Position|\buniform\b|fragment\(\)|vertex\(\)|ShaderMaterial",
    "enemy AI": r"\benemy|\bEnemy|state_machine|StateMachine|behaviou?r.?tree|\bchase\b|\bpatrol|aggro",
    "combat": r"\bdamage|take_damage|\bhealth\b|\bhp\b|\battack|\bweapon|\bbullet|\bprojectile",
    "inventory": r"\binventory|\bInventory|add_item|remove_item|\bslots?\b",
    "save data": r"FileAccess|ConfigFile|localStorage|json\.dump|save_game|savegame|load_game|UserDefaults|ResourceSaver",
    "database": r"sqlite|SELECT\s+.+\s+FROM|INSERT INTO|CREATE TABLE|mongoose|prisma|PostgreSQL|psycopg",
    "web server": r"http\.server|express\(\)|Flask\(|FastAPI\(|app\.(get|post)\(|createServer|HTTPServer",
    "camera": r"Camera2D|Camera3D|PerspectiveCamera|OrthographicCamera|\bcamera\b",
    "particles": r"GPUParticles|CPUParticles|ParticleSystem|\bparticles?\b",
    "procedural generation": r"FastNoiseLite|OpenSimplex|perlin|\bnoise\(|procedural|generate_(map|world|terrain|level|chunk)",
    "level/tiles": r"TileMap|TileSet|tilemap|set_cell|\blevel\b|GridMap",
    "machine learning": r"\btorch\b|tensorflow|sklearn|keras|transformers|onnx",
    "web scraping": r"BeautifulSoup|selenium|puppeteer|playwright|scrapy|cheerio",
    "command line": r"argparse|sys\.argv|process\.argv|\bcommander\b|clap::|getopt",
    "Minecraft": r"minecraft|Bukkit|Spigot|\bForge\b|Fabric|mcfunction|Mojang",
    "testing": r"\bunittest\b|\bpytest\b|\bdescribe\(|\bit\(\s*['\"]|XCTest|\bassert_eq|@Test\b",
    "timers": r"\bTimer\b|setTimeout|setInterval|start_timer|wait_time",
    "game loop": r"_process\(|_physics_process\(|def update\(|function update\(|Update\(\)|gameLoop|game_loop",
    "signals/events": r"\bsignal\b|emit_signal|\.emit\(|\.connect\(|addEventListener|EventEmitter|dispatchEvent",
}
_TAG_RX = {t: re.compile(p) for t, p in TAG_RULES.items()}

# Words too generic to say what a file is about.
STOP = set("""
self this that true false null none nil var let const func function def return if else elif for while in of
new class extends import from export default public private static void int float bool string str list dict
get set value values data item items type types name names node nodes len print args kwargs result results
index key keys obj object array vector vector2 vector3 math min max abs clamp lerp delta time position
pos size width height x y z i j k n tmp temp err error e event res req ready process physics init main
await async try catch except finally pass break continue and or not is as with super onready export
signal emit connect add remove update draw on off begin end start stop the to a an be by at it its
color rect child children parent scene get_node queue_free free show hide visible true false
document window element elements style div span id src href length push pop map filter forEach then
console log info debug warn http https www com html css js json png jpg mp3 wav ogg
head title body meta link script stylesheet rel doctype utf charset viewport content class button input
""".split())

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _defs(ext: str, text: str) -> dict:
    out: dict[str, list[str]] = {"functions": [], "classes": [], "signals": [], "imports": []}
    if ext == "gd":
        out["functions"] = re.findall(r"^\s*(?:static\s+)?func\s+(\w+)", text, re.M)
        out["classes"] = re.findall(r"^\s*class_name\s+(\w+)|^\s*class\s+(\w+)", text, re.M)
        out["signals"] = re.findall(r"^\s*signal\s+(\w+)", text, re.M)
        out["imports"] = re.findall(r"(?:preload|load)\(\s*\"res://([^\"]+)\"", text)
    elif ext in ("py", "ipynb"):
        out["functions"] = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", text, re.M)
        out["classes"] = re.findall(r"^\s*class\s+(\w+)", text, re.M)
        out["imports"] = re.findall(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", text, re.M)
    elif ext in ("js", "mjs", "cjs", "jsx", "ts", "tsx", "vue", "svelte", "html", "htm"):
        out["functions"] = re.findall(
            r"function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>|^\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{",
            text, re.M,
        )
        out["classes"] = re.findall(r"class\s+(\w+)", text)
        out["imports"] = re.findall(r"import\s+(?:[^'\"]+\s+from\s+)?['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"]|<script[^>]+src=['\"]([^'\"]+)['\"]", text)
    elif ext == "swift":
        out["functions"] = re.findall(r"\bfunc\s+(\w+)", text)
        out["classes"] = re.findall(r"\b(?:class|struct|enum|protocol|actor)\s+(\w+)", text)
        out["imports"] = re.findall(r"^\s*import\s+(\w+)", text, re.M)
    elif ext in ("cs", "java", "kt", "kts", "dart", "scala"):
        out["functions"] = re.findall(r"(?:public|private|protected|internal|static|override|fun|void)\s+[\w<>\[\], ]*?(\w+)\s*\(", text)
        out["classes"] = re.findall(r"\b(?:class|struct|interface|enum|object)\s+(\w+)", text)
        out["imports"] = re.findall(r"^\s*(?:using|import)\s+([\w.]+)", text, re.M)
    elif ext in ("c", "h", "cpp", "hpp", "cc", "go", "rs", "lua", "rb", "php", "sh", "zsh", "bash"):
        out["functions"] = re.findall(
            r"^\s*(?:fn|func|function|def|local function)\s+([\w.:]+)|^[\w\*\s]+?\b(\w+)\s*\([^;]*\)\s*\{", text, re.M
        )
        out["classes"] = re.findall(r"\b(?:struct|class|impl|trait|module|type)\s+(\w+)", text)
        out["imports"] = re.findall(r"#include\s+[<\"]([^>\"]+)|^\s*use\s+([\w:]+)|require\s*\(?['\"]([^'\"]+)", text, re.M)
    for k, v in out.items():
        flat = [next((g for g in x if g), "") if isinstance(x, tuple) else x for x in v]
        seen = dict.fromkeys(f for f in flat if f and f not in ("if", "for", "while", "switch", "catch", "return", "function"))
        out[k] = list(seen)
    return out


def _description(text: str) -> str:
    """First real comment or docstring: usually the author's own words about the file."""
    # (pattern, minimum length): a page title can be short, a comment needs a few words to be useful.
    patterns = [
        (r'^\s*(?:"""|\'\'\')(.+?)(?:"""|\'\'\')', 12),  # Python docstring
        (r"/\*\*?(.+?)\*/", 12),                          # block comment
        (r"<title>(.+?)</title>", 2),                     # web page title
        (r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)', 8),
    ]
    for p, min_len in patterns:
        m = re.search(p, text[:6000], re.S | re.I)
        if m:
            d = re.sub(r"^\s*\*\s?", "", m.group(1), flags=re.M)
            d = re.sub(r"\s+", " ", d).strip()
            if len(d) >= min_len and not re.match(r"(?i)(copyright|license|eslint|@ts-|jshint|global )", d):
                return d[:220]
    lines = []
    for line in text[:6000].splitlines():
        s = line.strip()
        m = re.match(r"^(?:#+|//+|--|;+)\s?(.*)$", s)
        if m and not s.startswith(("#!", "#include", "#import", "#define", "#if", "#pragma", "#region", "#endregion")):
            c = m.group(1).strip()
            if c and not re.match(r"(?i)(copyright|license|todo|fixme|noqa|type:|region)", c):
                lines.append(c)
                if len(" ".join(lines)) > 200:
                    break
                continue
        if lines and s:
            break
    d = " ".join(lines).strip()
    return d[:220] if len(d) >= 12 else ""


def _keywords(text: str, defined: set[str], limit: int = 8) -> list[str]:
    """Most frequent meaningful words in identifiers, strings and comments: the file's subject."""
    counts: Counter = Counter()
    for ident in _IDENT.findall(text[:200_000]):
        for w in _CAMEL.sub("_", ident).lower().split("_"):
            if len(w) >= 3 and w not in STOP and not w.isdigit():
                counts[w] += 1
    return [w for w, n in counts.most_common(limit * 3) if n >= 2][:limit]


def summarize(path: Path, text: str) -> dict | None:
    ext = path.suffix.lower().lstrip(".") or path.name.lower()
    if not text.strip():
        return None
    lines = text.count("\n") + 1
    s: dict = {"lang": LANGS.get(ext, ext.upper() or "Code"), "lines": lines}
    if ext == "gd":
        m = re.search(r"^\s*extends\s+([\w.\"/:]+)", text, re.M)
        if m:
            s["extends"] = m.group(1).strip('"')
    elif ext in ("py", "js", "ts", "swift", "cs", "java", "kt"):
        m = re.search(r"class\s+\w+\s*(?:\(|extends|:)\s*([\w.]+)", text)
        if m:
            s["extends"] = m.group(1)
    if ext in ("json",):
        m = re.findall(r'^\s{2}"([^"]+)"\s*:', text[:20000], re.M) or re.findall(r'^\s*"([^"]+)"\s*:', text[:4000], re.M)
        s["keys"] = list(dict.fromkeys(m))[:10]
    d = _defs(ext, text)
    for k in ("functions", "classes", "signals", "imports"):
        if d[k]:
            s[k] = d[k][:14]
            if len(d[k]) > 14:
                s[k + "_more"] = len(d[k]) - 14
    desc = _description(text)
    if desc:
        s["desc"] = desc
    hits = [(t, len(rx.findall(text[:200_000]))) for t, rx in _TAG_RX.items()]
    s["tags"] = [t for t, n in sorted(hits, key=lambda x: -x[1]) if n][:7]
    defined = set(d["functions"]) | set(d["classes"])
    s["keywords"] = _keywords(text, defined)
    return s


def search_words(s: dict | None) -> str:
    """Tags and subject words go into the keyword index, so 'pathfinding' finds AStar code."""
    if not s:
        return ""
    return " ".join(s.get("tags", []) + s.get("keywords", []) + ([s["extends"]] if s.get("extends") else []))
