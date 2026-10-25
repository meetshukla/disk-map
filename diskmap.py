#!/usr/bin/env python3
"""Disk Map: scan your home folder, find reclaimable junk, and render an
interactive HTML report (treemap + cleanup checklist that builds a script).

Usage:
    python3 diskmap.py              # scan, then open the app (with a Clean button)
    python3 diskmap.py --serve      # open the app on the last scan, no rescan
    python3 diskmap.py --render     # just write a static disk-map.html (script only)
    options: --port 8777  --no-open

The app runs a server on 127.0.0.1 only. It deletes nothing until you press
"Clean now" and confirm, and it only ever deletes paths from its own
recommendation list, inside your home folder.
"""
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
SCAN_JSON = os.path.join(HERE, "scan.json")
TEMPLATE = os.path.join(HERE, "template.html")
OUT_HTML = os.path.join(HERE, "disk-map.html")

MB = 1024 * 1024
GB = 1024 * MB
KEEP_KB = 20 * 1024            # remember every dir >= 20 MB
TREE_MIN = 100 * MB            # treemap nodes >= 100 MB
TREE_MAX_DEPTH = 7
REC_MIN = 100 * MB             # recommendations >= 100 MB
IDLE_DAYS = 30
_scale = float(os.environ.get("DISKMAP_TEST_SCALE", "1"))   # shrink thresholds for tests
KEEP_KB, TREE_MIN, REC_MIN = int(KEEP_KB * _scale), int(TREE_MIN * _scale), int(REC_MIN * _scale)

# How each recommendation is cleaned.
REMOVE_DIR_IDS = {"nm_idle", "nm_active", "buildout", "rust", "venv", "pods", "appcache", "electron",
                  "browsers", "nodecache", "codexinstall", "npm", "bun", "yarn", "pip", "gradle", "cargo", "conda", "codexlogs"}
WORKTREE_IDS = {"wt_remove"}
REMOVE_DIR_IDS = REMOVE_DIR_IDS | {"wt_nm"}
CMD_TOOL = {"docker": ["docker", "system", "prune", "-a", "-f"],
            "sims": ["xcrun", "simctl", "delete", "unavailable"],
            "colima": ["colima", "delete", "-f"]}

# Directory names that are build output / dependency installs.
PATTERN_NAMES = {"node_modules", ".next", ".turbo", ".nuxt", ".svelte-kit",
                 ".parcel-cache", ".expo", ".venv", "venv", "target", "Pods",
                 "DerivedData", "worktrees", ".gradle"}
APP_CACHE_NAMES = {"Cache", "Caches", "CachedData", "Code Cache", "GPUCache",
                   "CacheStorage", "Service Worker", "ShipIt", "CachedExtensionVSIXs",
                   "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache", "logs", "Crashpad"}


def h(rel):
    return os.path.join(HOME, rel)


# ---------------------------------------------------------------- scanning

def run_du(sizes, patterns, progress):
    """Stream `du -k -x ~` and keep large dirs + pattern matches."""
    proc = subprocess.Popen(["du", "-k", "-x", HOME], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            errors="surrogateescape", bufsize=1 << 20)
    n = 0
    for line in proc.stdout:
        n += 1
        if n % 5000 == 0:
            progress["dirs"] = n
        tab = line.find("\t")
        if tab < 0:
            continue
        kb = int(line[:tab])
        path = line[tab + 1:-1]
        if kb >= KEEP_KB:
            sizes[path] = kb * 1024
            name = path.rsplit("/", 1)[-1]
            if name in PATTERN_NAMES or name in APP_CACHE_NAMES:
                patterns.append(path)
    proc.wait()
    progress["dirs"] = n


def run_find_large(out):
    """Files >= 1 GB anywhere in home (du doesn't list files)."""
    try:
        res = subprocess.run(["find", HOME, "-xdev", "-type", "f", "-size", "+1G"],
                             capture_output=True, text=True, errors="surrogateescape")
        for p in res.stdout.splitlines():
            try:
                st = os.lstat(p)
                out.append((p, st.st_blocks * 512, st.st_mtime))
            except OSError:
                pass
    except Exception:
        pass


