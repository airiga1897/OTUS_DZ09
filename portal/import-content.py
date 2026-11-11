#!/usr/bin/python3
"""Import the bundled WXR by stable post slug and preserve existing post text."""
from argparse import ArgumentParser
from pathlib import Path
from xml.etree import ElementTree
import json
import subprocess

WXR = {"wp": "http://wordpress.org/export/1.2/",
       "content": "http://purl.org/rss/1.0/modules/content/",
       "excerpt": "http://wordpress.org/export/1.2/excerpt/"}
WP = ("/usr/local/bin/wp", "--allow-root", "--path=/var/www/otus")


def wp(*arguments):
    return subprocess.run(
        (*WP, *arguments), check=True, capture_output=True, text=True, encoding="utf-8",
        timeout=90,
    ).stdout.strip()


def field(record, tag):
    return record.findtext(tag, namespaces=WXR) or ""


def materials(path):
    items = ElementTree.parse(path).findall("./channel/item")
    records = []
    for item in items:
        lesson = next(field(meta, "wp:meta_value")
                      for meta in item.findall("wp:postmeta", WXR)
                      if field(meta, "wp:meta_key") == "otus_lesson")
        records.append({"lesson": lesson, "slug": field(item, "wp:post_name"),
                        "title": field(item, "title"), "summary": field(item, "excerpt:encoded"),
                        "body": field(item, "content:encoded")})
    if ({row["lesson"] for row in records} != set(map(str, range(1, 10))) or
            len(records) != 9 or len({row["slug"] for row in records}) != len(records) or
            any(not all(row.values()) for row in records)):
        raise ValueError("WXR must contain nine complete materials with distinct slugs.")
    return records


def find(slug):
    matches = json.loads(wp(
        "post", "list", "--post_type=post", "--post_status=any", f"--post_name__in={slug}",
        "--fields=ID,post_name,post_status", "--format=json",
    ))
    matches = [post for post in matches if post["post_name"] == slug]
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous WordPress slug: {slug}")
    return matches[0] if matches else None


def apply(record, check):
    slug = record["slug"]
    lesson = record["lesson"]
    post = find(slug)
    if not post:
        if check:
            raise RuntimeError(f"Missing material: {slug}. Run import-content.")
        wp("post", "create", "--post_type=post", "--post_status=publish",
           f"--post_name={slug}", f'--post_title={record["title"]}',
           f'--post_excerpt={record["summary"]}', f'--post_content={record["body"]}',
           f'--meta_input={json.dumps({"otus_lesson": lesson})}', "--porcelain")
        post = find(slug)
        if not post:
            raise RuntimeError(f"WordPress did not save the expected slug: {slug}")
        action = "created"
    else:
        action = "preserved"
    post_id = str(post["ID"])
    values = json.loads(wp("post", "meta", "list", post_id, "--keys=otus_lesson", "--format=json"))
    if not values and not check:
        wp("post", "meta", "add", post_id, "otus_lesson", lesson)
        values = [{"meta_value": lesson}]
    if [str(value["meta_value"]) for value in values] != [lesson] or post["post_status"] != "publish":
        raise RuntimeError(f"Check WordPress status and lesson metadata for {slug}.")
    return action


def main():
    import fcntl

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().with_name("materials.xml")
    with Path("/run/lock/otus-dz09-import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for record in materials(path):
            action = apply(record, args.check)
            print(f'{record["slug"]}: {"verified" if args.check else action}')


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"WP-CLI failed: {error.stderr.strip()}") from error
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error)) from error
