import sys
import pypdf

pdf = sys.argv[1]
out = sys.argv[2]
r = pypdf.PdfReader(pdf)
parts = []
for i, p in enumerate(r.pages):
    try:
        parts.append("--- page %d ---\n%s" % (i + 1, p.extract_text() or ""))
    except Exception as e:
        parts.append("--- page %d ERROR %s ---" % (i + 1, e))
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(parts))
print("pages", len(r.pages))