def scan(on_progress=None):
    """Full scan of HOME. `on_progress(dirs, seconds)` is called about once a second."""
    t0 = time.time()
    sizes, patterns, large, progress = {}, [], [], {"dirs": 0}
    tf = threading.Thread(target=run_find_large, args=(large,))
    tf.start()
    td = threading.Thread(target=run_du, args=(sizes, patterns, progress))
    td.start()
    last_print = 0
    while td.is_alive():
        td.join(1)
        secs = time.time() - t0
        if on_progress:
            on_progress(progress["dirs"], secs)
        if secs - last_print >= 5:
            last_print = secs
            print(f"  … {progress['dirs']:,} folders scanned ({secs:.0f}s)", flush=True)
    while tf.is_alive():                     # large-file search can finish a little later
        tf.join(1)
        if on_progress:
            on_progress(progress["dirs"], time.time() - t0)
    du = shutil.disk_usage("/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/")
    data = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "home": HOME,
        "disk": {"total": du.total, "used": du.used, "free": du.free},
        "sizes": sizes,
        "patterns": patterns,
        "large_files": large,
        "seconds": round(time.time() - t0),
        "dirs": progress["dirs"],
    }
    with open(SCAN_JSON, "w") as f:
        json.dump(data, f)
    print(f"Scan finished in {data['seconds']}s: {len(sizes):,} large folders, "
          f"{len(large)} files ≥1 GB")
    return data


# ---------------------------------------------------------------- analysis

CATEGORY_RULES = [
    # (category, predicate on path relative to home)
    ("ai", lambda r: r.startswith((".ollama", ".cache/huggingface", ".lmstudio", ".cache/lm-studio", ".cache/whisper"))),
    ("sdk", lambda r: r.startswith(("Library/Android", ".android", "Library/Developer", ".rustup", ".cargo",
                                     "anaconda3", "miniconda3", ".conda", ".anaconda", "go/", ".gradle",
                                     ".colima", "Library/Containers/com.docker.docker", "VirtualBox VMs",
                                     "Virtual Machines.localized"))),
    ("cache", lambda r: r.startswith(("Library/Caches", ".cache", ".npm", "Library/Logs", ".Trash",
                                       ".bun/install/cache", "Library/pnpm"))
                         or any(f"/{n}" in "/" + r for n in ("Cache/", "Caches/", "CachedData/", "Code Cache/", "GPUCache/"))
                         or r.rsplit("/", 1)[-1] in APP_CACHE_NAMES),
    ("dev", lambda r: "node_modules" in r or r.startswith(("Developer", "Projects", "code", "src", "conductor",
                                                           ".codex", ".claude", ".cursor"))
                       or any(f"/{n}" in "/" + r + "/" for n in (".next/", ".turbo/", ".venv/", "target/", ".git/"))),
    ("media", lambda r: r.startswith(("Pictures", "Movies", "Music", "Screen Studio Projects"))
                         or r.lower().endswith((".mp4", ".mov", ".mkv", ".m4v", ".wav", ".mp3", ".photoslibrary"))),
    ("docs", lambda r: r.startswith(("Documents", "Desktop", "Downloads", "Google Drive", "Library/CloudStorage",
                                      "Library/Mobile Documents"))),
    ("apps", lambda r: r.startswith(("Library/Application Support", "Library/Containers", "Library/Group Containers",
                                      "Applications", "Library"))),
]


def categorize(rel):
    for cat, pred in CATEGORY_RULES:
        try:
            if pred(rel):
                return cat
        except Exception:
            pass
    return "other"


def build_tree(sizes, large_files):
    children = {}
    for p, s in sizes.items():
        if p == HOME or s < TREE_MIN:
            continue
        rel = os.path.relpath(p, HOME)
        if rel.count("/") >= TREE_MAX_DEPTH:
            continue
        children.setdefault(os.path.dirname(p), []).append(p)
    files = {}
    for p, s, _ in large_files:
        if s >= TREE_MIN:
            files.setdefault(os.path.dirname(p), []).append((p, s))

    def node(p, depth):
        size = sizes.get(p, 0)
        rel = "" if p == HOME else os.path.relpath(p, HOME)
        n = {"n": os.path.basename(p) or "~", "s": size, "p": rel, "k": categorize(rel) if rel else "other"}
        kids = []
        for c in sorted(children.get(p, []), key=lambda c: -sizes[c]):
            kids.append(node(c, depth + 1))
        for fp, fs in files.get(p, []):
            frel = os.path.relpath(fp, HOME)
            kids.append({"n": os.path.basename(fp), "s": fs, "p": frel, "k": categorize(frel), "f": 1})
        if kids:
            rest = size - sum(k["s"] for k in kids)
            if rest > TREE_MIN / 2:
                kids.append({"n": "smaller items", "s": rest, "p": rel, "k": n["k"], "x": 1})
            n["c"] = sorted(kids, key=lambda k: -k["s"])
        return n

    return node(HOME, 0)


def project_mtime(proj):
    """Most recent modification among a project's top-level entries."""
    newest = 0
    try:
        with os.scandir(proj) as it:
            for e in it:
                if e.name in ("node_modules", ".next", ".turbo", ".venv", "venv", "target", ".DS_Store"):
                    continue
                try:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
        gi = os.path.join(proj, ".git", "index")
        if os.path.exists(gi):
            newest = max(newest, os.path.getmtime(gi))
    except OSError:
        pass
    return newest


def q(p):
    return "'" + p.replace("'", "'\\''") + "'"


