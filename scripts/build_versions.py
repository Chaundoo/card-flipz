"""Write versions.json: one short id per data file, based on its contents.

The site downloads this small file first and only re-downloads the data files whose id changed.
Every GitHub job that changes a data file runs this before saving; the "Update versions.json"
job runs it after uploads made through GitHub's website.
"""
import hashlib, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def file_id(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:12]


def main():
    files = ["eras/eras.json"]
    with open(os.path.join(ROOT, "eras/eras.json")) as f:
        for e in json.load(f).get("eras", []):
            files.append(f"eras/{e['id']}/{e['id']}.json")
    files += ["pairings.json", "profitlog.json"]
    out = {"files": {}}
    for rel in files:
        v = file_id(rel)
        if v:
            out["files"][rel] = v
    path = os.path.join(ROOT, "versions.json")
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    old = open(path).read() if os.path.exists(path) else ""
    if text != old:
        with open(path, "w") as f:
            f.write(text)
        print("versions.json updated")
    else:
        print("versions.json already up to date")


if __name__ == "__main__":
    main()
