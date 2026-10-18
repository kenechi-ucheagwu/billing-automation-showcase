from pathlib import Path
import re

# The generator's fixed starting point. Every other date is computed at runtime,
# so no file in the repository should contain one. Split so this file passes too.
REFERENCE_DAY = "REFERENCE_DAY = date(" + "2026, 1, 1)"
YEAR = r"(?:19|20)\d{2}"
DATE_PATTERNS = [rf"\b{YEAR}[-/.]\d{{1,2}}[-/.]\d{{1,2}}\b",
                 rf"\b\d{{1,2}}[-/.]\d{{1,2}}[-/.]{YEAR}\b",
                 rf"\bdate\(\s*{YEAR}\s*,"]


def test_repo_text_hygiene():
    root = Path(__file__).resolve().parents[1]
    skip = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "build", "dist", "sandbox"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(p in skip or p.startswith("pytest-cache-files-")
               or p.endswith((".egg-info", ".sandbox")) for p in relative.parts):
            continue
        if not path.is_file():
            continue
        raw = path.read_bytes()
        assert raw.isascii(), str(relative)
        text = raw.decode("ascii")
        if relative.as_posix() == "billing_guard/synthetic.py":
            assert text.count(REFERENCE_DAY) == 1
            text = text.replace(REFERENCE_DAY, "")
        assert not re.search(r"\d{15,}", text), str(relative)
        assert not any(re.search(pattern, text) for pattern in DATE_PATTERNS), str(relative)