def git(wt, *args):
    try:
        r = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def chat_index():
    """Where Claude Code and Codex chats ran, so worktrees with chats are never removed.
    Claude: ~/.claude*/projects/<cwd with non-alphanumerics as '-'>/*.jsonl
    Codex:  first line of each ~/.codex/sessions/**/*.jsonl records its cwd."""
    claude, codex = {}, {}
    for base in glob.glob(os.path.join(HOME, ".claude*", "projects")):
        try:
            for d in os.listdir(base):
                claude[d] = claude.get(d, 0) + len(glob.glob(os.path.join(base, d, "*.jsonl")))
        except OSError:
            pass
    for sub in (".codex/sessions", ".codex/archived_sessions"):
        for f in glob.iglob(os.path.join(HOME, sub, "**", "*.jsonl"), recursive=True):
            try:
                with open(f) as fh:
                    cwd = json.loads(fh.readline()).get("payload", {}).get("cwd")
                if cwd:
                    codex[cwd] = codex.get(cwd, 0) + 1
            except Exception:
                pass
    return claude, codex


def chats_in(wt, idx):
    claude, codex = idx
    return claude.get(re.sub(r"[^A-Za-z0-9]", "-", wt), 0) + \
        sum(n for cwd, n in codex.items() if cwd == wt or cwd.startswith(wt + "/"))


def worktree_state(wt, size, now, idx):
    """Classify one git worktree as "remove" (nothing to lose), "idle" or "active"."""
    rc, out = git(wt, "status", "--porcelain")
    dirty = len(out.splitlines()) if rc == 0 else -1
    _, branch = git(wt, "symbolic-ref", "-q", "--short", "HEAD")
    _, contained = git(wt, "for-each-ref", "--count=1", "--contains", "HEAD", "refs/heads", "refs/remotes")
    _, unpushed = git(wt, "rev-list", "--count", "HEAD", "--not", "--remotes")
    unpushed = int(unpushed.strip() or 0) if unpushed.strip().isdigit() else -1
    on_branch = bool(contained.strip())
    chats = chats_in(wt, idx)
    idle = (now - project_mtime(wt)) / 86400
    bits = [branch.strip() or "detached HEAD"]
    if dirty:
        bits.append("git status failed" if dirty < 0 else f"{dirty} uncommitted change{'s' * (dirty > 1)}")
    if not on_branch:
        bits.append("commits on no branch")
    if unpushed:
        bits.append("unpushed check failed" if unpushed < 0 else f"{unpushed} unpushed commit{'s' * (unpushed > 1)}")
    if chats:
        bits.append(f"{chats} chat{'s' * (chats > 1)}")
    bits.append("active today" if idle < 1 else f"idle {idle:.0f}d")
    kind = "active" if idle < 2 else "remove" if (dirty == 0 and on_branch and unpushed == 0 and not chats) else "idle"
    cmd = f"git -C {q(wt)} worktree remove {q(wt)}"
    return kind, {"p": wt, "s": size, "d": " · ".join(bits), "c": cmd, "force": False}


def worktree_items(roots, sizes, now):
    """Find git worktrees under agent worktree folders (one or two levels deep)."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for root in roots:
        try:
            children = [os.path.join(root, c) for c in os.listdir(root)]
        except OSError:
            continue
        for cp in children:
            if not os.path.isdir(cp) or os.path.islink(cp):
                continue
            size = sizes.get(cp, 0)
            if os.path.exists(os.path.join(cp, ".git")):
                jobs.append((cp, size))
                continue
            inner = [os.path.join(cp, g) for g in os.listdir(cp)
                     if os.path.isdir(os.path.join(cp, g)) and os.path.exists(os.path.join(cp, g, ".git"))]
            if len(inner) == 1:              # Codex layout: worktrees/<id>/<repo>
                jobs.append((inner[0], size))
            elif not inner and size >= REC_MIN:
                jobs.append((cp, size))      # leftover folder with no git checkout
    idx = chat_index()
    groups = {"remove": [], "idle": [], "active": []}
    with ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(worktree_state, p, size, now, idx) for p, size in jobs
                if os.path.exists(os.path.join(p, ".git"))]
        for f in futs:
            kind, item = f.result()
            groups[kind].append(item)
    return groups


def recommendations(data):
    sizes = data["sizes"]
    now = time.time()
    recs = []

    def size_of(p):
        return sizes.get(p, 0)

    def add(rid, label, paths, kind, cat, note, cmd=None):
        # paths: abs path str, (path, size), or {"p": path, "s": size, "d": detail, "c": shell command}
        items = []
        for p in paths:
            if isinstance(p, str):
                p = {"p": p, "s": size_of(p)}
            elif isinstance(p, tuple):
                p = {"p": p[0], "s": p[1]}
            if p["s"] >= 1 and inside_home(p["p"]):
                items.append(dict(p, p=os.path.relpath(p["p"], HOME)))
        total = sum(p["s"] for p in items)
        if total < REC_MIN:
            return
        recs.append({"id": rid, "label": label, "kind": kind, "cat": cat, "note": note,
                     "size": total, "paths": sorted(items, key=lambda x: -x["s"]), "cmd": cmd})

    def exists(rel):
        return h(rel) in sizes

    # ---- regenerable caches (safe)
    fixed_safe = [
        ("npm", "npm cache", [h(".npm/_cacache")], "dev", "Re-downloaded on next install.", None),
        ("pnpm", "pnpm store", [h("Library/pnpm/store")], "dev", "Shared package store; re-downloaded as needed.", None),
        ("bun", "Bun install cache", [h(".bun/install/cache")], "dev", "Re-downloaded on next install.", None),
        ("yarn", "Yarn cache", [h("Library/Caches/Yarn"), h(".yarn/berry/cache")], "dev", "Re-downloaded on next install.", None),
        ("pip", "pip cache", [h("Library/Caches/pip"), h(".cache/pip")], "dev", "Re-downloaded on next install.", None),
        ("uv", "uv cache", [h(".cache/uv")], "dev", "Python package cache for uv.", "command -v uv >/dev/null && uv cache clean"),
        ("gradle", "Gradle caches", [h(".gradle/caches"), h(".gradle/wrapper/dists")], "sdk", "Rebuilt on next Gradle build.", None),
        ("derived", "Xcode DerivedData", [h("Library/Developer/Xcode/DerivedData")], "sdk", "Xcode build intermediates.", None),
        ("simcache", "Simulator caches", [h("Library/Developer/CoreSimulator/Caches")], "sdk", "Regenerated by Simulator.", None),
        ("brew", "Homebrew downloads", [h("Library/Caches/Homebrew")], "cache", "Old bottles and downloads.", "command -v brew >/dev/null && brew cleanup -s --prune=all"),
        ("browsers", "Puppeteer & Playwright browsers", [h(".cache/puppeteer"), h("Library/Caches/ms-playwright"), h(".cache/selenium")], "dev", "Headless browser downloads; re-fetched when needed.", None),
        ("cargo", "Cargo registry cache", [h(".cargo/registry")], "sdk", "Rust crate downloads.", None),
        ("gomod", "Go module cache", [h("go/pkg/mod")], "sdk", "Go dependencies.", "command -v go >/dev/null && go clean -modcache"),
        ("conda", "Conda package cache", [h("anaconda3/pkgs"), h("miniconda3/pkgs"), h(".conda/pkgs")], "sdk", "Downloaded package tarballs.", None),
        ("logs", "Logs", [h("Library/Logs")], "cache", "Old app and diagnostic logs.", None),
        ("stremio", "Stremio stream cache", [h("Library/Application Support/stremio-server/stremio-cache")], "cache", "Buffered video from streams you've watched.", None),
        ("nodecache", "Misc tool caches", [h(".cache/node"), h(".cache/prisma"), h(".cache/mongodb-binaries"), h(".cache/yt-dlp"),
                                            h(".cache/gallery-dl"), h(".cache/opencode")],
         "cache", "Downloaded binaries and runtimes; re-fetched on use.", None),
    ]
    # Codex unpacks each runtime update into codex-runtime-install-*; only those leftovers are offered.
    # The live runtime (codex-primary-runtime) runs Codex's own processes and is in NEVER_TOUCH.
    fixed_safe.append(("codexinstall", "Leftover Codex runtime installs",
                       sorted(glob.glob(h(".cache/codex-runtimes/codex-runtime-install-*"))), "cache",
                       "Unpacked copies left over from Codex runtime updates. The runtime Codex uses is kept.", None))
    claimed = set()
    for rid, label, paths, cat, note, cmd in fixed_safe:
        paths = [p for p in paths if p in sizes]
        add(rid, label, paths, "safe", cat, note, cmd)
        claimed.update(paths)

    # ~/Library/Caches children
    lc = h("Library/Caches")
    app_caches = []
    for p, s in sizes.items():
        if os.path.dirname(p) == lc and p not in claimed and s >= 150 * MB:
            app_caches.append((p, s))
    apple = [(p, s) for p, s in app_caches if os.path.basename(p).startswith("com.apple.")]
    other = [(p, s) for p, s in app_caches if not os.path.basename(p).startswith("com.apple.")]
    add("appcache", "App caches", other, "safe", "cache", "~/Library/Caches — apps rebuild these automatically.")
    claimed.update(p for p, _ in app_caches)

    # Electron/app caches under Application Support and elsewhere
    pat = data["patterns"]
    ecache = []
    for p in pat:
        name = os.path.basename(p)
        if name in APP_CACHE_NAMES and "/Library/Application Support/" in p and sizes.get(p, 0) >= 100 * MB:
            if not any(p.startswith(c + "/") for c, _ in ecache):
                ecache.append((p, sizes.get(p, 0)))
    add("electron", "App web caches", ecache, "safe", "cache", "Chromium/Electron caches (Code, Cursor, Slack, Discord…).")

    # ---- build output in projects
    def topmost(paths):
        out = []
        for p in sorted(paths):
            if out and p.startswith(out[-1] + "/"):
                continue
            out.append(p)
        return out

    def skip_area(p):
        """True unless `p` sits in a normal project folder. Tool installs live in hidden folders
        (~/.nvm global packages, ~/.vscode and ~/.cursor extensions, ~/.config/yarn/global…) and
        must never be offered; agent worktrees (.claude/worktrees, .codex/worktrees) are projects."""
        segs = os.path.relpath(os.path.dirname(p), HOME).split("/")
        for i, s in enumerate(segs):
            if s.startswith(".") and not (s in (".claude", ".codex") and segs[i + 1:i + 2] == ["worktrees"]):
                return True
        return segs[0] in ("Library", "Applications") or any(s in ("extensions", "vendor") for s in segs) \
            or (segs[-1] == "lib" and os.path.basename(p) == "node_modules")

    # Agent worktrees (Claude Code, Codex, Conductor). A whole worktree is only offered when there is
    # nothing to lose: clean, pushed, idle 2+ days and no chat ever ran in it. Other idle worktrees
    # only offer their node_modules, so the worktree and its chats stay resumable.
    roots = [p for p in pat if os.path.basename(p) == "worktrees" and "/.git/" not in p
             and ("/.claude/" in p or p == h(".codex/worktrees"))]
    for extra in (h("conductor/workspaces"), h(".codex/worktrees")):
        if extra in sizes and extra not in roots:
            roots.append(extra)
    wts = worktree_items(roots, sizes, now)
    add("wt_remove", "Unused agent worktrees", wts["remove"], "safe", "dev",
        "Clean, fully pushed, idle 2+ days, and no Claude or Codex chat ever ran in them. "
        "Removed with `git worktree remove`; their branches stay in the repo.")
    all_wt = [w["p"] for g in wts.values() for w in g]
    in_wt = lambda p: any(p.startswith(w + "/") for w in all_wt)
    wt_nm = topmost([p for p in pat if os.path.basename(p) == "node_modules"
                     and any(p.startswith(w["p"] + "/") for w in wts["idle"])
                     and os.path.exists(os.path.join(os.path.dirname(p), "package.json"))])
    add("wt_nm", "node_modules in idle worktrees", wt_nm, "safe", "dev",
        "Worktrees with chats or unfinished work stay, so every chat can still be resumed. "
        "Only their installed packages go; run `npm install` there if you pick one back up.")

    nm = topmost([p for p in pat if os.path.basename(p) == "node_modules" and not skip_area(p) and not in_wt(p)
                  and os.path.exists(os.path.join(os.path.dirname(p), "package.json"))])
    idle_nm, active_nm = [], []
    for p in nm:
        age = (now - project_mtime(os.path.dirname(p))) / 86400
        (idle_nm if age > IDLE_DAYS else active_nm).append((p, sizes.get(p, 0)))
    add("nm_idle", f"node_modules in idle projects", idle_nm, "safe", "dev",
        f"Projects untouched for {IDLE_DAYS}+ days. `npm install` brings them back.")
    add("nm_active", "node_modules in active projects", active_nm, "review", "dev",
        "Recently used projects — you'd need to reinstall to keep working.")

    build = topmost([p for p in pat if os.path.basename(p) in (".next", ".turbo", ".nuxt", ".svelte-kit", ".parcel-cache", ".expo")
                     and "/node_modules/" not in p and not skip_area(p)])
    add("buildout", "Next.js & Turbo build output", build, "safe", "dev", "Rebuilt on next `dev`/`build`.")

    rust = [p for p in pat if os.path.basename(p) == "target" and os.path.exists(os.path.join(os.path.dirname(p), "Cargo.toml"))]
    add("rust", "Rust build output", topmost(rust), "safe", "dev", "`cargo build` regenerates it.")

    venvs = []
    for p in topmost([p for p in pat if os.path.basename(p) in (".venv", "venv") and not skip_area(p)]):
        if os.path.exists(os.path.join(p, "pyvenv.cfg")):
            venvs.append(p)
    add("venv", "Python virtualenvs", venvs, "review", "dev", "Recreate with `uv sync` / `pip install -r`.")

    pods = [p for p in pat if os.path.basename(p) == "Pods" and os.path.exists(os.path.join(os.path.dirname(p), "Podfile"))]
    add("pods", "CocoaPods installs", topmost(pods), "review", "dev", "`pod install` restores them.")


    # ---- needs review: big SDKs, models, VMs, backups
    fixed_review = [
        ("android", "Android SDK & emulators", [h("Library/Android/sdk"), h(".android/avd")], "sdk", "Delete system images / AVDs you no longer use.", None),
        ("devsupport", "iOS DeviceSupport", [h("Library/Developer/Xcode/iOS DeviceSupport"), h("Library/Developer/Xcode/watchOS DeviceSupport")], "sdk", "Re-created when you plug in a device.", None),
        ("archives", "Xcode Archives", [h("Library/Developer/Xcode/Archives")], "sdk", "Old app builds; keep ones you need for crash symbolication.", None),
        ("sims", "iOS Simulators", [h("Library/Developer/CoreSimulator/Devices")], "sdk", "Removes simulators for runtimes that are no longer installed.", "xcrun simctl delete unavailable"),
        ("ollama", "Ollama models", [h(".ollama/models")], "ai", "Re-pull with `ollama pull`.", None),
        ("hf", "Hugging Face cache", [h(".cache/huggingface")], "ai", "Downloaded model weights.", None),
        ("lmstudio", "LM Studio models", [h(".lmstudio/models"), h(".cache/lm-studio")], "ai", "Downloaded model weights.", None),
        ("whisper", "Whisper models", [h(".cache/whisper")], "ai", "Speech model weights.", None),
        ("optguide", "Chrome on-device AI models", [h(".gemini/antigravity-browser-profile/OptGuideOnDeviceModel"),
                                                    h("Library/Application Support/Google/Chrome/OptGuideOnDeviceModel")],
         "ai", "Gemini Nano weights Chrome downloads for built-in AI features. Re-downloaded if you use them.", None),
        ("docker", "Docker Desktop data", [h("Library/Containers/com.docker.docker")], "sdk", "Images, containers, volumes. Prune unused ones.", "command -v docker >/dev/null && docker system prune -a"),
        ("colima", "Colima VM disk", [h(".colima")], "sdk", "Container VM with its images and volumes. Deleted with `colima delete`.", "command -v colima >/dev/null && colima delete -f"),
        ("vbox", "Virtual machines", [h("VirtualBox VMs"), h("Virtual Machines.localized")], "sdk", "VM disk images.", None),
        ("iosbackup", "iPhone backups", [h("Library/Application Support/MobileSync/Backup")], "apps", "Old device backups.", None),
        ("trash", "Trash", [h(".Trash")], "cache", "Already deleted — emptying makes it permanent.", None),
    ]
    for rid, label, paths, cat, note, cmd in fixed_review:
        paths = [p for p in paths if p in sizes]
        add(rid, label, paths, "review", cat, note, cmd)

    # Large files outside Library
    lf = []
    for p, s, mt in data["large_files"]:
        r = os.path.relpath(p, HOME)
        if r.startswith(("Library/", ".")) or "/node_modules/" in p or ".photoslibrary/" in p or "VirtualBox VMs" in r:
            continue
        lf.append((p, s))
    inst = [(p, s) for p, s in lf if p.lower().endswith((".dmg", ".iso", ".pkg", ".zip", ".xip", ".tar", ".gz", ".ipsw"))]
    add("installers", "Installers & archives (≥1 GB)", inst, "review", "docs", "Disk images and zips — usually not needed after installing.")
    rest = [x for x in lf if x not in inst]
    add("bigfiles", "Other large files (≥1 GB)", rest, "review", "media", "Videos, recordings, exports. Check before deleting.")

    # Remove paths nested inside another recommendation's paths (avoid double counting)
    allp = sorted({pp["p"] for r in recs for pp in r["paths"]})
    keep = set()
    last = None
    for p in allp:
        if last and p.startswith(last + "/"):
            continue
        keep.add(p)
        last = p
    for r in recs:
        r["paths"] = [pp for pp in r["paths"] if pp["p"] in keep]
        r["size"] = sum(pp["s"] for pp in r["paths"])
    recs = [r for r in recs if r["size"] >= REC_MIN]
    for r in recs:
        r["mode"] = ("worktree" if r["id"] in WORKTREE_IDS else "cmd" if r["id"] in CMD_TOOL
                     else "dir" if r["id"] in REMOVE_DIR_IDS else "contents")
    recs.sort(key=lambda r: (r["kind"] != "safe", -r["size"]))
    return recs


def build_payload(data):
    return {
        "scannedAt": data["scanned_at"],
        "home": data["home"],
        "disk": data["disk"],
        "homeSize": data["sizes"].get(HOME, 0),
        "tree": build_tree(data["sizes"], data["large_files"]),
        "recs": recommendations(data),
    }


def page_html(payload, token=None):
    with open(TEMPLATE) as f:
        tpl = f.read()
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", blob).replace("/*__TOKEN__*/null", json.dumps(token))


def summary(payload):
    safe = sum(r["size"] for r in payload["recs"] if r["kind"] == "safe")
    review = sum(r["size"] for r in payload["recs"] if r["kind"] == "review")
    return f"Safe to remove: {safe / GB:.1f} GB · Needs review: {review / GB:.1f} GB"


def render(data):
    payload = build_payload(data)
    with open(OUT_HTML, "w") as f:
        f.write(page_html(payload))
    print(summary(payload))
    print(f"Wrote {OUT_HTML}")


# ---------------------------------------------------------------- cleaning

PROTECTED = {h(x) for x in ("", "Library", "Library/Application Support", "Library/Caches", "Library/Containers",
                            "Documents", "Desktop", "Downloads", "Developer", "Pictures", "Movies", "Music",
                            ".config", ".ssh", ".codex", ".claude")}


# Never deleted, whatever the recommendation rules say: chat histories and installed CLIs/tools.
NEVER_TOUCH = [h(x) for x in (".claude/projects", ".claude/history.jsonl", ".codex/sessions", ".codex/archived_sessions",
                              ".codex/history.jsonl", ".nvm", ".config", ".vscode", ".vscode-insiders", ".cursor",
                              ".local", ".bun/bin", ".cargo/bin", ".rustup", ".opencode", ".gemini/antigravity",
                              "Library/Application Support/Claude", ".ssh",
                              ".cache/codex-runtimes/codex-primary-runtime")]


def inside_home(p):
    p = os.path.normpath(p)
    if any(p == n or p.startswith(n + "/") or n.startswith(p + "/") for n in NEVER_TOUCH):
        return False                     # never the path itself, anything inside it, or a folder holding it
    return p.startswith(HOME + "/") and p not in PROTECTED


def du_size(p):
    if not os.path.lexists(p):
        return 0
    try:
        out = subprocess.run(["du", "-sk", p], capture_output=True, text=True).stdout
        return int(out.split()[0]) * 1024
    except Exception:
        return 0


def remove_paths(targets):
    """Delete a list of paths; retry once after making read-only trees writable (e.g. Go module cache)."""
    for i in range(0, len(targets), 200):
        subprocess.run(["rm", "-rf", "--", *targets[i:i + 200]], capture_output=True)
    left = [t for t in targets if os.path.lexists(t)]
    for t in left:
        subprocess.run(["chmod", "-R", "u+w", t], capture_output=True)
        subprocess.run(["rm", "-rf", "--", t], capture_output=True)
    left = [t for t in left if os.path.lexists(t)]
    return f"{len(left)} item(s) couldn't be deleted (in use or no permission)" if left else None


def clean_one(rec, entry, ap, ran_cmds):
    """Clean one recommended path. Returns an error string, or None on success."""
    if not inside_home(ap):
        return "refused: not a removable path inside your home folder"
    mode = rec.get("mode", "contents")
    if mode == "cmd":
        if rec["id"] in ran_cmds:
            return None
        ran_cmds.add(rec["id"])
        args = CMD_TOOL[rec["id"]]
        if not shutil.which(args[0]):
            return f"`{args[0]}` isn't installed"
        r = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-300:] or "command failed"
    if not os.path.lexists(ap):
        return None
    if mode == "worktree":
        if not os.path.exists(os.path.join(ap, ".git")):
            return "refused: not a git worktree"
        if "chats" not in ran_cmds:                          # re-check right before deleting
            ran_cmds.add("chats")
            clean_one.idx = chat_index()
        kind, item = worktree_state(ap, 0, time.time(), clean_one.idx)
        if kind != "remove":
            return f"skipped: changed since the scan ({item['d']})"
        args = ["git", "-C", ap, "worktree", "remove", ap]
        r = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        return None if r.returncode == 0 else r.stderr.strip()[-300:] or "git worktree remove failed"
    if os.path.islink(ap) or not os.path.isdir(ap):
        os.remove(ap)
        return None
    if mode == "contents":
        with os.scandir(ap) as it:
            return remove_paths([e.path for e in it])
    return remove_paths([ap])


def apply_removal(data, ap, old, new):
    """Update the in-memory scan after `ap` shrank from `old` to `new` bytes."""
    sizes = data["sizes"]
    delta = max(0, old - new)
    p = os.path.dirname(ap)
    while p.startswith(HOME):
        if p in sizes:
            sizes[p] = max(0, sizes[p] - delta)
        if p == HOME:
            break
        p = os.path.dirname(p)
    for k in [k for k in sizes if k == ap or k.startswith(ap + "/")]:
        del sizes[k]
    if new >= KEEP_KB * 1024:
        sizes[ap] = new
    data["patterns"] = [x for x in data["patterns"] if x in sizes and os.path.lexists(x)]
    data["large_files"] = [x for x in data["large_files"] if os.path.lexists(x[0])]


def run_clean(state, rels, emit):
    data, payload = state["data"], state["payload"]
    index = {p["p"]: (r, p) for r in payload["recs"] for p in r["paths"]}
    jobs = []
    for rel in rels:
        if rel in index:
            jobs.append(index[rel])
        else:
            emit({"p": rel, "ok": False, "error": "not in the current recommendations"})
    free_before = shutil.disk_usage(HOME).free
    emit({"start": len(jobs), "bytes": sum(p["s"] for _, p in jobs)})
    ran_cmds = set()
    for i, (rec, entry) in enumerate(jobs):
        ap = os.path.join(HOME, entry["p"])
        emit({"i": i, "p": entry["p"], "label": rec["label"], "working": True})
        old = data["sizes"].get(ap, entry["s"])
        try:
            err = clean_one(rec, entry, ap, ran_cmds)
        except Exception as e:
            err = str(e)
        new = du_size(ap)
        apply_removal(data, ap, old, new)
        emit({"i": i, "p": entry["p"], "ok": not err, "error": err, "freed": max(0, old - new)})
    emit({"refreshing": True})
    du = shutil.disk_usage("/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/")
    data["disk"] = {"total": du.total, "used": du.used, "free": du.free}
    try:
        state["payload"] = build_payload(data)
        with open(SCAN_JSON, "w") as f:
            json.dump(data, f)
    except Exception as e:                  # the deletes already happened; don't hide that behind a crash
        print(f"  ! couldn't refresh the map: {e!r}. Use Rescan.", flush=True)
    emit({"done": True, "freed": max(0, du.free - free_before), "free": du.free})


def serve(data, port, open_browser):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse
    token = secrets.token_urlsafe(24)       # embedded in the page; required for every API request
    state = {"data": data, "payload": build_payload(data)}
    lock = threading.Lock()                 # one clean or rescan at a time
    job = {"running": False, "phase": "idle"}
    print(summary(state["payload"]))

    def rescan_worker():
        try:
            data = scan(lambda d, s: job.update(dirs=d, secs=round(s)))
            job.update(phase="analyzing")
            state["data"], state["payload"] = data, build_payload(data)
            print(summary(state["payload"]), flush=True)
            job.update(phase="done", scannedAt=data["scanned_at"])
        except Exception as e:
            job.update(phase="error", error=str(e))
        finally:
            job["running"] = False
            lock.release()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def local_only(self):
            host = urlparse("//" + (self.headers.get("Host") or "")).hostname
            origin = self.headers.get("Origin")
            ok = host in ("127.0.0.1", "localhost") and (
                not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost"))
            if not ok:
                self.send_error(403)
            return ok

        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local_only():
                return
            path = urlparse(self.path).path
            if path == "/api/status":
                if self.headers.get("X-Diskmap-Token") != token:
                    return self.send_error(403, "bad token")
                return self.send_json(job)
            if path not in ("/", "/index.html"):
                return self.send_error(404)
            body = page_html(state["payload"], token).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.local_only():
                return
            if self.headers.get("X-Diskmap-Token") != token:
                return self.send_error(403, "bad token")
            if self.path == "/api/rescan":
                if not lock.acquire(blocking=False):
                    return self.send_json({"error": "busy"}, 409)
                prev = state["data"]
                job.clear()
                job.update(running=True, phase="scanning", dirs=0, secs=0, error=None,
                           expectDirs=prev.get("dirs"), expectSecs=prev.get("seconds"))
                print("Rescanning…", flush=True)
                threading.Thread(target=rescan_worker, daemon=True).start()
                return self.send_json(job, 202)
            if self.path != "/api/clean":
                return self.send_error(404)
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.send_error(400)
            if not lock.acquire(blocking=False):
                return self.send_error(409, "A cleanup or rescan is already running")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                def emit(obj):
                    try:                    # keep cleaning even if the tab was closed
                        self.wfile.write((json.dumps(obj) + "\n").encode())
                        self.wfile.flush()
                    except OSError:
                        pass
                    if obj.get("ok") is not None:
                        mark = "✓" if obj["ok"] else "✗"
                        print(f"  {mark} {obj['p']}" + (f"  ({obj['error']})" if obj.get("error") else ""), flush=True)
                run_clean(state, [p for p in req.get("paths", []) if isinstance(p, str)], emit)
            finally:
                lock.release()

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Disk Map running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8777
    if ("--render" in args or "--serve" in args) and os.path.exists(SCAN_JSON):
        with open(SCAN_JSON) as f:
            data = json.load(f)
    else:
        print(f"Scanning {HOME} (this can take a few minutes)…", flush=True)
        data = scan()
    if "--render" in args:
        render(data)
    else:
        serve(data, port, "--no-open" not in args)
